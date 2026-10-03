/// 构建期注入的环境变量声明。
///
/// 这里的类型只声明本项目实际读取的键，避免把整包环境类型都放进来。
interface ImportMetaEnv {
  readonly VITE_BACKEND_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
