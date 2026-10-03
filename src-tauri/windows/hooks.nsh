; 卸载前必须先结束仍在运行的进程。
;
; 卸载器删不掉被占用的文件时会静默跳过，且不会重试。主程序与后端在运行
; 时占用着自身文件与运行时库（python3xx.dll 等），于是整棵运行时会被跳过
; 并永久留在用户磁盘上——卸载界面显示的却是卸载完成。用户以为卸干净了，
; 磁盘没有释放，残留目录还可能让下一次安装覆盖失败。
;
; 因此这里在删除任何文件之前先结束进程，再留一小段时间让句柄真正释放。
!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "正在结束仍在运行的进程…"
  ExecWait 'taskkill /F /T /IM "omegaforge-studio.exe"' $0
  ExecWait 'taskkill /F /T /IM "omegaforge-backend.exe"' $0
  Sleep 1200
!macroend

; 安装前同样处理：覆盖安装时旧版本可能仍在运行，不做处理的话新文件同样
; 会被跳过，用户拿到的是新旧混杂的安装。
!macro NSIS_HOOK_PREINSTALL
  DetailPrint "正在结束仍在运行的旧版本…"
  ExecWait 'taskkill /F /T /IM "omegaforge-studio.exe"' $0
  ExecWait 'taskkill /F /T /IM "omegaforge-backend.exe"' $0
  Sleep 1200
!macroend

; 卸载后兜底清理运行时残留。
;
; 只结束进程并不保证清空：卸载器按安装清单删文件，而资源目录是否逐项进
; 入清单取决于打包器的实现。清单里没有的条目不会被删——卸载界面依旧显示
; 完成，用户磁盘上却留着整棵运行时。
;
; 这里清的是明确属于运行时的条目，且只在这个目录内操作：
;   - _internal         运行时库目录
;   - omegaforge-backend.exe  sidecar
; 用户数据不在安装目录（打包运行时一律写到 %APPDATA%），因此不会被波及。
; 即便如此仍做目录存在性判断，避免在空变量上执行递归删除。
!macro NSIS_HOOK_POSTUNINSTALL
  DetailPrint "清理运行时残留…"
  StrCmp "$INSTDIR" "" postuninstall_done
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\omegaforge-backend.exe"
  postuninstall_done:
!macroend
