"""OmegaForge unified LLM client — OpenAI-compatible, zero hard deps.

Set env vars:
  OMEGAFORGE_BASE_URL   (default https://api.openai.com/v1)
  OMEGAFORGE_API_KEY
  OMEGAFORGE_MODEL_MAIN  (default gpt-4o-mini)   — synthesis / heavy reason
  OMEGAFORGE_MODEL_FAST  (default gpt-4o-mini)   — mechanical tasks
  OMEGAFORGE_MODEL_JUDGE (default gpt-4o-mini)   — arena judging

No API key? MOCK_MODE kicks in: deterministic template responses so the
entire pipeline is runnable & testable offline.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass

from ..core.errors import UserError
from .upstream_guard import open_upstream


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    mock: bool = False


MOCK_MODELS = {"mock-main", "mock-fast", "mock-judge"}


def _as_tokens(value) -> int:
    """把上游 usage 字段收敛为非负整数。

    上游可能返回 null / 字符串 / 负数：字符串会在下游
    `prompt_tokens + completion_tokens` 处被拼成 "127"（虚高 10 倍），
    负数则会倒扣用量账本。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _norm_agent_name(value) -> str:
    """把名称收敛为可直接展示的形态。

    约束：不得把驼峰压平。`"DistilledAgent".title()` 得到
    `"Distilledagent"`——`.title()` 会把词内大写字母一并降为小写，
    于是驼峰命名的 Agent 名在界面上显示为粘连的小写串。
    已经含内部大写（如 ArxivResearcher）的标识符应保留原样。

    约束：兜底值必须保持可读。名称提取不到时兜底为 "DistilledAgent"，
    它同样会经此函数，故不能依赖调用方区分来源。
    """
    # 不剥离 source_ / sample_ 前缀：剥离会把 "sample_agent" 削成
    # "Agent"，把有区分度的词直接删掉——正是名称与输入无关的来源之一。
    # 下划线转空格后完整保留，可读性与区分度同时成立。
    s = "" if value is None else str(value).replace("_", " ").strip()
    # "None" 字面量同样要挡：str(None) 得到字符串 "None"，
    # 界面上会显示成程序出错，比兜底名更糟。
    if not s or s.lower() in ("none", "null", "undefined"):
        return "DistilledAgent"
    # 含"小写紧接大写"即视为驼峰标识符，保留原拼写
    if re.search(r"[a-z][A-Z]", s):
        return s
    return s.title()


def _split_arena_answers(user: str) -> tuple[str, str]:
    """从 ARENA_PROMPT 中切出 ANSWER A / ANSWER B 两段原文。"""
    m = re.search(r"ANSWER A:\s*\n(.*?)\n\s*ANSWER B:\s*\n(.*)$",
                  user, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


def _score_answer(text: str) -> dict:
    """对一份答案提取可观测特征，产出五维 0-10 分。

    诚实声明：这是**启发式代理指标**，不是语义判断。
    它只衡量"长度/结构/可核查痕迹/信息密度"这类表面特征，
    无法判断事实对错。真实裁判由 LLM judge 完成。

    它的价值在于：让 mock 模式下不同输入产生不同分数、且胜负由
    内容决定——而不是像原有写法那样写死 A=6.2 / B=8.4 永远判 B 赢。
    """
    t = (text or "").strip()
    n = len(t)
    lines = [x for x in t.splitlines() if x.strip()]
    n_lines = max(1, len(lines))

    # 1) 完成度：实质长度 + 是否给出结论
    has_conclusion = bool(re.search(
        r"(结论|综上|总结|总之|因此|therefore|in summary|to conclude)",
        t, re.I))
    completion = min(10.0, 2.0 + n / 200.0 + (2.0 if has_conclusion else 0.0))

    # 2) 正确性代理：可核查痕迹（链接/年份/编号引用/小数）
    cites = len(re.findall(r"(https?://|arXiv|doi[::]|\b\d{4}\b|\d+\.\d+)", t))
    correctness = min(10.0, 3.0 + cites * 1.1)

    # 3) 效率：单行平均长度过长视为啰嗦
    avg_line = n / n_lines
    efficiency = 10.0 - min(6.0, max(0.0, (avg_line - 95.0) / 22.0))
    if n == 0:
        efficiency = 0.0

    # 4) 鲁棒性：是否显式处理边界/异常/不确定性
    has_guard = bool(re.search(
        r"(如果|若|边界|异常|失败|不确定|否则|edge case|fallback|however|caveat)",
        t, re.I))
    robustness = min(10.0, 4.0 + (3.0 if has_guard else 0.0) + n / 900.0)

    # 5) 打磨度：结构化表达（分点/标题/编号）
    bullets = len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)]|#{1,6}\s)", t))
    heading = bool(re.search(r"(?m)^\s*#{1,6}\s|\b\d\.\s", t))
    polish = min(10.0, 3.0 + bullets * 0.9 + (1.5 if heading else 0.0))
    if n == 0:
        polish = 0.0

    def r5(x: float) -> float:
        return round(max(0.0, min(10.0, x)), 2)

    return {"task_completion": r5(completion),
            "correctness": r5(correctness),
            "efficiency": r5(efficiency),
            "robustness": r5(robustness),
            "polish": r5(polish)}


class LLMClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._extra_headers: dict = {}
        provider = os.getenv("OMEGAFORGE_PROVIDER", "")
        if provider == "zai":
            self._load_zai_provider()
        self.base_url = base_url or os.getenv("OMEGAFORGE_BASE_URL",
                                              "https://api.openai.com/v1")
        self.api_key = api_key or os.getenv("OMEGAFORGE_API_KEY", "")
        self.model_main = os.getenv("OMEGAFORGE_MODEL_MAIN", "gpt-4o-mini")
        self.model_fast = os.getenv("OMEGAFORGE_MODEL_FAST", "gpt-4o-mini")
        self.model_judge = os.getenv("OMEGAFORGE_MODEL_JUDGE", "gpt-4o-mini")
        # 已保存的 provider 配置（Studio 设置页写入）优先于预设默认值
        if not os.getenv("OMEGAFORGE_PROVIDER") \
                and not os.getenv("OMEGAFORGE_BASE_URL"):
            try:
                from .providers import ProviderManager
                name, cfg = ProviderManager().active()
                if name and cfg.get("base_url"):
                    self.base_url = cfg["base_url"]
                    self.api_key = cfg.get("api_key", "")
                    md = cfg.get("models") or {}
                    self.model_main = md.get("main") or self.model_main
                    self.model_fast = md.get("fast") or self.model_fast
                    self.model_judge = md.get("judge") or self.model_judge
            except Exception:                           # noqa: BLE001
                pass

    def _load_zai_provider(self) -> None:
        """从本机已有的接口配置自动读取（只读），省去手工填密钥。

        依次取接口地址、密钥、必需的自定义请求头与模型标识，全部用
        ``setdefault`` 写入环境：已在设置页手工填过的项**不会被覆盖**。

        读取失败一律静默返回——自动配置是便利功能，读不到就走正常的手工
        配置路径，不应让程序启动失败。
        """
        cfg_path = os.getenv("OMEGAFORGE_ZAI_CONFIG",
                             os.path.expanduser(
                                 "~/.openclaw-autoclaw/openclaw.json"))
        try:
            with open(cfg_path, encoding="utf-8") as f:
                raw = json.load(f)
            providers = ((raw.get("models") or {}).get("providers") or {})
            prov = providers.get("zai") if isinstance(providers, dict) else None
            if not isinstance(prov, dict):
                return
            base_url = prov.get("baseUrl")
            if not isinstance(base_url, str) or not base_url.strip():
                return
            models = prov.get("models")
            mc = models[0] if (isinstance(models, list) and models
                               and isinstance(models[0], dict)) else {}
        except Exception:                                   # noqa: BLE001
            return
        os.environ.setdefault("OMEGAFORGE_BASE_URL", base_url)
        os.environ.setdefault("OMEGAFORGE_API_KEY",
                              str(prov.get("apiKey", "") or ""))
        self._extra_headers = dict(mc.get("headers") or {})
        mid = str(mc.get("id") or "zai_auto")
        os.environ.setdefault("OMEGAFORGE_MODEL_MAIN", mid)
        os.environ.setdefault("OMEGAFORGE_MODEL_FAST", mid)
        os.environ.setdefault("OMEGAFORGE_MODEL_JUDGE", mid)
        self.base_url = os.environ["OMEGAFORGE_BASE_URL"]
        self.api_key = os.environ["OMEGAFORGE_API_KEY"]
        self.model_main = os.environ["OMEGAFORGE_MODEL_MAIN"]
        self.model_fast = os.environ["OMEGAFORGE_MODEL_FAST"]
        self.model_judge = os.environ["OMEGAFORGE_MODEL_JUDGE"]

    def configure(self, base_url: str, api_key: str,
                  models: dict | None = None) -> "LLMClient":
        """热切换 provider（ProviderManager/设置页调用）。"""
        if base_url is not None:
            self.base_url = str(base_url).rstrip("/")
        if api_key is not None:
            self.api_key = str(api_key)
        if isinstance(models, dict):
            self.model_main = str(models.get("main") or self.model_main)
            self.model_fast = str(models.get("fast") or self.model_fast)
            self.model_judge = str(models.get("judge") or self.model_judge)
        return self

    @property
    def mock_mode(self) -> bool:
        """仅当无 key 且 base_url 为默认值时才 Mock；
        Ollama / LM Studio / vLLM 等无 key 本地端点走真实调用。"""
        default_base = not self.base_url \
            or self.base_url.rstrip("/") == "https://api.openai.com/v1"
        return (not self.api_key) and default_base \
            and not os.getenv("OMEGAFORGE_ALLOW_KEYLESS")

    def chat(self, system: str, user: str, model: str | None = None,
             temperature: float = 0.4, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        return self._complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model=model, temperature=temperature,
            max_tokens=max_tokens, json_mode=json_mode)

    def chat_messages(self, messages: list, model: str | None = None,
                      temperature: float = 0.4, max_tokens: int = 2048,
                      json_mode: bool = False) -> LLMResponse:
        """多轮消息通道：允许 assistant / tool 轮次参与上下文。

        为什么必须另开一条而不是继续用 chat()
        -----------------------------------
        chat() 只收 (system, user) 单轮。工具循环要把"模型的上一步输出"
        和"工具返回"一起送回去，否则模型看不到自己说过什么、也看不到
        原始任务——例如 SuperAgent 在工具调用后第二轮只收到工具输出，
        任务上下文完全丢失，产出必然离题。

        不改 chat() 的签名而是新增方法：既有调用方（蒸馏各阶段、Arena、
        对话）全部不动，改动面最小。
        """
        if not isinstance(messages, list) or not messages:
            raise UserError("对话内容为空，无法向模型发起请求")
        return self._complete(list(messages), model=model,
                              temperature=temperature,
                              max_tokens=max_tokens, json_mode=json_mode)

    def _complete(self, messages: list, model: str | None = None,
                  temperature: float = 0.4, max_tokens: int = 2048,
                  json_mode: bool = False) -> LLMResponse:
        if self.mock_mode:
            # mock 是纯离线变换，只认 system + 最后一条 user；
            # 多轮场景下取最近一条 user，保持与单轮一致的可验证性。
            sys_txt = next((str(m.get("content", ""))
                            for m in messages if m.get("role") == "system"), "")
            user_txt = next((str(m.get("content", ""))
                             for m in reversed(messages)
                             if m.get("role") == "user"), "")
            return self._mock(sys_txt, user_txt, model or self.model_main)
        payload: dict = {
            "model": model or self.model_main,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}",
                   **self._extra_headers}
        if self._extra_headers.get("X-Request-Model"):
            headers["X-Request-Model"] = payload["model"]
        data = None
        for http_attempt in range(2):
            req = urllib.request.Request(
                f"{self.base_url.rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode(), headers=headers)
            try:
                # 走检查通道：地址过云元数据/链路本地黑名单、跳转目标复查、
                # 响应体封顶。直接 urlopen 的话，base_url 指向 169.254.169.254
                # 会把云上身份凭据原样取回（），且上游回 30MB
                # 内容可吃掉 94MB 内存。
                raw = open_upstream(req, timeout=90)
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    raise UserError(
                        "模型服务返回了无法解析的内容，请检查接口地址是否正确")
                if not isinstance(data, dict):
                    raise UserError(
                        "模型服务返回了无法解析的内容，请检查接口地址是否正确")
                break
            except urllib.error.HTTPError as e:
                # response_format 不被支持 → 裸重试；429/5xx → 退避重试
                if e.code == 400 and json_mode and "response_format" in payload:
                    payload.pop("response_format", None)
                    continue
                if e.code in (429, 500, 502, 503, 504) and http_attempt < 1:
                    time.sleep(2)
                    continue
                raise
        if data is None:
            raise RuntimeError("LLM call failed after retries")
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise UserError("模型服务没有返回任何内容，请稍后重试或更换模型")
        first = choices[0]
        msg = first.get("message") if isinstance(first, dict) else None
        if not isinstance(msg, dict):
            raise UserError("模型服务返回的内容格式有误，请稍后重试或更换模型")
        text = msg.get("content") or ""
        if not text.strip() and msg.get("reasoning_content"):
            text = str(msg["reasoning_content"])[-4000:]
        return LLMResponse(
            text=text,
            prompt_tokens=_as_tokens(usage.get("prompt_tokens")),
            completion_tokens=_as_tokens(usage.get("completion_tokens")),
            model=payload["model"])

    # ------------------------------------------------------------------
    def _mock(self, system: str, user: str, model: str) -> LLMResponse:
        """Deterministic offline response generator.

        v2: a REFERENCE TRANSFORM, not canned text — EXTRACT/COMPRESS/
        SYNTHESIZE derive their output from the JSON 请求体 embedded in
        the 提示词, so different sources produce different genomes and
        提示词s. Hash-stable; lets the whole pipeline be exercised
        offline with source-faithful data flow. Live LLM adds semantic
        fidelity on top of this.
        """
        ptok, ctok = len(system) // 4 + 10, 0

        def resp(text: str) -> LLMResponse:
            nonlocal ctok
            ctok = len(text) // 4 + 5
            return LLMResponse(text, ptok, ctok, model, mock=True)

        def payload_after(marker: str) -> dict:
            """Parse the JSON block embedded after a 提示词 marker."""
            i = user.find(marker)
            if i < 0:
                return {}
            s, e = user.find("{", i), user.rfind("}")
            if s < 0 or e <= s:
                return {}
            try:
                return json.loads(user[s:e + 1])
            except json.JSONDecodeError:
                return {}

        # ------------------------------------------------ EXTRACT
        if "SPEC:" not in user and "SIGNALS:" in user:  # EXTRACT 结构标记
            sig = payload_after("SIGNALS:")
            if not sig:
                return resp(json.dumps({
                    "name": "MockSourceAgent", "role": "research assistant",
                    "mission": "research topics and produce briefs",
                    "persona": "rigorous, cites sources, concise",
                    "tools": [{"name": "web_search",
                               "description": "search the web",
                               "params": {"query": "str"}}],
                    "workflow": [{"id": "s1", "actor": "worker",
                                  "action": "search", "input": "query",
                                  "output": "results"},
                                 {"id": "s2", "actor": "worker",
                                  "action": "synthesize brief",
                                  "input": "results", "output": "brief"}],
                    "io": {"input": "topic str", "output": "markdown brief"},
                    "failure_modes": ["stale sources", "overlong answers"],
                    "quality_bars": ["every claim sourced", "<800 words"]},
                    ensure_ascii=False))
            name = sig.get("name_hint") or "DistilledAgent"
            name = _norm_agent_name(name)
            roles = sig.get("role_hints") or ["assistant"]
            prompts = sig.get("prompt_candidates") or [""]
            persona_txt = max(prompts, key=len)[:600]
            tool_names: list = []
            for cand in sig.get("tool_candidates") or []:
                cstr = str(cand)
                for m in re.finditer(r"([a-zA-Z_][\w_]*)\s*\(([^)]*)\)", cstr):
                    tool_names.append({"name": m.group(1),
                                       "description": f"{m.group(1)} tool",
                                       "params": {}})
                # pipe format: name | params | description
                head = cstr.split("|")[0].strip().strip("\"'")
                if (re.fullmatch(r"[a-zA-Z_][\w_]{2,40}", head)
                        and head not in {t["name"] for t in tool_names}):
                    tool_names.append({"name": head,
                                       "description": cstr[:80],
                                       "params": {}})
                for m in re.finditer(r"[\"']([a-z_][\w_]{2,30})[\"']", cstr):
                    if m.group(1) not in {t["name"] for t in tool_names}:
                        tool_names.append({"name": m.group(1),
                                           "description": f"{m.group(1)} tool",
                                           "params": {}})
            cues = sig.get("workflow_cues") or ["plan", "execute", "report"]
            workflow = [{"id": f"s{i+1}", "actor": "worker",
                         "action": str(c), "input": "context",
                         "output": "artifact"}
                        for i, c in enumerate(cues[:6])]
            m0 = re.split(r"[.\n]", persona_txt)[0] if persona_txt else ""
            # 冠词必须后接空白才算冠词：写成 `(an?|the)?\s*` 时，
            # "You are ArxivScholar" 里的 A 会被当成冠词吃掉，使命变成
            # "rxivScholar"——名字首字被截，而它随后会进到基因组、评测题
            # 与对照报告里，用户看到的是一个被削掉开头的陌生词。
            m0 = re.sub(r"^\s*You are\s+(?:(?:an?|the)\s+)?", "", m0,
                        flags=re.I).strip()
            # 首句若只剩一个名字（"You are HomeChef." → "HomeChef"），
            # 把它当使命会让评测题变成"典型任务：HomeChef"——
            # 没有动作就没有可判分的东西。此时退到整段描述取使命。
            if " " not in m0:
                m0 = " ".join(re.split(r"[.\n]", persona_txt)[:2])
                m0 = re.sub(r"^\s*You are\s+(?:(?:an?|the)\s+)?", "", m0,
                            flags=re.I).strip()
            mission = m0[:140] or f"perform {roles[0]} duties"
            return resp(json.dumps({
                "name": name, "role": roles[0],
                "mission": mission,
                "persona": persona_txt or ", ".join(roles),
                "tools": tool_names or [{"name": "web_search",
                                          "description": "search the web",
                                          "params": {"query": "str"}}],
                "workflow": workflow,
                "io": {"input": "task str", "output": "structured result"},
                "failure_modes": ["skipped verification", "verbose drift"],
                "quality_bars": ["ground every claim", "stay within scope"]},
                ensure_ascii=False))
        # ------------------------------------------------ COMPRESS
        if "SPEC:" in user:  # COMPRESS 结构标记
            spec = payload_after("SPEC:")
            if not spec:
                return resp(json.dumps({
                    "name": "MockSourceAgent", "mission_one_liner":
                        "research + sourced briefs",
                    "persona_genes": ["rigorous", "cites sources", "concise"],
                    "tool_genes": ["web_search(query)"],
                    "workflow_genes": ["search -> verify -> brief"],
                    "upgrade_genes": [], "est_system_tokens": 320},
                    ensure_ascii=False))
            persona = str(spec.get("persona", ""))
            STOP = {"you", "are", "the", "every", "treat", "treats",
                    "your", "with", "that", "this", "from", "always",
                    "never", "without", "when", "while", "before",
                    "after", "which", "their", "them", "they", "have",
                    "been", "being", "into", "onto", "over", "under"}
            genes = [w for w in re.split(r"[^a-zA-Z]+", persona)
                     if len(w) > 3 and w.lower() not in STOP][:6]
            if not genes:
                genes = ["focused", "pragmatic"]
            tools = spec.get("tools") or []
            tool_genes = []
            for t in tools:
                if isinstance(t, dict) and t.get("name"):
                    params = ",".join((t.get("params") or {}).keys()) or "ctx"
                    tool_genes.append(f"{t['name']}({params})")
                elif isinstance(t, str):
                    tool_genes.append(t if "(" in t else f"{t}(ctx)")
            wf = [str(step.get("action", step)) if isinstance(step, dict)
                  else str(step) for step in (spec.get("workflow") or [])]
            upgrade = [f"enforce: {qb}" for qb in
                       (spec.get("quality_bars") or [])[:3]]
            upgrade.append("self-check output completeness before replying")
            mission = str(spec.get("mission") or spec.get("role") or "assistant duties")
            return resp(json.dumps({
                "name": spec.get("name", "DistilledAgent"),
                "mission_one_liner": mission[:120],
                "persona_genes": genes,
                "tool_genes": tool_genes or ["web_search(query)"],
                "workflow_genes": wf or ["plan", "execute", "report"],
                "upgrade_genes": upgrade,
                "est_system_tokens": 60 + 12 * len(genes) + 20 * len(tool_genes)},
                ensure_ascii=False))
        # ------------------------------------------------ SYNTHESIZE
        if "GENOME:" in user:  # SYNTHESIZE 结构标记
            g = payload_after("GENOME:")
            if not g:
                return resp(json.dumps({
                    "system_prompt": "You are a rigorous research agent. "
                                     "Search, verify, cite, answer under 800 words.",
                    "tools": ["web_search"], "workflow": ["search", "verify",
                                                          "brief"],
                    "design_notes": "compressed persona, gated verbosity"},
                    ensure_ascii=False))
            mission = g.get("mission_one_liner", "assist the user")
            persona = ", ".join(g.get("persona_genes") or []) or "focused"
            steps = " -> ".join(g.get("workflow_genes") or ["plan", "execute"])
            upgrades = "\n".join(f"- {x}" for x in (g.get("upgrade_genes") or [])
                                 if isinstance(x, str))
            prompt = (
                f"You are {g.get('name', 'an agent')}, a distilled specialist. "
                f"MISSION: {mission}.\n"
                f"OPERATING TRAITS: {persona}.\n"
                f"WORKFLOW: {steps}.\n"
                f"NON-NEGOTIABLE QUALITY BARS:\n{upgrades or '- verify before output'}\n"
                "Be dense: every sentence either enables a capability or "
                "prevents a failure mode. No filler.")
            return resp(json.dumps({
                "system_prompt": prompt,
                "tools": g.get("tool_genes", []),
                "workflow": g.get("workflow_genes", []),
                "design_notes": "deterministic reference compile (mock)"},
                ensure_ascii=False))
        if "MISSION:" in user:  # GEN_EVAL 结构标记
            # 出题必须由使命决定：考题若与使命无关，不论蒸馏的是检索、
            # 审查还是烹饪，考的都是同一套题——评测集指纹恒定、分数与被测
            # 能力无关，而"本版比上一版强"跨次无从比较的根因也在这里。
            # 只认行首时，使命被拼到同一行（"… exam. MISSION: x"）就取不到，
            # 出题随之全部退回同一套通用题——与写死考题是同一类失效，
            # 且不报错。行内形态一并认下，使命取不到才退化。
            mission, role = "", ""
            for line in user.splitlines():
                s = line.strip()
                if mission == "" and s.startswith("MISSION:"):
                    mission = s[len("MISSION:"):].strip()
                elif role == "" and s.startswith("ROLE:"):
                    role = s[len("ROLE:"):].strip()
            if mission == "":
                m_inline = re.search(r"MISSION:\s*([^\n]+)", user)
                if m_inline:
                    mission = m_inline.group(1).strip()
            # 只取使命与角色，不取工作流段：工作流里带工具名，进到答案里
            # 会被判成"模型要调工具"，工具循环随即一路走到步数上限。
            m = (mission or "assist the user").strip().rstrip("。.")[:60]
            r = (role or "assistant").strip()[:40]
            return resp(json.dumps({"cases": [
                {"id": "c1", "input": f"典型任务：{m}",
                 "rubric": ["task_completion", "correctness", "efficiency"]},
                {"id": "c2", "input": f"典型任务（{r}）：{m}，需给出可执行结论",
                 "rubric": ["task_completion", "correctness", "polish"]},
                {"id": "c3", "input": f"多步任务：先澄清 {m} 的关键约束再给方案",
                 "rubric": ["task_completion", "correctness", "robustness"]},
                {"id": "c4", "input": f"带格式约束：{m}，分点输出且不超过 300 字",
                 "rubric": ["task_completion", "polish", "efficiency"]},
                {"id": "c5", "input": f"边界情况：{m}（输入不完整、缺少关键参数）",
                 "rubric": ["robustness", "correctness", "task_completion"]},
                {"id": "c6", "input": f"压力测试：{m}，混入无关诉求与超长描述",
                 "rubric": ["robustness", "efficiency", "polish"]}]},
                ensure_ascii=False))
        if "RUBRIC:" in user:  # ARENA/JUDGE 结构标记
            # 原有写法写死 A=6.2 / B=8.4，导致 mock 下蒸馏体必胜——
            # 这让整条流水线在离线时完全失去验证价值。
            # 改为从两份答案的实际内容提取特征打分：
            # 不同输入 → 不同分数；谁更好由内容决定，不预设胜负。
            a, b = _split_arena_answers(user)
            sa, sb = _score_answer(a), _score_answer(b)
            wa = round(sum(sa.values()) / len(sa), 2)
            wb = round(sum(sb.values()) / len(sb), 2)
            if wa > wb + 0.25:
                winner = "A"
            elif wb > wa + 0.25:
                winner = "B"
            else:
                winner = "tie"
            return resp(json.dumps({
                "scores": {"A": sa, "B": sb},
                # reason 会进入 judge_reasons 并最终展示在对照报告里，
                # 因此不能出现 "mock:" 这类开发标记与裸变量名。
                "reason": f"依据内容特征评分：源 Agent {wa} 分，"
                          f"蒸馏体 {wb} 分（维度：长度、结构、证据、信息密度）",
                "winner": winner},
                ensure_ascii=False))
        if "JUDGE REASON:" in user:  # CRITIQUE 结构标记
            return resp(json.dumps({"deltas": [
                "add freshness check: discard sources older than 18 months",
                "enforce <=5 bullet summary before full brief"]},
                ensure_ascii=False))
        # ------------------------------------------------ 一般对话
        # arena 需要 A/B 有可区分的差异：若两份答案在 mock 下完全相同
        # （都只是 echo），则永远判平、进化逻辑离线无法验证。
        # 因此让回答质量随 系统提示词 质量单调变化——
        # 仍是"参考变换"（输入决定输出），不是固定文本。
        return resp(_mock_completion(system, user))


def _mock_completion(system: str, user: str) -> str:
    """按 系统提示词 的质量梯度生成不同完整度的回答。

    质量信号：长度、是否含质量条/工作流/输出约束。
    输出随之在"一句话"与"结构化简报"之间变化。
    """
    s = (system or "").strip()
    u = (user or "").strip()
    n = len(s)

    has_bar = bool(re.search(
        r"(must|never|always|verify|quality bar|non-negotiable"
        r"|必须|不得|务必|验证|质量)", s, re.I))
    has_flow = bool(re.search(
        r"(->|step\s*\d|first|then|finally|workflow|步骤|流程|首先|然后)", s, re.I))
    has_format = bool(re.search(
        r"(markdown|json|bullet|section|\d+\s*words|字数|格式|分点)", s, re.I))
    has_role = bool(re.search(r"(you are|act as|你是|你是一个)", s, re.I))

    score = sum([n >= 400, has_bar, has_flow, has_format, has_role])

    if score <= 1:
        return (f"{u[:60]}。简短答复：已完成。" if u else "已完成。")

    head = f"针对「{u[:50]}」的分析：\n\n" if u else ""
    if score <= 3:
        return (head + "- 要点一：核心结论\n- 要点二：主要依据\n\n"
                f"结论：{u[:30] or '任务'}已处理。")

    # 约束：高分段必须有梯度，答案要随 系统提示词 的实际内容变化。
    #
    # 若高分段一律返回同一份固定简报，不同来源的提示词会得到逐字相同的
    # 答案，经打分后 final_score 恒定。报告里最显眼的那个数字因此与输入
    # 无关——用户会把它当成蒸馏质量的度量，而它不度量任何东西；竞技场
    # "更强"的结论、自进化是否改善，在离线时也都因此失去依据。
    #
    # 差异载体只能是**不含工具名的纯数字**：
    #
    # 下游按文本判断有没有工具请求。往答案里塞流程段原文会带进
    # search / report / workflow 这类词，于是每轮都被判定"有工具要调"，
    # 一路走到步数上限。改一个生成模板却让另一处的解析失效，症状指向
    # 解析方而非模板，排查方向会被带偏。
    #
    # 用年份引用承载差异则安全：它只影响"可核查痕迹"这个打分维度，
    # 不含任何可被误读为工具名的词。
    steps = [x.strip() for x in re.split(r"->|→", s) if x.strip()]
    if len(steps) < 2:
        steps = [x.strip() for x in re.findall(
            r"(?:step\s*\d+|首先|然后|接着|最后)[:：]?\s*([^，。;\n]{2,40})",
            s, re.I)]
    bars = re.findall(r"(must|never|always|必须|不得|务必|验证)", s, re.I)

    # 引用条数 = 流程段数 + 质量约束数，纯数字，随 系统提示词 单调变化
    n_ref = len(steps[:6]) + len(bars[:4])
    cites = "、".join(str(2019 + i) for i in range(n_ref))

    item2 = "主要依据，见 arXiv:2401.00001"
    if cites:
        item2 += f"（参考年份：{cites}）"

    body = ("- 要点一：核心结论，基于可核查来源\n"
            f"- 要点二：{item2}\n"
            "- 要点三：边界情况与风险提示\n")
    return (
        head
        + "## 分析\n"
        + body
        + "\n## 结论\n"
        + f"综上，{u[:30] or '该任务'}已达成；若输入不完整则按降级路径处理。\n\n"
        + "## 来源\n- https://arxiv.org/abs/2401.00001 (2024)\n"
    )
