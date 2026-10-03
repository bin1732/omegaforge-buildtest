"""主模型换版后的目录判定核查。

约束：目录里只有另一版本的主文件、却缺必需条目时，不得被判为"有这个模型"——
否则随包内置的新版那份会被跳过，界面显示"未安装"，用户装过却要重下一次。
判据与运行时共用同一套（voice/spec.dir_has_kind），不另立一份。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omegaforge.voice import spec, assets  # noqa: E402

NAME = spec.SPEC["tts"]["name"]
BIG = 60_000  # 大于 MODEL_MIN_BYTES，模拟真实体积的 fp32/int8 主模型


def mk(root, files):
    d = root / NAME
    d.mkdir(parents=True, exist_ok=True)
    for rel, size in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return d


def case(name, files, expect_has, note=""):
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        mk(root, files)
        got = spec.dir_has_kind(root, "tts")
        ok = got == expect_has
        print(f"{'PASS' if ok else 'FAIL'} {name}: dir_has_kind={got} "
              f"(期望 {expect_has}) {note}")
        return ok


def main():
    results = []

    # 1. 只有另一版本的主文件（fp32），缺 int8
    results.append(case(
        "只有另一版本的主文件（缺必需条目）",
        {"model.onnx": BIG, "tokens.txt": 655, "lexicon.txt": BIG,
         "dict/jieba.dict.utf8": BIG},
        False,
        "——不得被判为'有这个模型'，否则内置的 int8 那份会被跳过"))

    # 2. int8 齐备
    results.append(case(
        "int8 齐备",
        {"model.int8.onnx": BIG, "tokens.txt": 655, "lexicon.txt": BIG,
         "dict/jieba.dict.utf8": BIG},
        True))

    # 3. int8 只有占位体积
    results.append(case(
        "int8 为占位小文件",
        {"model.int8.onnx": 500, "tokens.txt": 655, "lexicon.txt": BIG,
         "dict/jieba.dict.utf8": BIG},
        False, "——占位产物不得算已安装"))

    # 4. 旧模型与新模型同时在（升级后未清理）：应判可用
    results.append(case(
        "新旧同在",
        {"model.onnx": BIG, "model.int8.onnx": BIG, "tokens.txt": 655,
         "lexicon.txt": BIG, "dict/jieba.dict.utf8": BIG},
        True))

    # 5. assets.has_model 与 resolve 的一致性：该目录必须被跳过
    with tempfile.TemporaryDirectory() as t:
        home = Path(t) / "home"
        (home / "models").mkdir(parents=True)
        mk(home / "models", {"model.onnx": BIG, "tokens.txt": 655,
                             "lexicon.txt": BIG, "dict/jieba.dict.utf8": BIG})
        got = assets.has_model(home / "models", NAME)
        ok = got is False
        print(f"{'PASS' if ok else 'FAIL'} assets.has_model 对该目录="
              f"{got}（期望 False：必需条目不齐，须继续找内置那份）")
        results.append(ok)

    # 6. 单一定义：三处指向同一文件名
    from omegaforge.voice import tts as tts_mod
    same = (tts_mod.TTS_MODEL_FILE == spec.TTS_MODEL_FILE
            == spec.TTS_FILES[0] == spec.REQUIRED["tts"][0])
    print(f"{'PASS' if same else 'FAIL'} 单一定义：tts/spec下载清单/运行时需求"
          f" 三处一致 = {same}")
    results.append(same)

    bad = results.count(False)
    print(f"\n=== {len(results) - bad}/{len(results)} 通过 ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
