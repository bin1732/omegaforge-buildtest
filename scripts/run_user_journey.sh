#!/usr/bin/env bash
# 人类用户旅程运行器（仓库内版本，供 CI 调用）。
#
# 起真后端 + 托管真实构建产物 + 真实浏览器操作界面。
# 与本地 /data/workspace/run_journey.sh 的区别：路径取自仓库自身，
# 命令名兼容 Windows runner（python 而非 python3）。
#
# 为什么不能用 pkill -f "omegaforge.server"：
# 本脚本自身的命令行里也含这个字符串，pkill 会把运行它的 shell 一起杀掉，
# 清理步骤因此根本没执行，数据目录留着上一轮的内容——下一轮计数全是脏的，
# 症状却像"功能重复写入"。按 PID 文件精确结束。
set -u

cd "$(dirname "$0")/.."

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

DIST=frontend/dist
BE_PID=${TMPDIR:-/tmp}/journey-be.pid
FE_PID=${TMPDIR:-/tmp}/journey-fe.pid

# 旅程用到的目录（数据目录、两个技能包）必须由 Python 生成并回传绝对路径。
# 不能在这里用 mkdir /tmp/... ：runner 上 shell 是 Git Bash，它的 /tmp 与
# Windows 版 Python 看到的 /tmp 不是同一个地方，shell 建好的目录后端根本
# 看不见，报「缺少技能说明文件」——症状像"安装功能坏了"（run77 真实失败）。
FIX=${TMPDIR:-/tmp}/journey-fixtures.sh
PY0=python3
command -v python3 >/dev/null 2>&1 || PY0=python
# 基目录可由 OF_JOURNEY_FIXTURE_ROOT 指定。
# 写在仓库内是为了让夹具文件与后端看到同一棵目录树；真正的失败原因不是
# 目录位置——run90 日志显示导出值被 shell 吃掉了反斜杠
# （D:aomegaforge-buildtestomegaforge-buildtest...）：bash 把反斜杠当转义
# 符，`export V=D:\a\b` 载入后变成 `D:ab`。夹具里的值必须加单引号。
FIX_BASE=${OF_JOURNEY_FIXTURE_ROOT:-${TMPDIR:-/tmp}}
"$PY0" scripts/make_journey_fixtures.py --base "$FIX_BASE/journey-fixtures" \
  --out "$FIX" || exit 1

# shellcheck disable=SC1090
# set -a：载入期间的新变量一律导出。夹具文件已自带 export，这里是第二道——
# 任一道缺失都会让子进程（node）读不到，报成"请先运行夹具"。
set -a
. "$FIX"
set +a
: "${OF_JOURNEY_HOME:?fixtures 未给出数据目录}"
: "${OF_SKILL_A:?fixtures 未给出技能包 A}"
: "${OF_SKILL_B:?fixtures 未给出技能包 B}"

# 生成后立刻自检：说明文件不在就报出真实路径与目录内容，不要留到界面上
# 变成一句「缺少技能说明文件」——那会把根因指向安装功能本身。
"$PY0" - <<'PYCHECK' || exit 1
import os, sys
for k in ("OF_SKILL_A", "OF_SKILL_B"):
    d = os.environ.get(k)
    if not d:
        print(f"FAIL 夹具未给出 {k}"); sys.exit(1)
    md = os.path.join(d, "SKILL.md")
    if not os.path.isfile(md):
        print(f"FAIL {k} 下没有技能说明文件：{md}")
        try:
            print("  该目录内容：", os.listdir(d))
        except OSError as e:
            print("  该目录不可列：", e)
        sys.exit(1)
print("夹具自检通过：两个技能包都含技能说明文件")
PYCHECK
HOME_DIR="$OF_JOURNEY_HOME"

# 三个旅程脚本共用同一组端口（后端 8787、静态服务 8899）。残留进程占住
# 端口时这里绑不上，报出来的是"服务未就绪"——症状指向本段没起来，真因是
# 别处的进程仍占着端口。所以启动前一并清掉全部六个 pid 文件。
for f in "$BE_PID" "$FE_PID" \
         "${TMPDIR:-/tmp}/journey-deep-be.pid" "${TMPDIR:-/tmp}/journey-deep-fe.pid" \
         "${TMPDIR:-/tmp}/journey-full-be.pid" "${TMPDIR:-/tmp}/journey-full-fe.pid"; do
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

# 数据源自环境变量，与装机运行时同一套解析路径
OMEGAFORGE_HOME="$HOME_DIR" OMEGAFORGE_ALLOWED_ORIGINS=localhost \
  nohup "$PY" -m omegaforge.server --port 8787 > /tmp/journey-be.log 2>&1 &
echo $! > "$BE_PID"

cd "$DIST"
nohup "$PY" -m http.server 8899 > /tmp/journey-fe.log 2>&1 &
echo $! > "$FE_PID"
cd - >/dev/null

# 等两个服务就绪才继续：界面连不上后端时所有步骤会一起假失败，
# 症状是"功能全坏"，而实际是没等。
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
  tail -5 /tmp/journey-be.log 2>/dev/null || true
  exit 1
fi

node scripts/user_journey.js http://127.0.0.1:8899/
rc=$?

for f in "$BE_PID" "$FE_PID"; do
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
done
exit $rc
