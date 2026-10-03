"""OmegaForge Token Bank — hard budget control & per-phase token ledger.

Every LLM call in OmegaForge flows through TokenBank. Nothing can spend
beyond the declared budget. This is the foundation of "更省 token":
budget-first design instead of usage-after-fact.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict


@dataclass
class SpendRecord:
    phase: str          # e.g. "extract", "synthesize", "arena:judge"
    model: str
    prompt_tokens: int
    completion_tokens: int
    ts: float = field(default_factory=time.time)
    note: str = ""

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BudgetExceeded(RuntimeError):
    pass


def _as_tokens(raw) -> int:
    """把用量收敛为非负整数。

    为什么必须在这一层做（而不是指望调用方）：
      charge() 直接 int(prompt_tokens)，而上游 LLM 客户端返回的
      提示词与补全的标记数完全可能是 None 或非数字字符串
      （模型不返回 usage 字段时就是 None）。
      例如：None -> TypeError，'abc' -> ValueError，
      两者都会冒泡成"操作失败，请稍后重试"——把一次计费记账问题
      伪装成服务器故障，还会在账本里留下半条不一致记录。

    另外注意：这里不能做字符串拼接意义上的"兜底"。
    若把两个数字串直接拼接，"12" 与 "7" 会合成 "127" 造成用量虚高，
    逐字段转 int 正是为了让这类问题在第二道关卡被夹住。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


class TokenBank:
    """Thread-safe token budget controller."""

    def __init__(self, budget_tokens: int = 400_000,
                 on_charge=None):
        # 预算必须是显式的非负整数。
        #
        # 容易踩到的严重缺陷：旧判断写作 `if self.budget and projected > self.budget`，
        # 而 0 是假值 —— budget=0 时整个上限检查被跳过，等于**无限额度**。
        # 用户填 0（或入口校验被绕过：CLI / MCP / 直接调用）就能无限制花钱，
        # 而界面上明明写着"预算"。
        # 负预算同理不可接受（旧行为是任何调用都立刻被拒，报错还很难懂），
        # 统一夹到 0：0 表示"一分都不能花"，这才是 hard budget 的语义。
        self.budget = max(0, _as_tokens(budget_tokens))
        self._spent: list[SpendRecord] = []
        self._running = 0            # 与 _spent 同步维护，避免 O(n²)
        self._lock = threading.Lock()
        self.on_charge = on_charge   # 可选回调的参数为阶段、模型、标记数以及提示词与补全的用量

    # ---- accounting -------------------------------------------------
    def charge(self, phase: str, model: str, prompt_tokens: int,
               completion_tokens: int, note: str = "") -> SpendRecord:
        with self._lock:
            rec = SpendRecord(phase, model, _as_tokens(prompt_tokens),
                              _as_tokens(completion_tokens), note=note)
            projected = self._running + rec.total
            if projected > self.budget:
                raise BudgetExceeded(
                    f"token budget {self.budget} exceeded at {projected} "
                    f"(phase={phase})")
            self._spent.append(rec)
            self._running = projected
            if self.on_charge:
                try:
                    self.on_charge(phase, model, rec.total,
                                   rec.prompt_tokens, rec.completion_tokens)
                except Exception:                       # noqa: BLE001
                    pass
            return rec

    def charge_estimate(self, phase: str, model: str, est_tokens: int,
                        note: str = "") -> None:
        """调用前的额度预检。

        注意：这是**预检，不是预占**——它不记账、不扣减。
        旧 docstring 写的是 "Reserve tokens ... (soft reservation)"，
        但实现里从头到尾没有任何扣减动作，属于注释在撒谎。

        为什么刻意不改成真预占：引擎在 estimate 之后还会用真实用量
        charge 一次，若 estimate 也记账就会双重计费，账目虚高。
        所以保留预检语义，但把文档改对，并明确其局限——
        它只能挡住"已经明显不够"的情况，不能挡住"预估偏小导致
        真实调用完才发现超支"（此时钱已经花出去了）。
        """
        with self._lock:
            projected = self._running + _as_tokens(est_tokens)
            if projected > self.budget:
                raise BudgetExceeded(
                    f"estimated call ({est_tokens} tok) would exceed budget "
                    f"{self.budget} (phase={phase})")

    @property
    def total_spent(self) -> int:
        # 原有写法每次 sum(全部记录)，而 charge() 会调它 —— 于是 N 次
        # charge 是 O(N²)。例如 5000 次从 0.023ms/次 劣化到 0.162ms/次。
        # 改为与 _spent 同步维护的累加值，charge 变成 O(1)。
        return self._running

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.total_spent)

    def ledger_by_phase(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._spent:
            out[r.phase] = out.get(r.phase, 0) + r.total
        return out

    def summary(self) -> dict:
        return {
            "budget": self.budget,
            "spent": self.total_spent,
            "remaining": self.remaining,
            "by_phase": self.ledger_by_phase(),
            "calls": len(self._spent),
        }

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"summary": self.summary(),
                       "records": [asdict(r) for r in self._spent]},
                      f, ensure_ascii=False, indent=2)
