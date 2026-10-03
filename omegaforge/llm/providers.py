"""OmegaForge Provider Manager — 多模型统一接入。

预设覆盖：OpenAI / 智谱 / DeepSeek / Moonshot / OpenRouter / SiliconFlow /
Ollama（本地）/ LM Studio（本地）/ 自定义 OpenAI 兼容端点。

本地端点自动探测（Ollama 11434 / LM Studio 1234），key 缺省为空。
配置持久化 OMEGAFORGE_HOME/providers.json；运行时经 LLMClient.configure 热切换。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Optional
from ..core.errors import user_error
from ..core.atomicio import atomic_write_json, file_lock
from ..core.paths import LazyHome, resolve_home
from .upstream_guard import open_upstream

PRESETS: list[dict] = [
    {"name": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": {"main": "gpt-4o-mini", "fast": "gpt-4o-mini",
                "judge": "gpt-4o-mini"}, "needs_key": True, "local": False,
     "model_catalog": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
                       "o3-mini"]},
    {"name": "zhipu", "label": "智谱 BigModel", "base_url":
     "https://open.bigmodel.cn/api/paas/v4",
     "models": {"main": "glm-4.5-flash", "fast": "glm-4.5-flash",
                "judge": "glm-4.5-flash"}, "needs_key": True, "local": False,
     "model_catalog": ["glm-4.5-flash", "glm-4.5", "glm-4.5-air",
                       "glm-4.6", "glm-4-plus", "glm-4-flash",
                       "glm-4-long"]},
    {"name": "deepseek", "label": "DeepSeek 深度求索", "base_url":
     "https://api.deepseek.com/v1",
     "models": {"main": "deepseek-chat", "fast": "deepseek-chat",
                "judge": "deepseek-chat"}, "needs_key": True, "local": False,
     "model_catalog": ["deepseek-chat", "deepseek-reasoner"]},
    {"name": "moonshot", "label": "月之暗面 Kimi", "base_url":
     "https://api.moonshot.cn/v1",
     "models": {"main": "kimi-latest", "fast": "moonshot-v1-8k",
                "judge": "kimi-latest"}, "needs_key": True, "local": False,
     "model_catalog": ["kimi-latest", "kimi-k2-0905-preview",
                       "moonshot-v1-8k", "moonshot-v1-32k",
                       "moonshot-v1-128k"]},
    {"name": "doubao", "label": "字节豆包·火山方舟", "base_url":
     "https://ark.cn-beijing.volces.com/api/v3",
     "models": {"main": "doubao-pro-32k", "fast": "doubao-lite-32k",
                "judge": "doubao-pro-32k"}, "needs_key": True, "local": False,
     "model_catalog": ["doubao-pro-32k", "doubao-pro-128k",
                       "doubao-lite-32k"]},
    {"name": "qwen", "label": "阿里通义千问·百炼", "base_url":
     "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": {"main": "qwen-plus", "fast": "qwen-turbo",
                "judge": "qwen-max"}, "needs_key": True, "local": False,
     "model_catalog": ["qwen-max", "qwen-plus", "qwen-turbo",
                       "qwen-long", "qwen2.5-72b-instruct"]},
    {"name": "hunyuan", "label": "腾讯混元", "base_url":
     "https://api.hunyuan.cloud.tencent.com/v1",
     "models": {"main": "hunyuan-turbo", "fast": "hunyuan-lite",
                "judge": "hunyuan-pro"}, "needs_key": True, "local": False,
     "model_catalog": ["hunyuan-turbo", "hunyuan-pro", "hunyuan-standard",
                       "hunyuan-lite"]},
    {"name": "qianfan", "label": "百度千帆·文心", "base_url":
     "https://qianfan.baidubce.com/v2",
     "models": {"main": "ernie-4.0-turbo-8k", "fast": "ernie-3.5-8k",
                "judge": "ernie-4.0-turbo-8k"}, "needs_key": True,
     "local": False,
     "model_catalog": ["ernie-4.0-turbo-8k", "ernie-4.0-8k",
                       "ernie-3.5-8k", "ernie-speed-128k"]},
    {"name": "minimax", "label": "MiniMax 稀宇", "base_url":
     "https://api.minimax.chat/v1",
     "models": {"main": "MiniMax-Text-01", "fast": "abab6.5s-chat",
                "judge": "MiniMax-Text-01"}, "needs_key": True,
     "local": False,
     "model_catalog": ["MiniMax-Text-01", "abab6.5s-chat"]},
    {"name": "spark", "label": "讯飞星火", "base_url":
     "https://spark-api-open.xf-yun.com/v1",
     "models": {"main": "generalv3.5", "fast": "lite",
                "judge": "generalv3.5"}, "needs_key": True, "local": False,
     "model_catalog": ["generalv3.5", "generalv3", "lite"]},
    {"name": "openrouter", "label": "OpenRouter", "base_url":
     "https://openrouter.ai/api/v1",
     "models": {"main": "openai/gpt-4o-mini", "fast": "openai/gpt-4o-mini",
                "judge": "openai/gpt-4o-mini"}, "needs_key": True,
     "local": False,
     "model_catalog": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet",
                       "deepseek/deepseek-chat", "z-ai/glm-4.5"]},
    {"name": "siliconflow", "label": "SiliconFlow 硅基流动", "base_url":
     "https://api.siliconflow.cn/v1",
     "models": {"main": "Qwen/Qwen2.5-7B-Instruct",
                "fast": "Qwen/Qwen2.5-7B-Instruct",
                "judge": "Qwen/Qwen2.5-7B-Instruct"},
     "needs_key": True, "local": False,
     "model_catalog": ["Qwen/Qwen2.5-72B-Instruct",
                       "Qwen/Qwen2.5-7B-Instruct",
                       "deepseek-ai/DeepSeek-V3", "THUDM/glm-4-9b-chat"]},
    {"name": "ollama", "label": "Ollama（本地）", "base_url":
     "http://127.0.0.1:11434/v1",
     "models": {"main": "qwen2.5:7b", "fast": "qwen2.5:7b",
                "judge": "qwen2.5:7b"}, "needs_key": False, "local": True,
     "model_catalog": []},
    {"name": "lmstudio", "label": "LM Studio（本地）", "base_url":
     "http://127.0.0.1:1234/v1",
     "models": {"main": "local-model", "fast": "local-model",
                "judge": "local-model"}, "needs_key": False, "local": True,
     "model_catalog": []},
    {"name": "custom", "label": "自定义 OpenAI 兼容", "base_url": "",
     "models": {"main": "", "fast": "", "judge": ""},
     "needs_key": True, "local": False, "model_catalog": []},
]

PRESET_MAP = {p["name"]: p for p in PRESETS}


def _home() -> str:
    # 唯一真源在 core/paths.py；这里保留薄封装以兼容既有调用点。
    return resolve_home()


class ProviderManager(LazyHome):
    """读取/保存/应用 provider 配置。"""

    def __init__(self, home: Optional[str] = None):
        # 无内存缓存（_load 每次读盘），只需路径惰性
        super().__init__(home)

    @property
    def path(self) -> str:
        return os.path.join(self.home, "providers.json")

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self, active: str, config: dict) -> None:
        os.makedirs(self.home, exist_ok=True)
        # 读-改-写：锁内重新读盘，否则两个写入者各自基于旧快照整体覆盖，
        # 先写的那份配置会静默消失（与 kb/tasks 同一类失效）。
        with file_lock(self.path):
            data = self._load()
            data["active"] = active
            data.setdefault("configs", {})[active] = config
            data["updated_ts"] = time.time()
            atomic_write_json(self.path, data)

    def active(self) -> tuple[Optional[str], dict]:
        data = self._load()
        name = data.get("active")
        cfg = (data.get("configs") or {}).get(name, {}) if name else {}
        return name, cfg

    @staticmethod
    def probe(base_url: str, api_key: str = "") -> dict:
        """探测端点连通性（GET /models），返回 {online, models?, latency_ms}。"""
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=headers)
            # 与 client._complete 同一条检查通道：探测接口同样是"服务端按用户
            # 给的地址发请求"，不检查就是 SSRF（例如 169.254.169.254 未被拦）。
            body = json.loads(open_upstream(req, timeout=4))
            if not isinstance(body, dict):
                return {"online": False,
                        "latency_ms": int((time.time() - t0) * 1000),
                        "error": "端点返回的内容不是合法的模型列表"}
            # 列表里混入非字典条目会让整次探测挂掉（AttributeError→线上故障），
            # 而那只是上游返回得不够规范。
            names = [m.get("id") for m in (body.get("data") or [])
                     if isinstance(m, dict)][:12]
            return {"online": True,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "models": names}
        except Exception as e:                          # noqa: BLE001
            # 探测结果会被 /api/providers/test 整个返回给前端，
            # 异常原文（含 URL、可能的 key、英文类名）因此会直接上屏。
            # 这里只留可展示的中文短句，完整异常进内部日志。
            return {"online": False,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": user_error(e, "providers.probe")}

    def status_all(self) -> list[dict]:
        """预设全表 + 本地端点在线探测 + 当前激活态。"""
        active, _cfg = self.active()
        out = []
        for p in PRESETS:
            item = dict(p)
            item["active"] = (p["name"] == active)
            if p["local"]:
                probe = self.probe(p["base_url"])
                item["online"] = probe["online"]
                item["detected_models"] = probe.get("models") or []
                item["latency_ms"] = probe.get("latency_ms")
            out.append(item)
        return out
