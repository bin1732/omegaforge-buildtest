/**
 * 面向用户的文案层（表层防线）。
 *
 * 职责：任何从后端/运行时冒出来的技术字符串，在到达 <Alert>/<p> 之前
 * 必须过这一层。目标是——用户永远看不到 URL、HTTP 状态、异常类名、
 * 文件路径、JSON 片段、英文报错。
 *
 * 错误码取自后端的错误码常量，与后端返回的 {error, code} 对齐。
 */

/** 后端错误码 → 中文短句。这里的键必须与后端错误码的字面值一致。 */
const CODE_TEXT: Record<string, string> = {
  E_NETWORK: '无法连接到模型服务，请检查网络与接口地址',
  E_TIMEOUT: '请求超时，请稍后重试',
  E_AUTH: '密钥无效或已过期，请在设置中更新',
  E_FORBIDDEN: '当前密钥无权访问该资源',
  E_NOT_FOUND: '请求的内容不存在或已被清理',
  E_RATE_LIMIT: '请求过于频繁，请稍后再试',
  E_UPSTREAM: '模型服务暂时不可用，请稍后重试',
  E_BAD_RESPONSE: '模型返回了无法解析的内容',
  E_INVALID_INPUT: '输入有误，请检查后重试',
  E_PERMISSION: '该功能需要授权，请在「设置 → 系统能力」中开启后重试',
  E_APPROVAL: '该操作需要你确认后才会执行',
  E_PLAN: '计划模式：以下操作待你确认后再执行',
  E_INTERNAL: '操作失败，请稍后重试',
}

/** HTTP 状态 → 中文短句（后端未给出 code 时的兜底）。 */
const HTTP_TEXT: Record<number, string> = {
  400: '请求格式有误',
  401: '密钥无效或已过期，请在设置中更新',
  403: '当前操作不被允许',
  404: '请求的内容不存在',
  408: '请求超时，请稍后重试',
  409: '该操作需要你确认后才会执行',
  413: '内容过长，请精简后重试',
  422: '内容无法处理，请检查后重试',
  429: '请求过于频繁，请稍后再试',
  500: '服务暂时不可用，请稍后重试',
  502: '服务暂时不可用，请稍后重试',
  503: '服务暂时不可用，请稍后重试',
  504: '服务响应超时，请稍后重试',
}

/** 兜底：任何未被识别的错误都到这里，绝不外泄细节。 */
const FALLBACK = '操作失败，请稍后重试'

/**
 * 判断一段文本是否含有技术痕迹。
 * 命中则说明后端这条消息没走脱敏，必须丢弃，改用兜底文案。
 */
const TECHNICAL_PATTERNS: RegExp[] = [
  /https?:\/\//i, // URL
  /\bHTTP\s*\/?\d?/i, // HTTP 状态
  /\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/, // IP
  /:\d{2,5}\b/, // 端口
  /\/api\//i, // 接口路径
  /\b(Error|Exception|Traceback|Warning)\b/, // 异常类名
  /[A-Za-z]+(Error|Exception)\b/, // URLError / KeyError
  /^\s*[\[{]/, // JSON / 数组片段
  /<[a-zA-Z/][^>]*>/, // HTML 标签
  /\.(py|json|log|txt|toml|yaml|yml)\b/i, // 文件名
  /\b(None|null|undefined|NaN|True|False)\b/, // 空值字面量
  /^[a-zA-Z0-9_\-\s:.'"()]{0,80}$/u, // 纯 ASCII（中文文案不会命中）
]

/** 文本是否可直接展示给用户。空串视为不可用。 */
export function isSafeUserText(text: unknown): text is string {
  if (typeof text !== 'string') return false
  const t = text.trim()
  if (!t) return false
  // 含中文字符才算"审校过的文案"；纯 ASCII 一律视为技术残留
  const hasCJK = /[一-龥]/.test(t)
  if (!hasCJK) return false
  return !TECHNICAL_PATTERNS.some((re) => re.test(t))
}

/** 后端返回体里挑出可安全展示的文案；挑不出返回 null。 */
function pickSafeError(payload: unknown): string | null {
  if (typeof payload === 'string' && isSafeUserText(payload)) return payload
  if (payload && typeof payload === 'object') {
    const err = (payload as Record<string, unknown>).error
    if (isSafeUserText(err)) return err
    const msg = (payload as Record<string, unknown>).message
    if (isSafeUserText(msg)) return msg
  }
  return null
}

/** 按错误码取文案。 */
function fromCode(code: unknown): string | null {
  if (typeof code !== 'string') return null
  return CODE_TEXT[code] ?? null
}

/** 按 HTTP 状态取文案。 */
function fromStatus(status: unknown): string | null {
  if (typeof status !== 'number') return null
  if (HTTP_TEXT[status]) return HTTP_TEXT[status]
  if (status >= 500) return HTTP_TEXT[500]
  if (status >= 400) return '请求未成功，请检查后重试'
  return null
}

/**
 * 把任意错误转成可安全展示的中文短句。
 *
 * 优先级：后端审校文案 → 错误码 → HTTP 状态 → 通用兜底。
 * 页面里所有 {err} 都必须先过这个函数。
 */
export function userMessage(err: unknown): string {
  // ApiError：带 path/status/body
  if (err && typeof err === 'object' && 'status' in err) {
    const e = err as { status?: unknown; code?: unknown; body?: unknown }
    // 后端审校文案最具体，优先；code/status 只是它缺失时的兜底
    return (
      pickSafeError(e.body) ??
      fromCode(e.code) ??
      fromStatus(e.status) ??
      FALLBACK
    )
  }
  // 普通 Error：message 通常含技术细节，只做安全校验
  if (err instanceof Error) {
    if (isSafeUserText(err.message)) return err.message
    return FALLBACK
  }
  // 字符串
  if (isSafeUserText(err)) return err
  return FALLBACK
}

/** 数字格式化：避免 NaN / undefined 直接上屏。 */
export function fmtNumber(v: unknown, fallback = '—'): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return fallback
  return n.toLocaleString('zh-CN')
}

/** 百分比：入参是 0-1 的小数。 */
export function fmtPercent(v: unknown, digits = 1, fallback = '—'): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return fallback
  return `${(n * 100).toFixed(digits)}%`
}

/** 金额：不带货币符号，避免臆造币种。 */
export function fmtCost(v: unknown, fallback = '—'): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return fallback
  return n >= 1 ? n.toFixed(2) : n.toFixed(4)
}

/** 时间：相对时间，避免暴露原始时间戳格式。 */
export function fmtTime(v: unknown, fallback = '—'): string {
  if (v == null || v === '' || v === 0) return fallback
  const d = v instanceof Date ? v : new Date(String(v))
  if (Number.isNaN(d.getTime())) {
    // 可能是秒级时间戳
    const n = Number(v)
    if (Number.isFinite(n) && n > 0) {
      const d2 = new Date(n > 1e12 ? n : n * 1000)
      if (!Number.isNaN(d2.getTime())) return d2.toLocaleString('zh-CN')
    }
    return fallback
  }
  return d.toLocaleString('zh-CN')
}

/** 任务/运行状态 → 中文。key 覆盖后端真实枚举值。 */
const STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  ingest: '读取中',
  extract: '提取中',
  compress: '压缩中',
  synthesize: '合成中',
  gen_eval: '生成评测',
  arena: '竞技场对比',
  finalize: '收尾中',
  succeeded: '已完成',
  done: '已完成',
  failed: '失败',
  error: '出错',
  cancelled: '已取消',
  interrupted: '已中断',
  pending: '等待中',
}

export function fmtStatus(v: unknown, fallback = '未知'): string {
  if (typeof v !== 'string') return fallback
  const key = v.toLowerCase()
  // 未收录的状态一律回落，绝不让英文枚举原样上屏
  return STATUS_TEXT[key] ?? fallback
}

/** 竞技场裁决 → 中文。未收录一律回落，绝不让 win/tie/loss 原样上屏。 */
const VERDICT_TEXT: Record<string, string> = {
  win: '蒸馏体胜出',
  tie: '双方持平',
  loss: '源 Agent 胜出',
}

export function fmtVerdict(v: unknown, fallback = '无结论'): string {
  if (typeof v !== 'string' || v === '') return fallback
  return VERDICT_TEXT[v.toLowerCase()] ?? fallback
}

/**
 * 长 id 缩写：保留首尾，中间省略。
 * 运行 id 是完整哈希，原样铺在列表里既读不出信息也撑破布局；
 * 这里只做展示层截断，完整值仍通过 title 属性保留给需要复制的场景。
 */
export function shortId(v: unknown, keep = 8, fallback = '—'): string {
  if (v == null) return fallback
  const s = String(v)
  if (s.length <= keep * 2 + 1) return s
  return `${s.slice(0, keep)}…${s.slice(-4)}`
}
