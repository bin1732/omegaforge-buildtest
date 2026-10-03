// Tauri 必需的构建脚本。
//
// 为什么必须存在（实测教训，勿删）：
//   此前连续 21 次 CI 失败的根因就是缺这个文件。
//   tauri::generate_context!() 宏依赖 OUT_DIR 环境变量，而 OUT_DIR 只有
//   在 crate 存在 build script 时才由 Cargo 注入。缺了它，编译报：
//     error: OUT_DIR env var is not set, do you have a build script?
//   该报错发生在依赖全部编译完成之后（约 5 分钟处），极具迷惑性。
fn main() {
    tauri_build::build()
}
