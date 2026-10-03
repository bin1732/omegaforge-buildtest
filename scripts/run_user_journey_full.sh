#!/usr/bin/env bash
# 用户层全功能旅程运行器（第二批）：检索 / 会话 / 语音 / 供应商。
#
# 与 run_user_journey.sh 的分工：那个验增删除改，这个验带外部依赖的功能。
# 两者都必须跑在**真实构建产物**上，不是源码目录。
#
# 语音模型：打包时下到 src-tauri/_internal/models，而源码态后端按
# "仓库根/models → 数据目录/models" 查找，看不到打包产物里的那份。
# 如果不把它接过来，界面会显示"不可用"，报成"内置模型没生效"——
# 那是环境差异，不是产品缺陷，会把排查方向带偏。这里显式接过来。
set -u

cd "$(dirname "$0")/.."

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

DIST=frontend/dist
BE_PID=${TMPDIR:-/tmp}/journey-full-be.pid
FE_PID=${TMPDIR:-/tmp}/journey-full-fe.pid
HOME_DIR=${TMPDIR:-/tmp}/journey-full-home

# 后端端口必须落在前端 PORT_CANDIDATES 之内。两端不一致时后端其实活着、
# 就绪检查也返回 200，而界面每个功能都报"无法连接到本机服务"——症状
# 指向服务没起来，真因是端口对不上，排查方向完全相反。
BE_PORT=8787
FE_PORT=8899

for f in "$BE_PID" "$FE_PID" \
         "${TMPDIR:-/tmp}/journey-be.pid" "${TMPDIR:-/tmp}/journey-deep-be.pid"; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
done
sleep 1

rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR"
export OF_HOME_DIR="$HOME_DIR"
export OMEGAFORGE_HOME="$HOME_DIR"

# ---- 把打包产物里的语音模型接到旅程的数据目录下 ----
BUNDLED=src-tauri/_internal/models
if [ -d "$BUNDLED" ]; then
  if ln -s "$(cd "$BUNDLED" && pwd)" "$HOME_DIR/models" 2>/dev/null; then
    echo "语音模型：已链接 $BUNDLED -> $HOME_DIR/models"
  else
    # 符号链接在 Windows runner 上可能不可用；退回到复制。
    # 复制失败必须报出来：静默失败会让"模型不可用"看起来像产品缺陷。
    cp -r "$BUNDLED" "$HOME_DIR/models" \
      && echo "语音模型：已复制" \
      || echo "WARN 语音模型不可用（链接与复制都失败）"
  fi
else
  echo "WARN 打包产物里没有 $BUNDLED —— 语音将显示为不可用"
fi

# 接过来之后必须真的可用：链接成功但内容为空时，界面仍显示"不可用"，
# 与没接过来完全一样，而日志里只会看到一句"已链接"。
"$PY" - <<'PYCHECK' || exit 1
import os, sys
from omegaforge.voice import spec
home = os.environ.get("OF_HOME_DIR", "")
root = os.path.join(home, "models")
if not os.path.isdir(root):
    print(f"WARN 旅程数据目录下没有模型目录：{root}")
    sys.exit(0)
for kind in ("tts", "asr"):
    ok = spec.dir_has_kind(root, kind)
    print(f"  {kind}: {'可用' if ok else '不可用'}  ({root})")
PYCHECK

if [ ! -f "$DIST/index.html" ]; then
  echo "FAIL 缺少构建产物 $DIST/index.html"
  exit 1
fi

OMEGAFORGE_ALLOWED_ORIGINS=localhost \
  nohup "$PY" -m omegaforge.server --port "$BE_PORT" > /tmp/journey-full-be.log 2>&1 &
echo $! > "$BE_PID"

cd "$DIST"
nohup "$PY" -m http.server "$FE_PORT" > /tmp/journey-full-fe.log 2>&1 &
echo $! > "$FE_PID"
cd - >/dev/null

b=""; f=""
for _ in $(seq 1 40); do
  b=$(curl -s -m 2 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$BE_PORT/api/status" || true)
  f=$(curl -s -m 2 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$FE_PORT/" || true)
  if [ "$b" = "200" ] && [ "$f" = "200" ]; then break; fi
  sleep 1
done
echo "backend=$b frontend=$f"
if [ "$b" != "200" ] || [ "$f" != "200" ]; then
  echo "FAIL 服务未就绪（backend=$b frontend=$f）"
  tail -20 /tmp/journey-full-be.log 2>/dev/null || true
  exit 1
fi

node scripts/user_journey_full.js "http://127.0.0.1:$FE_PORT/"
rc=$?

# 后端日志必须一并留下：界面功能失败时只看到"无法连接到本机服务"，
# 而后端是否崩过、崩在哪一行全在这个日志里。不落盘就只能靠猜。
echo "---- 后端日志尾部 ----"
tail -20 /tmp/journey-full-be.log 2>/dev/null || true
echo "---- 后端日志结束 ----"

for f in "$BE_PID" "$FE_PID"; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
done
exit $rc
