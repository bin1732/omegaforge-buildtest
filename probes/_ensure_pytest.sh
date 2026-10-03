#!/bin/sh
# 准备 pytest 运行环境。
#
# 沙盒的 /tmp 在命令之间会被回收，且 pip download 对索引的解析偶发失败，
# 因此这里做了重试与双索引回退。取包约 10~20 秒。
DEST=${1:-/tmp/pylibs}
DL=/tmp/pipdl
rm -rf "$DL" "$DEST"
mkdir -p "$DL" "$DEST"

ok=0
i=1
while [ $i -le 4 ]; do
  if pip download pytest -d "$DL" -q >/dev/null 2>&1; then
    if ls "$DL"/*.whl >/dev/null 2>&1; then ok=1; break; fi
  fi
  i=$((i + 1))
  sleep 2
done

if [ $ok -ne 1 ]; then
  echo "[ensure_pytest] 取包失败" >&2
  exit 1
fi

for w in "$DL"/*.whl; do
  python3 -c "import zipfile;zipfile.ZipFile('$w').extractall('$DEST')"
done
PYTHONPATH="$DEST" python3 -m pytest --version
