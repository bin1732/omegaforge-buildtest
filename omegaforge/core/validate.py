"""validate — API 入参统一校验层。

为什么需要这一层：

    1. 类型非法一律 500
       例如：POST /api/distill 传 budget=[1] → 500「操作失败，请稍后重试」；
       /api/tasks/add 传 priority=[1]、/api/voice/tts 传 speed=[1]、
       /api/providers/apply 传 models="x"、/api/providers/test 传 models=[1]
       全部 500。用户只是传错了格式，却被告知"服务器故障"，
       会反复重试；监控也会把它当成线上事故。

    2. 数值无下界 → "假成功"
       例如：budget=0 / budget=-5 / rounds=0 / rounds=-3 / gens=0 全部 200
       放行并返回 job id。这类任务要么零预算立刻空转、要么零轮评测，
       最终产物必然是空壳，但界面上仍显示"已完成"，用户查不出原因。

    3. 必填校验不告诉用户缺哪个字段
       例如：POST /api/kb/add 只传 title → 「请填写完整的必填项」。
       界面上有两个输入框，用户不知道到底哪个没填。

    4. 中英文长度判定用同一把尺子
       例如：26 字的完整中文指令被判"too short"拒绝，而英文要 47 字符
       才通过。中文表达密度约为英文两倍，等长门槛等于系统性拒绝中文。

本模块提供：类型安全转换（非法类型 → UserError/400，而非 500）、
范围校验、必填校验（指出字段名）、中英文混合的信息量权重。
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

from .errors import UserError

#: 信息量统计的提前退出上限：权重只用于"够不够格"的布尔判断，
#: 超过上限后结论不可能翻转，没必要继续扫完整份文本。
_WEIGHT_CAP = 100_000

# CJK 统一表意文字 + 日文假名 + 韩文音节：这些字符每个承载约一个词的语义
#: 注意：CJK 扩展 B~G（U+20000 起）必须显式列出 —— 若不列出，
#: 生僻字与部分繁体 Ext-B 字会被当成"5 个西文字符才计 1"，
#: 中文权重系统性低估 5 倍，正是本函数要解决的问题本身。
_CJK = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿"
    r"가-힯"
    r"𠀀-𪛟𪜀-𮯯"
    r"丽-𯨟𰀀-𱍏]"
)


def text_weight(text: str) -> float:
    """中英文混合文本的"信息量"权重。

    设计依据：中文一个字的信息密度约等于英文一个词，而一个英文词平均
    5 个字符。若统一按字符数卡门槛，中文会被系统性低估约 5 倍。

    · CJK 字符：每个计 1
    · 其余非空白字符：每 5 个计 1（约等于一个英文词）

    对照（len=字符数）：
      "你是一位资深代码审查专家，请检查以下代码并指出问题。"  len=26 → 26.0
      "Summarize the article and list three key risks."        len=47 →  8.2
    """
    t = text or ""
    # 例如：findall 会把每个匹配物化成字符串再取 len —— 200 万个汉字
    # 峰值 161MB。改成 finditer 逐个计数，峰值降回常数级；
    # 另一个列表推导同样会物化整份文本，一并改成循环计数。
    cjk = 0
    for _m in _CJK.finditer(t):
        cjk += 1
        if cjk >= _WEIGHT_CAP:
            break
    other = 0
    for ch in t:
        if ch.isspace():
            continue
        if _CJK.match(ch):
            continue
        other += 1
        if other >= _WEIGHT_CAP * 5:
            break
    return cjk + other / 5.0


def looks_enough(text: str, *, min_weight: float = 10.0) -> bool:
    """判断一段文本是否具备作为提示词的最低信息量。

    只做增量放宽，不收紧原有英文判定：调用方仍先按 len>40 放行，
    本函数用于"字符数不足 40 但中文信息量足够"的情形。
    """
    return text_weight(text) >= min_weight


# ────────────────────────── 类型安全转换 ──────────────────────────
# 原则：任何来自 请求体 的值都不可信。转换失败必须变成 400 的 UserError，
# 绝不让它以 TypeError/AttributeError 的形式穿透成 500。


def _label(key: str, label: str | None) -> str:
    return label or key


def as_int(payload: Mapping[str, Any], key: str, default: int,
           minimum: int | None = None, maximum: int | None = None,
           label: str | None = None) -> int:
    """取整数：类型非法/超范围 → UserError（400），而非 500。

    刻意不接 list/dict/None：这些没有合理的整数语义，
    静默兜底成 default 会让使用者以为参数生效了，实际并未生效。
    """
    if key not in payload or payload[key] is None:
        return default
    raw = payload[key]
    name = _label(key, label)
    if isinstance(raw, bool):            # bool 是 int 子类，但语义上不是数值
        raise UserError(f"{name}需要填写数字")
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        # 必须先判有限性：Python 的 json.loads 默认接受 NaN / Infinity
        # （parse_constant 不拦），而 int(nan) 抛 ValueError、
        # int(inf) 抛 OverflowError——两者都会穿透成 500。
        if not math.isfinite(raw):
            raise UserError(f"{name}需要填写有效数字")
        if raw != int(raw):
            raise UserError(f"{name}需要填写整数")
        val = int(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return default
        try:
            val = int(s)
        except ValueError:
            raise UserError(f"{name}需要填写数字，当前填写的不是有效数值")
        # 这里不需要有限性检查：int() 的结果恒为有限值，非有限值只会从
        # 浮点分支进来，那边已有对应判断。
        #
        # 反过来，对超大整数做有限性判断会先转 float，超出 float 量程时抛
        # OverflowError——中间长度的超长数字串会被当成内部错误返回，而更长
        # 的反而被解释器自身的位数限制挡住，形成"越长越安全、中间段反而
        # 失败"的错位。范围约束由下面的 minimum/maximum 承担，与位数无关。
    else:
        raise UserError(f"{name}需要填写数字，当前格式无法识别")
    if minimum is not None and val < minimum:
        raise UserError(f"{name}不能小于 {minimum}")
    if maximum is not None and val > maximum:
        raise UserError(f"{name}不能大于 {maximum}")
    return val


def as_float(payload: Mapping[str, Any], key: str, default: float,
             minimum: float | None = None, maximum: float | None = None,
             label: str | None = None) -> float:
    """取浮点数：类型非法/超范围 → UserError（400）。"""
    if key not in payload or payload[key] is None:
        return default
    raw = payload[key]
    name = _label(key, label)
    if isinstance(raw, bool):
        raise UserError(f"{name}需要填写数字")
    if isinstance(raw, (int, float)):
        # 例如：float(10**400) 抛 OverflowError（"int too large to convert to
        # float"），它既不是 ValueError 也不是 TypeError，原样穿透成 500。
        # JSON 里一个 400 位整数不带小数点就会被解析成 Python int，
        # 因此这是真实可达的输入，不是理论情况。
        try:
            val = float(raw)
        except OverflowError:
            raise UserError(f"{name}数值过大，请填写一个正常范围的数值")
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return default
        try:
            val = float(s)
        except ValueError:
            raise UserError(f"{name}需要填写数字，当前填写的不是有效数值")
    else:
        raise UserError(f"{name}需要填写数字，当前格式无法识别")
    # NaN 必须与 inf 一起挡在门外。NaN 的任何比较都为 False，
    # 因此能静默通过 min/max 全部范围校验（例如 nan 原样落库），
    # 再向下游扩散：nan + 5 = nan，用量与预算会整片变成 nan 而无人察觉。
    if not math.isfinite(val):
        raise UserError(f"{name}需要填写有效数字")
    if minimum is not None and val < minimum:
        raise UserError(f"{name}不能小于 {minimum}")
    if maximum is not None and val > maximum:
        raise UserError(f"{name}不能大于 {maximum}")
    return val


def as_dict(payload: Mapping[str, Any], key: str,
            default: dict | None = None, label: str | None = None) -> dict:
    """取字典：例如 models="x" 与 models=[1] 都会触发 AttributeError→500。"""
    if key not in payload or payload[key] is None:
        return dict(default or {})
    raw = payload[key]
    name = _label(key, label)
    if not isinstance(raw, dict):
        raise UserError(f"{name}需要填写一组键值对，当前格式无法识别")
    return raw


def as_str_list(payload: Mapping[str, Any], key: str,
                default: list[str] | None = None,
                label: str | None = None) -> list[str]:
    """取字符串列表：例如 tags=123 会被存进知识库，此后检索永久 TypeError。"""
    if key not in payload or payload[key] is None:
        return list(default or [])
    raw = payload[key]
    name = _label(key, label)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        out = []
        for i, item in enumerate(raw):
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append(str(item))
            else:
                raise UserError(f"{name}第 {i + 1} 项需要填写文字")
        return out
    raise UserError(f"{name}需要填写一组文字，当前格式无法识别")


def as_text(payload: Mapping[str, Any], key: str, default: str = "",
            label: str | None = None, max_len: int | None = None) -> str:
    """取字符串：非字符串类型一律转成文字，避免 f-string 拼出对象 repr。"""
    if key not in payload or payload[key] is None:
        return default
    raw = payload[key]
    name = _label(key, label)
    if isinstance(raw, str):
        val = raw
    elif isinstance(raw, bool):
        val = "true" if raw else "false"
    elif isinstance(raw, (int, float)):
        val = str(raw)
    elif isinstance(raw, (list, tuple, dict)):
        # 例如：str([1,2]) 会把 "[1, 2]" 这种结构直接写进产物
        raise UserError(f"{name}需要填写文字，当前格式无法识别")
    else:
        raise UserError(f"{name}需要填写文字，当前格式无法识别")
    val = val.strip()
    if max_len is not None and len(val) > max_len:
        raise UserError(f"{name}内容过长，请控制在 {max_len} 字以内")
    return val


def require(payload: Mapping[str, Any], keys: Iterable[str],
            labels: dict[str, str] | None = None) -> None:
    """必填校验：明确指出缺哪个字段。

    missing 若只用于判空，提示会恒为「请填写完整的必填项」，
    界面上有多个输入框时无法定位具体缺哪一项。
    """
    labels = labels or {}

    def _blank(v) -> bool:
        # 不能用 `payload.get(k) or ""` 判空：0 与 False 是假值，
        # 例如 require({"a": 0}, ["a"]) 会报"请填写：a"——
        # 数值 0 是合法填写，把它当成未填写会拒绝正常输入。
        if v is None:
            return True
        # 空容器同样按"没填"处理：否则必填项看着是空的却能提交成功，
        # 属于"假成功"。
        # 只判空容器，不动 0 / False（它们是合法填写，见上）。
        if isinstance(v, (list, tuple, dict, set)) and not v:
            return True
        return not str(v).strip()

    missing = [labels.get(k, k) for k in keys if _blank(payload.get(k))]
    if missing:
        raise UserError("请填写：" + "、".join(missing))


def pick_first(payload: Mapping[str, Any], keys: Iterable[str],
               default: str = "") -> str:
    """按优先级取第一个非空字段。

    用于前后端字段命名不一致的兼容：若前端发送的名称与后端读取的
    名称不同，主功能在界面上会恒定失败。
    """
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        # 例如：JSON 里 `{"source_prompt": false}` 会被 str() 变成 "False" 并
        # 当成有效文本返回——于是"没有源提示词"被当成"源提示词是 False"。
        # 同理 `{"id": {"a": 1}}` 返回 "{'a': 1}" 这种结构 repr。
        # 两者都不是用户填的文字，必须当作"这一路没有值"继续找下一个键，
        # 否则兼容取值的兜底链会在第一站就被垃圾值截断。
        # 数值保留（str(123) == "123" 是合理取值，id 常以数字形式传来）。
        if isinstance(v, bool):
            continue
        if isinstance(v, (dict, list, tuple, set)):
            continue
        s = str(v).strip()
        if s:
            return s
    return default
