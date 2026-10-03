import * as React from "react"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

export type LogLine = {
  t?: number
  level?: string
  msg?: string
  [k: string]: unknown
}

/**
 * 后端 /api/jobs/<id> 的 log 是**字符串数组**（形如 ["评测任务：…", …]）。
 * 若只按对象取 msg / message / text / content，字符串元素一个都取
 * 不到，于是整列日志渲染成 "—"——用户点开运行详情看到的是一屏破折号，
 * 而日志其实完好地躺在后端。
 *
 * 只有把真实响应接进界面渲染才暴露得出来：单看类型声明
 * LogLine[] 完全正常，列表页又用 `as LogLine[]` 把不匹配强转掉了。
 */
export type LogEntry = LogLine | string

/**
 * 取日志文本。
 *
 * 若写成 `l.msg ?? JSON.stringify(l)`，任何没有 msg 字段的事件都会把
 * 整个对象序列化后甩到界面上（{"level":"info","phase":"ingest",...}）。
 * 这种写法只适合排障，用户看到只会认为程序出错。这里改为按优先级取已知文本
 * 字段，都取不到时给一个中性占位，绝不把对象整体倒出来。
 */
function pickText(l: LogEntry): string {
  if (typeof l === "string") return l.trim() || "—"
  for (const k of ["msg", "message", "text", "content"] as const) {
    const v = l[k]
    if (typeof v === "string" && v.trim()) return v
  }
  return "—"
}

// 日志元素既可能是对象也可能是字符串，直接写 l.t / l.level 属于类型收窄
// 不完整，编译期就会报错（"Property 't' does not exist on type 'string'"）。
// 类型检查不通过也可能照样产出构建结果，因此这类问题容易被漏掉。
// 收成两个取值函数，类型收窄一次到位。
function timeOf(l: LogEntry): number | undefined {
  if (typeof l !== "object" || l === null) return undefined
  return typeof l.t === "number" ? l.t : undefined
}

function levelOf(l: LogEntry): string | undefined {
  if (typeof l !== "object" || l === null) return undefined
  return typeof l.level === "string" ? l.level : undefined
}

const LEVEL: Record<string, string> = {
  error: "text-destructive",
  warn: "text-[var(--budget-warn)]",
  warning: "text-[var(--budget-warn)]",
  info: "text-foreground/80",
}

export function LogStream({ lines }: { lines: LogEntry[] }) {
  const ref = React.useRef<HTMLDivElement>(null)
  const [pin, setPin] = React.useState(true)

  React.useEffect(() => {
    if (!pin || !ref.current) return
    ref.current.scrollTop = ref.current.scrollHeight
  }, [lines.length, pin])

  if (lines.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
        等待日志…
      </div>
    )
  }

  return (
    <div className="relative">
      <ScrollArea className="h-64 rounded-md bg-[var(--log-bg)] p-3">
        <div ref={ref} className="space-y-0.5 font-mono text-xs leading-relaxed">
          {lines.map((l, i) => {
            const t = timeOf(l)
            const lv = levelOf(l)
            return (
            <div key={i} className="flex gap-2">
              {t !== undefined && (
                <span className="shrink-0 text-muted-foreground tabular-nums">
                  {new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false })}
                </span>
              )}
              {lv && (
                <span
                  className={cn(
                    "shrink-0 uppercase",
                    LEVEL[lv] ?? "text-muted-foreground"
                  )}
                >
                  {lv}
                </span>
              )}
              <span className="text-[var(--log-fg)]">{pickText(l)}</span>
            </div>
            )
          })}
        </div>
      </ScrollArea>
      <button
        onClick={() => setPin((p) => !p)}
        className="absolute right-3 top-2 rounded bg-background/80 px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
      >
        {pin ? "锁定底部" : "自由滚动"}
      </button>
    </div>
  )
}
