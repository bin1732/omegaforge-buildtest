#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为安装包写体积与校验和清单，供产物分支回传与两侧对账。

## 为什么单独写清单

产物分支只回传清单、不回传安装包本体（git 单文件上限 100MB，内置语音后
安装包远超该上限，push 会被直接拒绝）。此时分支上若什么都不留，"这一轮
到底打没打出包、多大"就无从复查——失败那轮恰恰最需要这个信息。

清单同时是装机验收与回传通道的对账依据：装机步骤量到的体积与这里记录的
体积不一致时，说明打的与验的不是同一个包。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

CHUNK = 1 << 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--run", default="")
    args = ap.parse_args()

    if not os.path.isfile(args.exe):
        print(f"FAIL 安装包不存在：{args.exe}")
        return 1

    h = hashlib.sha256()
    with open(args.exe, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)

    data = {
        "run": args.run,
        "name": os.path.basename(args.exe),
        "bytes": os.path.getsize(args.exe),
        "sha256": h.hexdigest(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
