/**
 * OmegaForge 后端 API 客户端（类型安全，零臆造）。
 *
 * 字段与路径以后端实际路由为准，任何新增接口都必须与后端对齐。
 * 端口号在后端默认值、桌面端启动配置与本文件三处必须保持一致。
 * 本文件只使用运行时端口，不引入其他端口。
 *
 * 边界：后端由 Tauri 以 sidecar 方式托管，只监听 127.0.0.1。
 */

const DEFAULT_BASE = 'http://127.0.0.1:8787'

/** 构建期可用 VITE_BACKEND_BASE 指定后端地址，未指定时用默认端口。 */
const BASE: string =
  (import.meta.env.VITE_BACKEND_BASE as string | undefined) || DEFAULT_BASE

/* ---------------- 基础请求层 ---------------- */

export class ApiError extends Error {
  /** 后端返回的错误码（E_xxx），可能缺失 */
  public code?: string
  /** 原始响应体，交给 userMessage() 做安全筛查，禁止直接渲染 */
  public body?: unknown

  constructor(
    public path: string,
    public status: number,
    message: string,
    opts?: { code?: string; body?: unknown },
  ) {
    // message 保持后端原始文案；技术前缀不再拼进 message，
    // 展示一律走 format.ts 的 userMessage()。
    super(message)
    this.name = 'ApiError'
    this.code = opts?.code
    this.body = opts?.body
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const { timeoutMs = 8000, ...rest } = init ?? {}
  const ctrl = new AbortController()
  // 超时与"连不上"要分开：fetch 失败时无法从异常本身分辨是哪种，
  // 只能靠这个标记。判错会把超时说成服务没启动，或反过来。
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    ctrl.abort()
  }, timeoutMs)
  try {
    let res: Response
    try {
      res = await fetch(`${BASE}${path}`, { ...rest, signal: ctrl.signal })
    } catch {
      // 两者都不会给出可展示的文案，落到兜底就只剩"请稍后重试"。
      // 但连不上本机服务时重试不会有帮助，必须按原因分别说清。
      throw new ApiError(
        path,
        0,
        '',
        {
          body: timedOut
            ? { error: '请求超时，请稍后重试' }
            : { error: '无法连接到本机服务，请确认应用已正常启动' },
        },
      )
    }
    const text = await res.text()
    let body: unknown = null
    try {
      body = text ? JSON.parse(text) : null
    } catch {
      body = text
    }
    if (!res.ok) {
      const msg =
        typeof body === 'object' && body && 'error' in body
          ? String((body as { error: unknown }).error)
          : text.slice(0, 200)
      const code =
        typeof body === 'object' && body && 'code' in body
          ? String((body as { code: unknown }).code)
          : undefined
      throw new ApiError(path, res.status, msg, { code, body })
    }
    return body as T
  } finally {
    clearTimeout(timer)
  }
}

const get = <T>(p: string, t?: number) => request<T>(p, { timeoutMs: t })

const post = <T>(p: string, payload?: unknown, t?: number) =>
  request<T>(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: payload === undefined ? '{}' : JSON.stringify(payload),
    timeoutMs: t,
  })

/* ---------------- 类型定义（取自后端真实响应结构） ---------------- */

export interface BackendStatus {
  version: string
  /** 真实字段名是 mock_mode；写成 mock 不会报错，但取值恒为假值，显示必然出错 */
  mock_mode: boolean
  models: { main: string; fast: string; judge: string }
  jobs: number
  /** 由后端 DEFAULT_BUDGET 与全局用量台账计算，非前端臆造 */
  budget: { limit: number; spent: number; remaining: number }
}

export interface ProviderCurrent {
  base_url: string
  has_key: boolean
  models: { main?: string; fast?: string; judge?: string }
}

export interface ProviderPreset {
  id?: string
  name?: string
  base_url?: string
  has_key?: boolean
  models?: Record<string, string>
  model_catalog?: string[]
  [k: string]: unknown
}

export interface ProvidersResponse {
  presets: ProviderPreset[]
  /**
   * 全新安装（尚未配置供应商）时后端返回 null，不是空串，
   * 因此这里必须允许 null，并把它当作"尚未配置"处理。
   */
  active: string | null
  current: ProviderCurrent
}

/**
 * GET /api/runs 的真实字段：
 *   ["created_ts", "id", "meta", "phase", "progress", "seq", "status"]
 *
 * 字段名只有一份真源，即后端返回本身：时间字段是 created_ts 而非
 * created_at，阶段字段是单值 phase 而非数组 phases。宽松的索引签名
 * 会让这类偏差在编译期完全不可见，取值一律以这里列出的名字为准。
 *
 * 注：outcome 只在 /api/jobs/<id> 详情里有，列表接口不返回。
 */
export interface RunItem {
  id: string
  status: string
  /** 真实字段名是 created_ts（不是 created_at） */
  created_ts?: number
  /** 真实字段名是 phase（单值字符串），不是 phases（数组） */
  phase?: string
  progress?: number
  [k: string]: unknown
}

export interface JobDetail extends RunItem {
  log?: Array<{ t?: number; level?: string; msg?: string; [k: string]: unknown }>
  report?: unknown
  events?: unknown[]
}

/**
 * POST /api/distill 的真实返回：{"job": jid, "run": jid}
 * 注意不是 job_id —— 按 job_id 取值为 undefined，
 * 后续轮询会指向无效地址。
 */
export interface DistillStartResponse {
  job: string
  run: string
  [k: string]: unknown
}

export interface ChatSendPayload {
  message: string
  conversation_id?: string
  model?: string
  persona?: string
}

export interface ChatReply {
  reply?: string
  conversation_id?: string
  model?: string
  why?: string
  [k: string]: unknown
}

export interface Conversation {
  id: string
  title?: string
  model_pref?: string
  messages?: Array<{ role: string; content: string }>
  [k: string]: unknown
}

/* ---------------- 系统 / 供应商 ---------------- */

/** GET /api/status —— 版本、mock 模式、当前模型、在跑任务数 */
export const fetchStatus = () => get<BackendStatus>('/api/status')

/** GET /api/usage —— Token 用量汇总 */
export const fetchUsage = () => get<Record<string, unknown>>('/api/usage')

/** GET /api/providers —— 供应商清单 + 当前生效配置 */
export const fetchProviders = () => get<ProvidersResponse>('/api/providers')

/** GET /api/providers/models?name=xxx —— 某供应商的可用模型目录 */
export function fetchProviderModels(name: string) {
  // 真实返回 {"name":..., "catalog":[...], "online":...}，
  // 没有 models 字段 —— 按 models 取值恒为 undefined。
  return get<{ name?: string; catalog?: string[]; online?: boolean | null }>(
    `/api/providers/models?name=${encodeURIComponent(name)}`,
  )
}

/** POST /api/providers/apply —— 应用供应商配置（name 必填） */
export function applyProvider(p: {
  name: string
  base_url?: string
  api_key?: string
  models?: { main?: string; fast?: string; judge?: string }
}) {
  return post<{ applied: string; config: ProviderCurrent }>(
    '/api/providers/apply',
    p,
  )
}

/** POST /api/providers/test —— 探测连通性（base_url 与 model 必填） */
export function testProvider(p: {
  name?: string
  base_url: string
  api_key?: string
  models?: { main?: string }
}) {
  return post<Record<string, unknown>>('/api/providers/test', p, 20000)
}

/* ---------------- 蒸馏 / 运行 ---------------- */

/** POST /api/distill —— 启动一次蒸馏流水线 */
export function startDistill(payload: Record<string, unknown>) {
  return post<DistillStartResponse>('/api/distill', payload, 15000)
}

/**
 * GET /api/runs —— 运行记录列表。
 * 返回的是 {"runs": [...]} 包装对象，不是裸数组；
 * 若按裸数组取值，length 与 map 都会失效，页面无法渲染。
 */
export const fetchRuns = async (): Promise<RunItem[]> => {
  const d = await get<{ runs?: RunItem[] }>('/api/runs')
  return Array.isArray(d?.runs) ? d.runs : []
}

/** GET /api/jobs/<id> —— 单个任务详情（含事件流 / 报告） */
export const fetchJob = (id: string) =>
  get<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`)

/** GET /api/report/<job> —— 蒸馏报告 JSON */
export const fetchReport = (id: string) =>
  get<Record<string, unknown>>(`/api/report/${encodeURIComponent(id)}`)

/**
 * GET /api/compare?a=<基准>&b=<对照> —— 两个任务的跨版本比对。
 *
 * 为什么必须有这一条：命令行侧有该能力，界面若无入口则能力等于不存在——
 * 界面是多数用户的主入口。
 * 参数顺序固定为基准在前、对照在后；顺序反了则结论强弱方向随之反转。
 */
export const fetchCompare = (a: string, b: string) =>
  get<Record<string, unknown>>(
    `/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`)

/**
 * GET /api/genome/<job> —— 能力基因组 JSON。
 * 与 /api/report 成对存在：report 讲结论，genome 讲结构。
 * 基因组面板的数据源来自这里，另一端是 /api/report。
 */
export const fetchGenome = (id: string) =>
  get<Record<string, unknown>>(`/api/genome/${encodeURIComponent(id)}`)

/* ---------------- 对话 ---------------- */

/** POST /api/chat —— 发送一条消息（message 必填） */
export function sendChat(p: ChatSendPayload) {
  return post<ChatReply>('/api/chat', p, 60000)
}

/**
 * POST /api/chat/stream —— SSE 流式对话。
 * 返回底层 Response，由调用方读取 ReadableStream。
 */
export function streamChat(p: ChatSendPayload) {
  return fetch(`${BASE}/api/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p),
  })
}

/** GET /api/conversations —— 会话列表 */
export const fetchConversations = async (): Promise<Conversation[]> => {
  const d = await get<{ conversations?: Conversation[] }>('/api/conversations')
  return Array.isArray(d?.conversations) ? d.conversations : []
}

/** POST /api/conversations/new —— 新建会话 */
export const newConversation = (title?: string) =>
  post<Conversation>('/api/conversations/new', { title })

/** POST /api/conversations/model —— 设置会话偏好模型 */
export const setConversationModel = (id: string, model: string) =>
  post<Conversation>('/api/conversations/model', {
    conversation_id: id,
    model,
  })

/** POST /api/conversations/delete —— 删除会话 */
export const deleteConversation = (id: string) =>
  post<Record<string, unknown>>('/api/conversations/delete', {
    conversation_id: id,
  })

/* ---------------- 记忆 / 知识库 / 任务 / Wiki ---------------- */

export const rememberMemory = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/memory/remember', p)

/**
 * POST /api/memory/recall —— 检索记忆。
 * 查询词的键名是 q，以后端路由为准。键名写错不会报错，
 * 但查询词恒为空串，界面上搜任何词都返回空。
 */
export const recallMemory = (q: string) =>
  post<Record<string, unknown>>('/api/memory/recall', { q })

export const kbList = () => get<Record<string, unknown>>('/api/kb/list')

export const kbAdd = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/kb/add', p)

/**
 * POST /api/kb/search —— 检索知识库。
 * 查询词的键名是 q，以后端路由为准。键名写错不会报错，
 * 但库里有内容也永远搜不到。
 */
/**
 * POST /api/kb/delete —— 删除一条知识。
 * 没有它，用户在界面上加错一条就再也删不掉：存储层的 delete() 早已
 * 存在，缺的只是这个出口。
 */
export const kbDelete = (id: string) =>
  post<Record<string, unknown>>('/api/kb/delete', { doc_id: id })

export const kbSearch = (q: string) =>
  post<Record<string, unknown>>('/api/kb/search', { q })

// 默认 scope 是 pending，界面若照默认取，就永远拿不到已完成的条目——
// 于是"显示已完成"点了也没东西可显示，而概览里明明写着"已完成 N"。
// 用户在界面上完成一件事之后，就再也看不到它了。
// 界面自己有 showDone 过滤，故一律取全量，由界面决定显示哪些。
export const tasksList = (scope = 'all') =>
  get<Record<string, unknown>>(`/api/tasks/list?scope=${encodeURIComponent(scope)}`)

export const taskAdd = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/tasks/add', p)

/**
 * POST /api/tasks/done —— 标记完成。
 * 任务编号的键名是 task_id，以后端路由为准。写成 id 会被判为缺参，
 * 而用户点的是按钮、没有输入框可填，只能反复失败。
 */
export const taskDelete = (id: string) =>
  post<Record<string, unknown>>('/api/tasks/delete', { task_id: id })

export const taskDone = (id: string) =>
  post<Record<string, unknown>>('/api/tasks/done', { task_id: id })

export const wikiList = () => get<Record<string, unknown>>('/api/wiki/list')

/**
 * GET /api/wiki/page?slug=xxx —— 单个词条详情。
 * 查询串键名是 slug，以后端路由为准。写成 name 会取不到词条，
 * 列表里明明列着的条目点开却显示未找到。
 * （后端另有 ?q= 走搜索分支，两个键名不可混用。）
 */
export const wikiPage = (slug: string) =>
  get<Record<string, unknown>>(`/api/wiki/page?slug=${encodeURIComponent(slug)}`)

export const wikiSave = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/wiki/save', p)

export const wikiDelete = (slug: string) =>
  post<Record<string, unknown>>('/api/wiki/delete', { slug })

/* ---------------- 技能 / 工具 / 语音 / 人格 ---------------- */

export const skillsList = () => get<Record<string, unknown>>('/api/skills/list')

export const skillInstall = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/skills/install', p, 30000)

export const skillInvoke = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/skills/invoke', p, 60000)

export const fetchPermissions = () =>
  get<{ permissions: Record<string, unknown> }>('/api/tools/permissions')

// 保存系统能力开关。
//
// 为什么必须有这个 POST：工具执行的拒绝提示是「请在「设置 → 系统能力」
// 中开启后重试」，而界面上若只有读取，用户按提示找到面板后看到的是
// 「此面板只读」——指引把他送来，这里又把他推走，形成闭环死路。
// 开启文件/终端/网络能力是高权限操作，故由界面在提交前自行确认。
export const savePermissions = (p: Record<string, boolean>) =>
  post<{ permissions: Record<string, unknown> }>(
    '/api/tools/permissions', p)

export interface ToolAuditEntry {
  ts?: number
  tool?: string
  risk?: string
  mode?: string
  capability?: string
  origin?: string
  verdict?: string
  reason?: string
}

export function fetchToolAudit(limit = 50) {
  return get<{ entries: ToolAuditEntry[] }>(
    `/api/tools/audit?limit=${encodeURIComponent(String(limit))}`)
}

export const voiceStatus = () => get<Record<string, unknown>>('/api/voice/status')

export const voiceAsr = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/voice/asr', p, 60000)

export const voiceTts = (p: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/voice/tts', p, 60000)

/** 模型下载进度。下载在后端后台线程跑，这里只轮询状态。 */
export const voiceModelProgress = (kind: string) =>
  get<Record<string, unknown>>(
    `/api/voice/models/progress?kind=${encodeURIComponent(kind)}`)

/** 开始下载模型。立即返回，不等待下载完成。 */
export const voiceModelInstall = (kind: string) =>
  post<Record<string, unknown>>('/api/voice/models/install', { kind })

export const fetchPersonas = () => get<Record<string, unknown>>('/api/personas')
