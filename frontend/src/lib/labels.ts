/**
 * 字段键名 → 中文标签（表层防线之一）。
 *
 * ## 为什么需要这个文件
 *
 * 后端返回的 JSON 键名是英文 snake_case（persona_genes、baseline_comparable…）。
 * 直接渲染会让用户看到 "PERSONA GENES" 这类开发术语——这是最典型的技术残留。
 *
 * ## 键名来源（取自后端真实响应）
 *
 * - Genome 19 键 / DistillReport 10 键：蒸馏流水线产出的结构化字段
 * - Run 与 Job：RunItem / JobDetail（lib/api.ts）
 * - BackendStatus：/api/status 的返回结构
 *
 * 任何新增后端字段都必须同步到这里，否则校验会报未翻译。
 */

const FIELD_LABELS: Record<string, string> = {
  /* ---- Genome：身份 ---- */
  name: '名称',
  mission_one_liner: '使命',
  source_fingerprint: '源指纹',
  lineage: '血统',

  /* ---- Genome：基因 ---- */
  persona_genes: '人格基因',
  tool_genes: '工具基因',
  workflow_genes: '工作流基因',
  upgrade_genes: '增强基因',

  /* ---- Genome：编译产物 ---- */
  system_prompt: '系统提示词',
  tools: '工具',
  workflow: '工作流',

  /* ---- Genome：经济性 ---- */
  est_system_tokens: '提示词估算 Token',
  baseline_tokens_per_task: '单任务基线 Token',

  /* ---- Genome：进化状态 ---- */
  arena_generation: '竞技场代数',
  arena_best_score: '竞技场最佳分',
  arena_history: '竞技场历史',

  /* ---- Genome：元信息 ---- */
  id: '标识',
  created_ts: '创建时间',
  schema_version: '结构版本',

  /* ---- DistillReport ---- */
  source_signals: '源特征',
  generation: '代数',
  final_score: '蒸馏体得分',
  baseline_score: '对照组得分',
  verdict: '裁决',
  judge_reasons: '评审理由',
  evolution_notes: '进化备注',
  baseline_kind: '对照组类型',
  baseline_comparable: '对照组可比',
  baseline_note: '对照组说明',

  /* ---- DistillReport：结论可信度（计算属性，非固定字段，
   * 静态字段表收录不到，必须手工同步）----
   * 这三项决定「更强」这个结论能不能成立，是本产品最核心的字段，
   * 一旦漏译就会以英文字段名原样显示给用户。 */
  claim_valid: '结论成立',
  self_certified: '裁判与作答同源',
  exam_self_authored: '考题由被测方自出',
  /* 注意：`contamination_note` 后端从不产生（可信度说明只有
   * 结论说明一个字段，含四种原因分支）。若给这个键配上"污染说明"
   * 的翻译，会让人以为存在该字段并照着去找。此处不再声明。 */

  /* ---- DistillReport：评测过程 ---- */
  ablation: '基因消融',
  answer_model: '作答模型',
  arena_cases: '评测用例数',
  best_generation: '最佳代',
  contaminated_cases: '含操纵痕迹用例',
  debiased_cases: '完成去偏用例',
  eval_set_cases: '评测集用例数',
  eval_set_fingerprint: '评测集指纹',
  eval_set_source: '评测集来源',
  generations_run: '已跑代数',
  judge_model: '裁判模型',
  question_model: '出题模型',
  question_source: '题目来源',
  rolled_back: '未改善已回滚',
  rubric_source: '评分标准来源',
  trust_note: '可信度说明',

  /* ---- Run / Job ----
   * 真实字段（GET /api/runs、GET /api/jobs/<id>）：
   *   ["created_ts","id","meta","phase","progress","seq","status", ...]
   * created_at / phases 这两个名字后端都不返回，写成它们会让
   * 列表与详情里的创建时间、阶段两栏取不到值。
   * 注意 Genome 段已有 created_ts:'创建时间'，这里不要重复声明。 */
  status: '状态',
  outcome: '结果',
  phase: '当前阶段',
  progress: '进度',
  seq: '事件序号',
  meta: '元信息',
  log: '日志',
  report: '报告',
  events: '事件',
  genome: '基因组',
  tokens_spent: '已用 Token',

  /* ---- 后端状态 ---- */
  version: '版本',
  mock_mode: '离线演示模式',
  models: '模型',
  jobs: '进行中的任务',
  budget: '预算',

  /* ---- 竞技场 / 评测通用 ---- */
  score: '得分',
  reason: '理由',
  task: '任务',
  case: '用例',
  winner: '胜出方',
  delta: '差值',
  kind: '类型',
  note: '说明',
  label: '标签',
  prompt_chars: '提示词字符数',
  comparable: '可比',
  source_ref: '来源',
  message: '消息',
  reply: '回复',
  title: '标题',
  content: '内容',
  role: '角色',

  /* ---- 模型供应商（/api/providers 真实响应） ---- */
  active: '启用状态',
  base_url: '接口地址',
  current: '当前配置',
  detected_models: '检测到的模型',
  has_key: '已配置密钥',
  latency_ms: '响应延迟(毫秒)',
  local: '本地服务',
  model_catalog: '模型目录',
  needs_key: '需要密钥',
  online: '在线状态',
  presets: '预设供应商',
  catalog: '可用模型',

  /* ---- 用量统计（/api/usage 真实响应） ---- */
  bucket_seconds: '统计间隔(秒)',
  by_model: '按模型统计',
  by_phase: '按阶段统计',
  entries: '记录数',
  heatmap: '活跃热力图',
  hottest_phase: '最耗时阶段',
  timeline: '时间分布',
  total: '合计',

  /* ---- 预算（/api/status.budget 真实响应） ---- */
  limit: '额度上限',
  spent: '已用额度',
  remaining: '剩余额度',

  /* ---- 模型角色（/api/status.models 真实响应） ---- */
  main: '主模型',
  fast: '快速模型',
  judge: '评审模型',

  /* ---- 工具权限（/api/tools/permissions 真实响应） ---- */
  permissions: '工具权限',
  terminal: '终端执行',
  fs: '文件读写',
  web_fetch: '联网抓取',

  /* ---- 集合类容器（各列表端点真实响应） ---- */
  items: '条目',
  results: '结果',
  conversations: '会话',
  memories: '记忆',
  docs: '知识库文档',
  pages: '词条',
  skills: '技能',
  runs: '运行记录',
  categories: '分类',
  stats: '统计',
  pending: '待处理',
  done: '已完成',

  /* ---- 列表端点逐字段补全 ----
   * 这些键若未收录，界面上会原样显示英文（count / done_ts / ts /
   * files_missing …）。来源是后端真实响应，不是按名猜的语义：
   *   /api/conversations  → count(消息数) / model_pref(模型偏好) / updated
   *   /api/tasks/list     → created / done_ts / priority / text
   *   /api/kb/list        → tags / text / ts / type
   *   /api/wiki/list      → links / slug
   *   /api/skills/list    → _dir(技能目录) / description / type / category
   *   /api/voice/status   → asr / tts / model / ready / lib_installed /
   *                         files_missing
   */
  count: '消息数',
  model_pref: '模型偏好',
  updated: '更新时间',
  created: '创建时间',
  done_ts: '完成时间',
  priority: '优先级',
  text: '正文',
  tags: '标签',
  ts: '时间',
  type: '类型',
  category: '分类',
  links: '反向链接',
  slug: '标识',
  description: '说明',
  _dir: '技能目录',
  asr: '语音识别',
  tts: '语音合成',
  ready: '就绪状态',
  lib_installed: '依赖库已安装',
  files_missing: '缺失的模型文件',
  model: '模型',

  /* ---- 错误（Structured 渲染兜底） ---- */
  error: '错误',
  code: '错误代码',
}

/**
 * 未收录字段的兜底。
 *
 * 不做 uppercase、不做下划线替换——那会让英文键名更醒目。
 * 这里保持小写原样，交由校验环节催补翻译，
 * 而不是把未翻译的英文放大给用户看。
 */
export function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key
}


/* ---- 运行阶段（动态键名，静态字段表覆盖不到）----
 *
 * 为什么必须单独一份：`/api/usage` 的 `by_phase` 是 **dict**，键名就是阶段
 * 标识本身（arena:judge、distill:extract …）。FIELD_LABELS 只收录固定字段名，
 * 永远命中不了这些动态键。若不做这层映射，用量页会原样显示
 * "arena:judge 212"、"distill:extract 205"，"最耗时阶段" 一栏
 * 显示 "arena:judge"。
 *
 * 键名取自后端产生的阶段标识，不是按名字猜的语义。
 */
const PHASE_LABELS: Record<string, string> = {
  chat: '对话',
  'arena:answer': '竞技·作答',
  'arena:judge': '竞技·裁决',
  'distill:extract': '蒸馏·抽取',
  'distill:compress': '蒸馏·压缩',
  'distill:gen_eval': '蒸馏·出题',
  'distill:synthesize': '蒸馏·合成',
  'evolve:critique': '进化·批评',
  ablation: '基因消融',
  freeze_eval: '冻结评测集',
  arena: '竞技',
  distill: '蒸馏',
  evolve: '进化',
  extract: '抽取',
  compress: '压缩',
  gen_eval: '评测生成',
  synthesize: '合成',
  finalize: '收尾',
  ingest: '摄入',
}

/**
 * 阶段名 → 中文。
 *
 * 三级回退，每级都有理由：
 *  1. 整名命中（arena:judge → 竞技场·裁决）
 *  2. 前缀命中（新增的 distill:xxx → 蒸馏·xxx）：新阶段不至于裸奔英文
 *  3. 原文：绝不臆造中文把未翻译的状态伪装成已翻译——那比显示英文更糟，
 *     用户会以为看懂了。未知阶段交由校验环节催补。
 */
export function phaseLabel(key: unknown): string {
  const k = String(key ?? '')
  if (PHASE_LABELS[k]) return PHASE_LABELS[k]
  const i = k.indexOf(':')
  if (i > 0) {
    const head = PHASE_LABELS[k.slice(0, i)]
    const tail = PHASE_LABELS[k.slice(i + 1)]
    if (head) return `${head}·${tail || k.slice(i + 1)}`
  }
  return k
}

/** 阶段名是否在映射表内——供校验使用。 */
export function hasPhaseLabel(key: unknown): boolean {
  return String(key ?? '') in PHASE_LABELS
}

/** 导出供校验核对覆盖率。 */
export const PHASE_KEYS = Object.keys(PHASE_LABELS)

/** 是否在映射表内——供校验使用，也用于未知字段的弱化样式。 */
export function hasLabel(key: string): boolean {
  return key in FIELD_LABELS
}

/** 导出供校验核对覆盖率。 */
export const LABEL_KEYS = Object.keys(FIELD_LABELS)
