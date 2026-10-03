# -*- coding: utf-8 -*-
"""行为配置不可变守卫：只做自进化，不做自我改进。

分界线
------
可以进化：提示词基因、技能 SKILL.md —— 可版本化、可评测、可回滚，
     且必须过冻结评测集与门禁才能晋升。
不可改进：AGENTS.md / SOUL.md / TOOLS.md / MEMORY.md 等行为配置 ——
     由人类主权持有，agent 只能提提案。

为什么必须禁止后者：允许 agent 改写自身行为规则，就等于允许它给自己扩权。
一旦被注入，就是**持久化**攻击链——外部内容改写规则后，此后每一次行为
都被改写后的规则支配，比单次命令执行严重得多。这与"把不可信数据当作
受信任配置、再由代理用既有凭据执行"是同一个失效模式。

读必须放行：agent 读自己的规则是它工作的基本前提，误杀等于让它失明。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from omegaforge.tools.rules import ( # noqa: E402
  critical_check, _immutable_target, IMMUTABLE_CONFIG_NAMES,
)

Z = "​"     # 零宽空格
CY = "е"     # 西里尔 е


def test_write_blocked_for_all_immutable_names():
  """清单内每个名字，写入都应被拒。"""
  for name in IMMUTABLE_CONFIG_NAMES:
    assert critical_check("fs.write", {"rel": name}), \
      f"写入 {name} 未被拦截"


def test_write_blocked_with_path_prefix_and_case():
  """带目录前缀、小写变体都应拦截。"""
  for p in ["workspace/AGENTS.md", "./SOUL.md", "a/b/TOOLS.md",
       "agents.md", "memory.md"]:
    assert critical_check("fs.write", {"rel": p}), f"写入 {p} 未被拦截"


def test_read_is_never_blocked():
  """读取必须放行——agent 靠读规则工作，误杀等于让它失明。"""
  for name in IMMUTABLE_CONFIG_NAMES:
    assert critical_check("fs.read", {"rel": name}) is None, \
      f"读取 {name} 被误杀"
    assert critical_check("fs.list", {"rel": name}) is None, \
      f"列出 {name} 被误杀"


def test_normal_files_unaffected():
  """普通文件写入不受影响，尤其 SKILL.md 属于**可进化**工件。"""
  for p in ["notes.md", "src/main.py", "skills/demo/SKILL.md",
       "output/report.md", "a.py"]:
    assert critical_check("fs.write", {"rel": p}) is None, \
      f"正常文件 {p} 被误杀"


def test_zero_width_and_homoglyph_do_not_bypass():
  """L0 归一化：不可见字符与同形字不能让清单失效（教训）。"""
  assert _immutable_target(f"AGENTS{Z}.md") == "AGENTS.md"
  assert _immutable_target(f"SOUL{Z}.md") == "SOUL.md"
  assert _immutable_target(f"./MEMORY{Z}.md") == "MEMORY.md"


def test_cmd_write_blocked():
  """命令侧写入拦截：重定向、tee、sed -i、cp、mv。

  必须走 critical_check("terminal") **接通层**判定，而不是直接调
  _cmd_immutable_write。注意事项：第一版测内层函数，撤掉 _cmd_critical
  里的接通后测试照样全绿——接通断了守卫形同虚设。
  """
  cases = [
    "echo x > AGENTS.md",
    "cat a >> SOUL.md",
    "echo x | tee TOOLS.md",
    "echo x | tee -a TOOLS.md",
    "sed -i 's/a/b/' MEMORY.md",   # 缺少该约束时漏判：表达式占了第一个 token
    "sed -i.bak s/a/b/ AGENTS.md",
    "cp /tmp/x AGENTS.md",
    "mv /tmp/y SOUL.md",
    "printf x > HEARTBEAT.md",
  ]
  for c in cases:
    assert critical_check("terminal", {"cmd": c}), f"命令未被拦截：{c}"


def test_cmd_read_never_blocked():
  """命令侧读取必须放行——这是"读允许写禁止"的切分点。"""
  cases = [
    "cat AGENTS.md",
    "grep -n x SOUL.md",
    "cat AGENTS.md > out.txt",    # 读规则、写到别处，必须放行
    "sed -n '1,5p' AGENTS.md",
    "sed 's/a/b/' file.md",
    "ls -la",
    "echo hi > note.md",
    "git status",
    "sed -i 's/a/b/' app.py",
  ]
  for c in cases:
    assert critical_check("terminal", {"cmd": c}) is None, \
      f"命令被误杀：{c}"


def test_gate_blocks_regardless_of_mode():
  """critical 层与四级矩阵独立：full（完全访问）也不能绕过。"""
  # critical_check 不接受 mode 参数，正说明它与矩阵无关
  assert critical_check("fs.write", {"rel": "AGENTS.md"})


if __name__ == "__main__":
  import pytest
  sys.exit(pytest.main([__file__, "-q"]))
