"""OmegaForge message layer — typed envelopes + shared blackboard.

Unlike Agency Swarm's free-text Communication Sheets, OmegaForge uses
schema-checked JSON envelopes: cheaper to route (no NLU on the bus),
impossible to silently drop fields, and trivially auditable.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Envelope:
    sender: str
    recipient: str            # role name or "BROADCAST"
    kind: str                  # task | result | question | answer | control
    subject: str
    body: Any                  # JSON-serializable 请求体
    schema_hint: dict = field(default_factory=dict)   # {"field": "type"}
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)
    reply_to: Optional[str] = None
    hop_count: int = 0

    def validate(self) -> list[str]:
        errs = []
        if not self.sender:
            errs.append("sender required")
        if not self.recipient:
            errs.append("recipient required")
        if self.kind not in {"task", "result", "question", "answer", "control"}:
            errs.append(f"unknown kind: {self.kind}")
        for f_, t in self.schema_hint.items():
            v = self.body.get(f_) if isinstance(self.body, dict) else None
            if v is None:
                errs.append(f"missing field: {f_}")
            elif t == "str" and not isinstance(v, str):
                errs.append(f"field {f_} must be str")
            elif t == "int" and not isinstance(v, int):
                errs.append(f"field {f_} must be int")
        return errs


class Blackboard:
    """Shared structured memory (v2 of 'shared state'): typed slots with
    history, replaces bloated full-transcript sharing."""

    def __init__(self):
        self._slots: dict[str, list[tuple[float, str, Any]]] = {}

    def post(self, key: str, value: Any, author: str) -> None:
        self._slots.setdefault(key, []).append((time.time(), author, value))

    def latest(self, key: str, default: Any = None) -> Any:
        hist = self._slots.get(key)
        return hist[-1][2] if hist else default

    def history(self, key: str) -> list[tuple[float, str, Any]]:
        return list(self._slots.get(key, []))

    def keys(self) -> list[str]:
        return list(self._slots.keys())


class MessageBus:
    def __init__(self):
        self._log: list[Envelope] = []
        self._subscribers: dict[str, list] = {}

    def publish(self, env: Envelope) -> list[str]:
        errs = env.validate()
        if errs:
            raise ValueError(f"invalid envelope: {errs}")
        self._log.append(env)
        for cb in self._subscribers.get(env.recipient, []):
            cb(env)
        for cb in self._subscribers.get("*", []):
            cb(env)
        return errs

    def subscribe(self, role: str, callback) -> None:
        self._subscribers.setdefault(role, []).append(callback)

    def log(self) -> list[Envelope]:
        return list(self._log)
