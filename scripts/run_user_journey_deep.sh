#!/usr/bin/env bash
# 深度人类旅程运行器：跑核心流水线五个页面（蒸馏/基因组/竞技场/运行记录/用量）。
#
# 与 run_user_journey.sh 同一套起法：真后端 + 真实构建产物 + 真实浏览器。
# 单独一份的原因：这一轮要跑两次完整蒸馏，耗时远长于增删改类旅程，
# 混在一起会让短旅程的失败被长任务掩盖。
set -u

cd "$(dirname "$0")/.."

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

DIST=frontend/dist
BE_PID=${TMPDIR:-/tmp}/journey-deep-be.pid
FE_PID=${TMPDIR:-/tmp}/journey-deep-fe.pid

# 三个旅程脚本共用同一组端口（后端 8787、静态服务 8899）。前一段旅程的
# 进程若没退干净，这里绑不上端口，报出来的是"服务未就绪"——症状指向本段
# 没起来，真因是上一段的进程仍占着端口。所以启动前要一并清掉全部六个
# pid 文件，只清自己的不够。
for f in "$BE_PID" "$FE_PID" \
         "${TMPDIR:-/tmp}/journey-be.pid" "${TMPDIR:-/tmp}/journey-fe.pid" \
         "${TMPDIR:-/tmp}/journey-full-be.pid" "${TMPDIR:-/tmp}/journey-full-fe.pid"; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
done
sleep 1

# 数据目录必须由 Python 生成并回传绝对路径：Git Bash 的 /tmp 与 Windows
# 版 Python 看到的 /tmp 不是同一个地方。用 shell 的 /tmp 兜底时，
# `rm -rf` 删的是 shell 那一侧，Python 建的目录其实没被删掉 ——
# 上一轮的运行记录会留在里面，下一轮的条目数断言全是脏的，
# 症状却像"功能重复写入"。
FIX=${TMPDIR:-/tmp}/journey-deep-fixtures.sh
PY0=python3
command -v python3 >/dev/null 2>&1 || PY0=python
"$PY0" scripts/make_journey_fixtures.py --out "$FIX" || exit 1
# shellcheck disable=SC1090
set -a
. "$FIX"
set +a
: "${OF_JOURNEY_HOME:?fixtures 未给出数据目录}"
HOME_DIR="$OF_JOURNEY_HOME"

for f in "$BE_PID" "$FE_PID"; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
done
sleep 1

rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR"

if [ ! -f "$DIST/index.html" ]; then
  echo "FAIL 缺少构建产物 $DIST/index.html"
  echo "界面旅程必须跑在真实产物上；跑在源码目录上等于没跑。"
  exit 1
fi

OMEGAFORGE_HOME="$HOME_DIR" OMEGAFORGE_ALLOWED_ORIGINS=localhost \
  nohup "$PY" -m omegaforge.server --port 8787 > /tmp/journey-deep-be.log 2>&1 &
echo $! > "$BE_PID"

cd "$DIST"
nohup "$PY" -m http.server 8899 > /tmp/journey-deep-fe.log 2>&1 &
echo $! > "$FE_PID"
cd - >/dev/null

b=""; f=""
for _ in $(seq 1 40); do
  b=$(curl -s -m 2 -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/api/status || true)
  f=$(curl -s -m 2 -o /dev/null -w "%{http_code}" http://127.0.0.1:8899/ || true)
  if [ "$b" = "200" ] && [ "$f" = "200" ]; then break; fi
  sleep 1
done
echo "backend=$b frontend=$f"
if [ "$b" != "200" ] || [ "$f" != "200" ]; then
  echo "FAIL 服务未就绪（backend=$b frontend=$f）"
  tail -5 /tmp/journey-deep-be.log 2>/dev/null || true
  exit 1
fi

node scripts/user_journey_deep.js http://127.0.0.1:8899/
rc=$?

for f in "$BE_PID" "$FE_PID"; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
done
exit $rc
