"""蒸馏数值边界的唯一真源。

为什么必须有唯一真源
--------------------
服务端对 budget/rounds/gens 有闸门
（`minimum=2000`、`minimum=1`）。命令行若直接用 argparse 的裸 `type=int`
**完全绕过**这些闸门——同一批非法输入在图形界面被拒、在命令行被放行。

    $ cli distill "..." --rounds 0 --gens 0
     verdict       : pending
     distilled     : 0.00 / 10     ← 一个用例都没评
     baseline      : 0.00 / 10
     generations   : 0
    [exit=0]                        ← 退出码还是成功

用户拿到空产物且退出码为 0：人以为跑完了，自动化流程也以为跑完了。

所以边界收敛到这里，服务端与 CLI 共用同一份，杜绝两边漂移。

取值依据
------------------------------
* MIN_BUDGET = 2000：mock 模式下 1500 必然在评测阶段抛 BudgetExceeded，
  2000 可完整跑通。低于此值不是"省着用"，是注定跑不完。
* ROUNDS/GENS 下界 1：0 表示"不评测/不进化"，产物必然为空，
  这不是合法配置而是空转配置。
"""
from __future__ import annotations

# 预算（token）
MIN_BUDGET = 2_000
MAX_BUDGET = 100_000_000
DEFAULT_BUDGET = 400_000

# 每代评测用例数
ROUNDS_MIN = 1
ROUNDS_MAX = 50
ROUNDS_DEFAULT = 6

# 最大进化代数
GENS_MIN = 1
GENS_MAX = 20
GENS_DEFAULT = 3

# 检索返回条数
# 上界不是性能考虑，是"静默截断"考虑：检索条数若不做收敛，负值会在下游
# 被静默接受或导致失败。放在这里，是为了让三个入口（服务端 / MCP / 主循环）
# 不会各自拍一个上界然后互相漂移。
SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 50
SEARCH_LIMIT_DEFAULT = 5

# 待办优先级：与 tasks._pri 的 (1,2,3) 同源。
# 内层 _pri() 早已能把脏值收敛成 3，但外层调用点的裸 int() 会**先抛异常**，
# 于是永远走不到内层——增强被绕过，而不是缺失。所以外层也必须收敛。
PRIORITY_MIN = 1
PRIORITY_MAX = 3
PRIORITY_DEFAULT = 2
