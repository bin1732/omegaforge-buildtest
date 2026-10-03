"""语音模型的唯一清单：下载文件与运行时需求同一处定义。

## 为什么单独成模块

此前下载清单写在 voice/fetch.py 的 SPEC["tts"]["files"]，运行时需求写在
voice/tts.py 的 status() 里：

    下载：["model.onnx", "tokens.txt"]
    运行：[TTS_MODEL_FILE, "tokens.txt", "lexicon.txt", "dict"]

两份不一致（下载缺 lexicon.txt 与 dict/），而**任一侧单独看都成立**：
下载器"成功"了（它只对自己清单里的文件负责），status 如实报缺什么
（它只对运行需求负责）。结果是——模型装完了、界面仍显示未安装、
用户点合成就报"模型未下载完整"，而排查时两边都没有错。

这类漂移不可能靠"记得同步两处"避免，只能让两边读同一份定义。
现在：
  · fetch.py     用本模块下载
  · asr.py/tts.py 用本模块判定 status
  · fetch_voice_assets.py 用本模块做打包内置
任何一侧改清单，三处同时生效。

## 文件清单的来源

来自 sherpa-onnx 官方模型仓库与其源码中的实际断言（不是推测）：

· TTS 需要 dict/ 下的 jieba 五个文件——依据 sherpa-onnx/csrc/
  melo-tts-lexicon.cc 里的 AssertFileExists(dict_dir + "/jieba.dict.utf8")
  等五行断言。缺任一个，加载时报的是"文件不存在"，而界面只会显示
  "模型未下载完整"，用户无从知道缺的是词典里的哪一个。
· TTS 的 fst 规则文件（date/number/phone/new_heteronym）不下载也能出声，
  但缺少会让日期与数字的读法不对（"2026"读成"二零二六"而非"二零二六年"），
  属发音质量而非可用性，故列为可选，不进必需清单。
"""
from __future__ import annotations

from .model_size import MODEL_MIN_BYTES

#: ASR：流式 zipformer 中英双语
ASR_FILES = [
    "tokens.txt",
    "encoder-epoch-99-avg-1.int8.onnx",
    "decoder-epoch-99-avg-1.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
]

#: TTS：vits-melo 中英双语
#:
#: model.onnx 为 fp32（170MB）。仓库里另有 model.int8.onnx（53.5MB），
#: 体积小得多，音质差异需另行确认，默认仍取 fp32；
#: 换用 int8 时改这里一处即可，下载与校验会同时跟着变。
TTS_FILES = [
    "model.onnx",
    "tokens.txt",
    "lexicon.txt",
    "dict/jieba.dict.utf8",
    "dict/hmm_model.utf8",
    "dict/user.dict.utf8",
    "dict/idf.utf8",
    "dict/stop_words.utf8",
]

SPEC = {
    "asr": {
        "name": "asr-zipformer-zh-en",
        "mirrors": [
            "https://huggingface.co/csukuangfj/"
            "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
            "/resolve/main",
            "https://hf-mirror.com/csukuangfj/"
            "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
            "/resolve/main",
        ],
        "files": ASR_FILES,
    },
    "tts": {
        "name": "vits-melo-tts-zh-en",
        # 官方仓库名是 vits-melo-tts-zh_en（zh 与 en 之间是下划线）；
        # 连字符写法作为备用镜像保留，两者都试，先试正确的那个。
        "mirrors": [
            "https://huggingface.co/csukuangfj/vits-melo-tts-zh_en/resolve/main",
            "https://hf-mirror.com/csukuangfj/vits-melo-tts-zh_en/resolve/main",
            "https://huggingface.co/csukuangfj/vits-melo-tts-zh-en/resolve/main",
        ],
        "files": TTS_FILES,
    },
}


#: 运行时需求：必须就位的顶层条目（文件与目录名）。
#:
#: 为什么**不**从 files 推导——推导会让需求跟着 files 一起变短：清单被误
#: 改成 ["model.onnx", "tokens.txt"] 时，推导出的需求也只剩这两项，于是
#: "下载成功"与"需求已满足"同时成立，自检恒为真，而用户拿到的是装完仍
#: 不可用的语音。需求必须独立定义、不随下载清单变化；两者由
#: uncovered_requirements() 校验覆盖关系。
REQUIRED = {
    "asr": ["tokens.txt", "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.int8.onnx"],
    "tts": ["model.onnx", "tokens.txt", "lexicon.txt", "dict"],
}


def required_entries(kind: str) -> list:
    """运行时必须就位的顶层条目。按"是否真的有内容"判定。"""
    return list(REQUIRED[kind])


def uncovered_requirements(kind: str) -> list:
    """需求条目里，下载清单未覆盖的那些。

    这是"下载清单必须覆盖需求"的校验点：files 少写一个（或被改回只有
    model.onnx 与 tokens.txt 的形态）时，这里报出缺 lexicon.txt 与 dict，
    而不是让需求跟着一起变短、自检恒真。
    """
    files = SPEC[kind]["files"]
    tops = {f.split("/")[0] for f in files}
    return [r for r in REQUIRED[kind] if r not in tops]


def is_dir_entry(kind: str, entry: str) -> bool:
    """该顶层条目是目录（清单里存在 "<entry>/..." 形式的路径）。"""
    return any(f.startswith(entry + "/") for f in SPEC[kind]["files"])


def min_bytes_for(entry: str) -> int:
    """该条目"算真的有内容"的体积下限。

    ## 为什么不能对所有条目都用同一个阈值

    阈值原本是给模型文件定的（占位/截断的 onnx 有几十 KB 也照样会崩）。
    但同一份清单里还有词表与词典这类真的小文件——vits-melo 的 tokens.txt
    只有 655 字节。一刀切套用 10KB 下限的结果是：

      · 打包内置阶段：tokens.txt 下载成功，却被判"产物过小"而拒收，
        构建直接失败（run83 的 step 19 就是这样红的）；
      · 运行时 status：文件明明在，却报 files_missing 含 tokens.txt，
        界面显示"模型未下载完整"，用户点合成失败，重试多少次都一样。

    两处的症状都指向"下载/网络有问题"，而真实原因是阈值用错了对象。
    因此下限按条目类型取：模型文件用 MODEL_MIN_BYTES，其余只要求非空。
    该判定只在这里定义一次，下载器、runtime status、打包内置三处共用，
    避免同一条判据出现两份字面量。
    """
    if entry.endswith(".onnx"):
        return MODEL_MIN_BYTES
    return 1


def kind_of_model(model_name: str) -> str:
    """模型目录名 -> 类型（asr/tts）。"""
    for k, v in SPEC.items():
        if v["name"] == model_name:
            return k
    return ""
