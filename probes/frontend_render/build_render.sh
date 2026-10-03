#!/bin/bash
# 把前端源码编译成 jsdom 可直接 require 的 CommonJS。
#
# 为什么用纯 tsc 而不是 esbuild 打包：沙盒里的 esbuild 二进制在部分
# 环境下 SIGSEGV（此前实测），tsc 是纯 JS，稳定。代价是不做 bundling，
# 所以 Node 侧要靠 Module._resolveFilename 处理 @/ 别名与第三方包。
set -e
cd "${FE_HOME:-/tmp/fe}"
rm -rf "${FE_OUT:-/tmp/ssrout}"
# 必须排除 vite.config.ts：它不在 src 下，tsc 会把它输出到 /tmp/fe/vite.config.js（CJS），
# **覆盖**真正的 ESM 配置，导致 npx vite build 报
# "exports is not defined in ES module scope"（实测踩到）。
# 必须走 -p：命令行直接给 src/main.tsx 时 tsc **忽略 tsconfig.json**，
# 于是 baseUrl/paths 不生效，所有 `@/...` 导入报 TS2307，解析不到的文件
# 一律不 emit（实测：pages/ 输出 0 个文件，守卫全线 RENDER-FAIL）。
# 单独生成一份渲染专用配置，extends 原配置以继承 paths，include 只收 src
# （vite.config.ts 必须排除：它会以 CJS 输出覆盖真正的 ESM 配置，
#  导致 npx vite build 报 "exports is not defined in ES module scope"）。
cat > "${FE_HOME:-/tmp/fe}"/tsconfig.render.json <<JSON
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmit": false,
    "noEmitOnError": false,
    "module": "commonjs",
    "moduleResolution": "node",
    "jsx": "react-jsx",
    "esModuleInterop": true,
    "allowImportingTsExtensions": false,
    "isolatedModules": false,
    "declaration": false,
    "sourceMap": false,
    "outDir": "${FE_OUT:-/tmp/ssrout}/src",
    "rootDir": "src"
  },
  "include": ["src"]
}
JSON
# 产物是 CJS，但前端 package.json 带 "type": "module"，
# 会让 .js 被 Node 当 ESM 解析，require 时报
# "exports is not defined in ES module scope"（实测踩到）。
printf '{"type":"commonjs"}' > "${FE_OUT:-/tmp/ssrout}"/package.json
node node_modules/typescript/bin/tsc -p "${FE_HOME:-/tmp/fe}"/tsconfig.render.json 2>&1 | head -20 || true
echo "--- 产物 ---"
ls "${FE_OUT:-/tmp/ssrout}"/src/pages/ 2>/dev/null | wc -l
ls "${FE_OUT:-/tmp/ssrout}"/src/pages/ 2>/dev/null | head -12
