// OmegaForge Studio — Tauri 2 桌面壳
//
// 职责边界：本壳只负责"承载 Web 前端 + 托管 Python 后端 sidecar"。
// 不含任何业务逻辑，业务逻辑全部在 Python 侧（omegaforge/）。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

// creation_flags 是 Windows 专属 trait 方法，不在 std 默认作用域内。
// 缺了这行编译报 E0599: no method named `creation_flags`（实测，勿删）。
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

/// 后端监听端口（与 omegaforge/server.py 默认值一致）
const BACKEND_PORT: u16 = 8787;
/// 就绪等待上限；超时后仍然开窗口，由前端显示后端异常，而不是让用户干等
const READY_TIMEOUT: Duration = Duration::from_secs(30);

/// 持有 sidecar 子进程句柄，确保退出时可被 kill。
struct Sidecar(Arc<Mutex<Option<CommandChild>>>);

/// Windows 上必须杀整棵进程树：PyInstaller 会派生子进程，
/// 仅 kill 主进程会留下占用 8787 端口的残留进程。
/// tauri-plugin-shell 内部已内置 CREATE_NO_WINDOW，故不会出现控制台黑框。
#[cfg(target_os = "windows")]
fn kill_tree(child: CommandChild) {
    let pid = child.pid();
    let _ = child.kill();
    let _ = std::process::Command::new("taskkill")
        .args(["/F", "/T", "/PID", &pid.to_string()])
        .creation_flags(0x08000000) // CREATE_NO_WINDOW
        .output();
}

#[cfg(not(target_os = "windows"))]
fn kill_tree(child: CommandChild) {
    let _ = child.kill();
}

fn port_open(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

fn shutdown(state: &Sidecar) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(child) = guard.take() {
            kill_tree(child);
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let (mut rx, child) = app
                .shell()
                .sidecar("omegaforge-backend")?
                .spawn()
                .map_err(|e| format!("无法启动 Python 后端 sidecar: {e}"))?;

            let sidecar = Sidecar(Arc::new(Mutex::new(Some(child))));
            app.manage(sidecar);

            // 转发后端 stdout/stderr 到 Rust 日志，便于定位打包后的问题
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            print!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            eprint!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(payload) => {
                            eprintln!(
                                "[backend] 退出 code={:?} signal={:?}",
                                payload.code, payload.signal
                            );
                        }
                        _ => {}
                    }
                }
            });

            // 阻塞等待后端就绪，避免前端首屏即白屏/报错
            let started = Instant::now();
            while !port_open(BACKEND_PORT) {
                if started.elapsed() > READY_TIMEOUT {
                    eprintln!(
                        "[backend] 端口 {} 在 {:?} 内未就绪，继续启动前端",
                        BACKEND_PORT, READY_TIMEOUT
                    );
                    break;
                }
                thread::sleep(Duration::from_millis(150));
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // 主窗口销毁时清理 sidecar，防止关闭后残留 Python 进程占端口
            if let WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<Sidecar>() {
                    shutdown(&state);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("构建 OmegaForge Studio 失败")
        .run(|app_handle, event| {
            // 兜底：任何退出路径都要清理，避免僵尸进程
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<Sidecar>() {
                    shutdown(&state);
                }
            }
        });
}
