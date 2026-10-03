# Ω OmegaForge

> **蒸馏任何 Agent，锻造出更强的，花更少的 Token。**

OmegaForge 是一个全面对标并超越 agency-swarm / agency-agents 生态的开源超级 Agent 框架。它的核心是一个**蒸馏引擎**：投喂任意类型的 Agent（Python 类 / YAML / JSON / 纯 prompt / 整个仓库 / markdown 人设），它自动完成 **摄取 → 提取 → 压缩 → 合成 → 出题 → 竞技场对战 → 进化** 的全流程，交付一个经过对战验证、Token 更省的蒸馏体——并附带开源桌面独立端 Studio。

```
任何 Agent 源 ──▶ Ω 蒸馏引擎 ──▶ 更强更省的蒸馏体 + 基因组 + 战报
   (不调用模型)      (预算内自动进化)        (胜出才交付，落败继续进化)
```

## 为什么更强、为什么更省

| 能力 | agency-swarm 生态 | OmegaForge |
|---|---|---|
| token 成本控制 | 无内置统计（issue #158/#162 实证），建团即超限额 | **TokenBank 预算硬熔断**，分阶段台账，charge 前预估 |
| Agent 间通信 | 自由文本 Communication Sheets，每跳全量 LLM 往返 | **Schema 校验 Envelope + Blackboard**，路由零 NLU 开销 |
| 系统提示词 | 冗长 instructions | **Genome 基因压缩**（est. ↓60-85%），密度优先编译 |
| 模型路由 | 单档为主 | **三档路由**（fast/main/judge），重活才用贵模型 |
| "更强"如何保证 | 无机制 | **竞技场闭环**：自动出题 → 同题对战 → 裁判评分 → 落败则改进并重新编译，**胜出才交付** |
| 死循环防护 | Genesis 卡死（issue #150） | 步数上限 + 预算熔断 + win 早停三重护栏 |
| 桌面独立端 | 无（全部 Web UI） | **桌面端 Studio**：系统托盘 + 中文安装器 + 自选安装目录（dmg/nsis/AppImage） |
| MCP 服务器 | 无 | **stdio JSON-RPC 2.0**，14 个工具，任意 MCP 客户端可挂载 |
| 技能系统 | 无 | **SKILL.md 安装/调用**，渐进披露省 token |
| 个人数据 | 无 | **知识库 + Wiki（双链）+ 待办 + 长期记忆**，agent 可直接调用 |
| 复刻任意 Agent | 不支持 | **SourceAgentLoader 通用摄取**：py/yaml/json/md/dir/粘贴文本，0 token |

## 产品能力（v0.2）

| 能力 | 状态 | 说明 |
|---|---|---|
| MCP 服务器 | ✅ | `python3 -m omegaforge.mcp_server`（stdio JSON-RPC 2.0，14 个工具），Claude Desktop/Cursor 可直接挂载 |
| 技能系统 | ✅ | SKILL.md 安装/列举/调用，渐进披露（列表只读元数据，调用才载入正文） |
| 个人知识库 | ✅ | CJK 二元组检索，零依赖离线可用 |
| Wiki | ✅ | markdown 页面 + [[双链]] + 反链 + 搜索，同步入 KB 索引 |
| 待办清单 | ✅ | 优先级/完成/统计 |
| 长期记忆 | ✅ | remember/recall，与知识库同引擎 |
| 系统托盘 | ✅ | 中文菜单，关窗后继续驻留托盘 |
| 中文安装器 | ✅ | NSIS 中文界面 + **自选安装目录** + 桌面/开始菜单快捷方式 |
| 桌面端 | ✅ | 桌面壳自动拉起 Python 后端，dmg/nsis/AppImage 三端打包 |

蒸馏体运行时可挂载个人工具包，直接读写你的知识库、词条库与待办。

## 快速开始

### CLI（零依赖，stdlib-only）

```bash
# 1) 蒸馏一个源 Agent（可直接粘贴提示词正文，也可给文件路径）
python3 -m omegaforge.cli distill "你是一个严谨的研究助手，负责检索并输出结构化简报。"

# 2) 查看裁决报告
python3 -m omegaforge.cli report output

# 3) 运行蒸馏出的蒸馏体
python3 -m omegaforge.cli run output/genome.json "研究：2026 年 AI Agent 趋势"
```

接真实 LLM（可选，不配则自动 Mock 离线模式）：

```bash
export OMEGAFORGE_API_KEY=sk-...
export OMEGAFORGE_BASE_URL=https://api.openai.com/v1   # 任意遵循 OpenAI 接口规范的地址
export OMEGAFORGE_MODEL_MAIN=gpt-4o-mini
export OMEGAFORGE_MODEL_FAST=gpt-4o-mini
export OMEGAFORGE_MODEL_JUDGE=gpt-4o-mini
```

### 桌面独立端 Studio

```bash
# 方式 A：网页端（起本地服务后用浏览器打开）
python3 -m omegaforge.server          # → http://127.0.0.1:8787

# 方式 B：桌面端
cd frontend && npm install && npm run build   # 先构建界面
npm run tauri build                          # 再打包安装包
```

界面五幕：**投喂源 Agent → 流水线日志 → 蒸馏裁决（竞技场）→ 基因组解剖 → 编译产物**，
另有 **知识库 / 任务 / 技能 / 记忆** 四个产品标签页。支持 `#job=<id>` 深链分享战报。

### MCP 接入（Claude Desktop 等）

```json
{"mcpServers": {"omegaforge": {"command": "python3",
  "args": ["-m", "omegaforge.mcp_server"],
  "cwd": "/path/to/omegaforge"}}}
```

### 测试

```bash
python3 tests/_legacy_all_features.py    # 核心引擎 55 项审计
python3 tests/_legacy_product_suite.py   # 产品套件 34 项
python3 tests/test_pipeline.py           # 端到端验证
```

## 蒸馏流水线

| 步骤 | 说明 | 模型档 | token |
|---|---|---|---|
| 1 INGEST | SourceAgentLoader 通用摄取（py/yaml/json/md/dir/paste） | — | **0** |
| 2 EXTRACT | AgentArchaeologist：信号 → 结构化 SourceSpec | fast | 1 call |
| 3 COMPRESS | SourceSpec → Genome（persona/tool/workflow/upgrade 四类基因） | fast | 1 call |
| 4 合成 | 基因组 → 编译提示词（≤350 词，密度优先） | **主模型** | 1 次调用 |
| 5 GEN_EVAL | 按使命自动出题：4 典型 + 1 对抗 + 1 压力 | fast | 1 call |
| 6 ARENA | 蒸馏体 vs 基线同题作答，LLM 裁判 5 维打分 | judge | 2N calls |
| 7 EVOLVE | 败则 CRITIQUE → upgrade_genes 引入突变 → 重编译 → 回到 6 | fast+main | 按需 |

护栏：`--budget` 硬熔断（charge 前预估）· `--gens` 代数上限 · win 早停。

## 文件结构

```
omegaforge/
├── omegaforge/            # 核心 Python 包（零第三方依赖）
│   ├── core/              #   预算、错误转译、原子写入、入参校验
│   ├── llm/               #   三档模型接入与用量记账
│   ├── distill/           #   摄取 · 基因组 · 蒸馏引擎
│   ├── agent/             #   蒸馏体运行时
│   ├── memory/            #   知识库 · 词条库 · 待办 · 记忆
│   ├── tools/             #   门禁、策略与工具执行
│   ├── chat/ voice/ skills/ mcp_server.py
│   ├── cli.py             # 命令行入口
│   └── server.py          # 界面后端 API
├── frontend/              # 界面（React + TypeScript + Vite）
├── src-tauri/             # 桌面壳配置
├── tests/                 # 端到端离线测试
├── docs/                  # 架构总览与界面设计规范
└── examples/              # 演示脚本
```

## 文档

- [架构总览（后端架构图 · 蒸馏时序图 · Token 经济学）](docs/ARCHITECTURE.md)
- [前端 UI/UX 架构（设计语言 · 信息架构 · 组件树 · 交互流）](docs/UI_UX_DESIGN.md)

## 路线图

- [ ] v0.2：真实基线回放（把原版 agent 本体接入竞技场，替代默认基线）
- [ ] v0.2：GitHub 仓库直接蒸馏（自动 clone + 摄取）
- [ ] v0.3：轨迹 SFT 档蒸馏（AgentBank 式，可选 GPU）
- [ ] v0.3：多 Agent 编排（Envelope 总线 + 动态拓扑可视化）
- [ ] v0.4：技能市场（Genome 分享 / 版本化 / 组合）

## 诚实边界

"蒸馏出来一定比原版强"在数学上无人能保证——OmegaForge 的答案是把"更强"变成**可验证的工程闭环**：不赢就进化，赢了才交付，每份战报可审计。当前 v0.1 的竞技场基线为默认基线（无质量要求的长回答），生产使用请接入真实原版回放（见路线图 v0.2）。

## License

MIT
