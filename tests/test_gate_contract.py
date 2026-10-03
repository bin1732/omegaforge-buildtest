"""门禁契约守卫 —— 权限四级 × 风险分级 × 审批 × 幂等 × 审计闭环。

每一条都做过回退校验（把实现退回旧行为，测试必须变红），不是形式化。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omegaforge.tools import policy as P     # noqa: E402
from omegaforge.tools.system_tools import (    # noqa: E402
  SystemTools, tool_dispatch)


@pytest.fixture
def home(tmp_path, monkeypatch):
  h = str(tmp_path / "wf")
  os.makedirs(h, exist_ok=True)
  monkeypatch.setenv("OMEGAFORGE_HOME", h)
  return h


def _st(home, mode=None):
  st = SystemTools(home)
  st.perms.save({"terminal": True, "fs": True, "web_fetch": True})
  if mode:
    st.policy.set_mode(mode)
  return st


# ------------------------------------------------------------ 1. 四级矩阵
def test_matrix_confirm_asks_on_write_and_cmd(home):
  st = _st(home, "confirm")
  st.fs_list(".")            # low：放行
  with pytest.raises(P.ApprovalRequired):
    st.fs_write("a.txt", "x")     # medium：问
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo hi")     # high：问


def test_matrix_auto_edit_allows_write_asks_cmd(home):
  st = _st(home, "auto_edit")
  assert st.fs_write("a.txt", "x")["bytes"] == 1  # medium：放行
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo hi")          # high：仍问


def test_matrix_full_allows_cmd(home):
  st = _st(home, "full")
  assert st.fs_write("a.txt", "x")["bytes"] == 1
  r = st.run_command("echo hi")          # high：放行
  assert r["ok"] is True and "hi" in r["output"]


def test_matrix_plan_does_not_execute(home):
  st = _st(home, "plan")
  with pytest.raises(P.PlanRequired) as e:
    st.fs_write("never.txt", "x")
  assert e.value.plan["tool"] == "fs.write"
  # 关键：计划模式不能真的落盘
  assert not os.path.exists(os.path.join(home, "never.txt"))


# ------------------------------------------- 2. 底线：critical 任何模式都拒
@pytest.mark.parametrize("mode", ["confirm", "auto_edit", "plan", "full"])
def test_critical_denied_in_every_mode(home, mode):
  """full（完全访问）也绝不放行破坏性命令。

  把"减少确认次数"实现成"连破坏性命令也不问"，等于把可逆操作的
  交互约定套用到不可逆操作上：破坏性命令一旦执行就无法撤销，
  确认这个动作对它没有补救意义，因此只能硬拒绝。
  """
  st = _st(home, mode)
  with pytest.raises(ValueError):
    st.run_command("rm -rf /")
  with pytest.raises(ValueError):
    st.run_command(":(){ :|:& };:")


def test_path_escape_denied_in_full_mode(home):
  st = _st(home, "full")
  with pytest.raises(ValueError):
    st.fs_write("../outside.txt", "x")


@pytest.mark.parametrize("mode", ["confirm", "auto_edit", "plan", "full"])
def test_config_escape_denied_in_every_mode(home, mode):
  """通过配置注入让程序去执行另一条命令：任何模式都拒。

  这类命令不含破坏性字样，命令级危险判定管不到它；拦住它的是 critical
  清单里的配置注入模式。若这层不生效，按程序名做的静态判定会把它当成
  无害的 git 子命令放行——而它实际能让程序去执行另一条命令。
  """
  st = _st(home, mode)
  with pytest.raises(ValueError):
    st.run_command('git config core.pager "less"')


# -------------------------------------------------------- 3. 审批一次性
def test_approval_one_shot_and_replay_rejected(home):
  st = _st(home, "confirm")
  with pytest.raises(P.ApprovalRequired) as e:
    st.run_command("echo one")
  aid = e.value.approval["approval_id"]

  r = st.run_command("echo one", approval=aid)
  assert r["ok"] is True, "带凭证应放行"

  # 同一凭证第二次必须失败（重放攻击）
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo one", approval=aid)


def test_approval_does_not_escalate_mode(home):
  """审批只放行"这一次"，不能悄悄把权限级别提上去。"""
  st = _st(home, "confirm")
  with pytest.raises(P.ApprovalRequired) as e:
    st.run_command("echo x")
  st.run_command("echo x", approval=e.value.approval["approval_id"])
  assert st.policy.mode() == "confirm", "审批后权限级别不得被改写"
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo y")     # 下次仍然要问


def test_approval_forged_or_tampered_rejected(home):
  st = _st(home, "confirm")
  with pytest.raises(P.ApprovalRequired) as e:
    st.run_command("echo x")
  aid = e.value.approval["approval_id"]
  exp, nonce, sig = aid.split(".")
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo x", approval=f"{exp}.{nonce}.{'0'*24}")
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo x", approval="garbage")


def test_approval_bound_to_args(home):
  """凭证绑定参数摘要：换个命令，凭证必须失效。"""
  st = _st(home, "confirm")
  with pytest.raises(P.ApprovalRequired) as e:
    st.run_command("echo aaa")
  aid = e.value.approval["approval_id"]
  with pytest.raises(P.ApprovalRequired):
    st.run_command("echo bbb", approval=aid)


# ------------------------------------------------------------- 4. 幂等
def test_idempotency_replay_runs_once(home):
  st = _st(home, "full")
  key = "k1"
  r1 = st.run_command("echo ran >> counter.txt", idem_key=key)
  r2 = st.run_command("echo ran >> counter.txt", idem_key=key)
  assert r1.get("idempotent_replay") is not True
  assert r2.get("idempotent_replay") is True, "重放应命中缓存"
  with open(os.path.join(home, "counter.txt"), encoding="utf-8") as f:
    assert f.read().count("ran") == 1, "同键重放不得执行第二遍"


# --------------------------------------------------------- 5. 审计闭环
def test_audit_is_readable(home):
  """审计只写不读等于没审计：执行后必须能查到，且带 verdict/mode。"""
  st = _st(home, "full")
  st.fs_write("a.txt", "x")
  entries = P.audit_read(limit=10, tool="fs.write")
  assert entries, "审计必须有记录可读"
  assert entries[-1]["verdict"] == "allow"
  assert entries[-1]["mode"] == "full"


def test_audit_bad_line_does_not_500(home):
  p = os.path.join(home, "tools_audit.jsonl")
  with open(p, "a", encoding="utf-8") as f:
    f.write("{not json\n")
    f.write('{"tool":"fs.write","verdict":"allow","mode":"full"}\n')
  got = P.audit_read(limit=5)
  assert len(got) == 1 and got[0]["tool"] == "fs.write", "坏行应跳过而非整体失败"


# ------------------------------------- 6. 能力未开 vs 需要审批，必须可分
def test_capability_denied_differs_from_approval(home):
  st = SystemTools(home)        # 能力默认全关
  with pytest.raises(PermissionError): # CapabilityDenied
    st.run_command("echo x")
  st.perms.save({"terminal": True, "fs": True, "web_fetch": True})
  st.policy.set_mode("confirm")
  with pytest.raises(P.ApprovalRequired):  # 已开，但本次需确认
    st.run_command("echo x")


# ------------------------------------- 7. 门禁不得掩盖参数错误
@pytest.mark.parametrize("mode", ["confirm", "full"])
def test_param_error_not_masked_by_gate(home, mode):
  """用户把命令填成空，应得到格式提示，而不是"需要授权"。"""
  st = _st(home, mode)
  with pytest.raises(ValueError):
    st.run_command("  ")
  with pytest.raises(ValueError):
    st.web_fetch("file:///etc/passwd")


def test_timeout_dirty_value_no_500(home):
  _st(home, "full")  # 开能力 + 设模式，经 tool_dispatch 走完整链路
  for bad in (None, "abc", [1], 3.7):
    r = tool_dispatch("run_command", {"cmd": "echo ok", "timeout": bad},
             home=home)
    assert r["ok"] is True, f"timeout={bad!r} 应兜底为默认值而非崩溃"


# ------------------------------------------------- 8. 元信息可供前端渲染
def test_policy_meta_exposes_matrix(home):
  meta = P.POLICY_META()
  assert [m["id"] for m in meta["modes"]] == list(P.MODES)
  assert meta["matrix"]["full"]["high"] == "allow"
  assert meta["matrix"]["confirm"]["medium"] == "ask"
  assert "critical" in meta["note"], "必须向前端声明：critical 不在矩阵内"


def test_blocked_command_has_clear_message(home):
  """破坏性命令必须给出明确引导，而不是"请求内容有误"让用户反复重试。"""
  from omegaforge.core.errors import _classify
  from omegaforge.tools.system_tools import BlockedCommand
  code, msg = _classify(BlockedCommand("x"))
  assert "破坏性" in msg, f"拦截提示必须说明原因，实际：{msg}"
  assert code != "E_INTERNAL", "安全拦截不是服务器故障，不得报 500 级语义"


def test_blocked_command_compat_with_valueerror(home):
  """继承链不能断：既有调用方用 except ValueError 兜底必须仍然成立。"""
  from omegaforge.tools.system_tools import BlockedCommand
  assert issubclass(BlockedCommand, ValueError)
