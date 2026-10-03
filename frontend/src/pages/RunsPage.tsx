import { userMessage, fmtStatus, fmtTime, shortId } from '@/lib/format'
import * as React from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { LogStream, type LogEntry } from "@/components/LogStream"
import { Structured } from "@/components/Structured"
import { EmptyState } from "@/components/EmptyState"
import { RunListSkeleton, DetailSkeleton } from "@/components/Skeletons"
import { fetchRuns, fetchJob, fetchCompare, type RunItem, type JobDetail } from "@/lib/api"
import { cn } from "@/lib/utils"
import { History } from "lucide-react"

const TONE: Record<string, string> = {
  succeeded: "text-[var(--verdict-win)]",
  done: "text-[var(--verdict-win)]",
  failed: "text-destructive",
  error: "text-destructive",
  running: "text-[var(--phase-active)]",
  queued: "text-muted-foreground",
}

const TERMINAL = new Set(["succeeded", "done", "failed", "error", "cancelled", "interrupted"])

/**
 * 取任务文本：后端给在 meta.task。
 *
 * 列表与详情若都不显示它，用户看到的就是一串运行编号加状态和时间，
 * 跑过几次之后根本无法区分哪次跑的是哪个任务，点开详情也只有日志和报告，
 * 同样没有任务。这是"能不能用"的问题，不只是对错问题。
 *
 * 这个缺口是怎么被发现的：一条渲染断言"点开详情后渲染出真实任务文本"基线
 * 通过，注入日志退化后它**跟着失败**——查下去才发现它通过的原因是
 * **日志第一行恰好包含任务文本**（"评测任务：联调验证：…"），
 * 即断言被同源文本"蹭"过去了。断言意外失败比一直通过更有价值。
 */
const taskOf = (r: unknown): string => {
  const m = (r as { meta?: unknown } | null | undefined)?.meta
  if (!m || typeof m !== "object") return ""
  const t = (m as Record<string, unknown>).task
  return typeof t === "string" ? t.trim() : ""
}

export function RunsPage() {
  const [runs, setRuns] = React.useState<RunItem[]>([])
  const [loading, setLoading] = React.useState(true)
  const [sel, setSel] = React.useState<string | null>(null)
  const [detail, setDetail] = React.useState<JobDetail | null>(null)
  const [err, setErr] = React.useState<string | null>(null)
  // 跨版本比对：a 为基准侧，b 为对照侧。
  const [cmpBase, setCmpBase] = React.useState<string | null>(null)
  const [cmpWith, setCmpWith] = React.useState<string | null>(null)
  const [cmpData, setCmpData] = React.useState<Record<string, unknown> | null>(null)
  const [cmpBusy, setCmpBusy] = React.useState(false)

  const load = React.useCallback(() => {
    setLoading(true)
    fetchRuns()
      .then((r) => {
        setRuns(r)
        setErr(null)
      })
      .catch((e) => setErr(userMessage(e)))
      .finally(() => setLoading(false))
  }, [])

  React.useEffect(load, [load])

  // 选好两条就发比对请求。base 先选、当前详情那条作为新版，
  // 这样"比上一版强了多少"的方向不会由用户去猜。
  React.useEffect(() => {
    if (!cmpBase || !cmpWith || cmpBase === cmpWith) {
      setCmpData(null)
      return
    }
    let alive = true
    setCmpBusy(true)
    fetchCompare(cmpBase, cmpWith)
      .then((d) => {
        if (!alive) return
        setCmpData(d)
        setErr(null)
      })
      .catch((e) => {
        if (!alive) return
        setCmpData(null)
        setErr(userMessage(e))
      })
      .finally(() => {
        if (alive) setCmpBusy(false)
      })
    return () => {
      alive = false
    }
  }, [cmpBase, cmpWith])

  const candidates = React.useMemo(
    () => runs.filter((r) => TERMINAL.has(String(r.status)) && r.id !== cmpBase),
    [runs, cmpBase])

  React.useEffect(() => {
    if (!sel) return
    let alive = true
    const tick = async () => {
      try {
        const j = await fetchJob(sel)
        if (!alive) return
        setDetail(j)
        if (TERMINAL.has(String(j.status))) return
      } catch (e) {
        if (alive) setErr(userMessage(e))
        return
      }
      if (alive) window.setTimeout(tick, 1500)
    }
    void tick()
    return () => {
      alive = false
    }
  }, [sel])

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
      <Card className="flex min-h-0 flex-col">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">运行记录</CardTitle>
            <Button variant="ghost" size="sm" onClick={load}>
              刷新
            </Button>
          </div>
        </CardHeader>
        <CardContent className="min-h-0 flex-1 p-0">
          {loading && runs.length === 0 ? (
            <RunListSkeleton />
          ) : runs.length === 0 ? (
            <div className="p-3">
              <EmptyState
                icon={History}
                title="暂无运行记录"
                description="在蒸馏工坊跑一次之后，这里会留下完整的事件流，可以随时回看。"
              />
            </div>
          ) : (
            <ScrollArea className="h-[60vh]">
              <ul className="space-y-1 p-3 pt-0">
                {runs.map((r) => (
                  <li key={r.id}>
                    {/* 外层不能是 button：里面还嵌着一个"设为基准"按钮。
                        HTML 不允许 button 嵌套 button，浏览器解析时会强行闭合外层，
                        把内层提到外面——实际 DOM 与 React 认为的结构不一致，
                        点击落不到处理函数上，用户看到的是"点了没反应"。
                        改用可聚焦容器，键盘可达性靠 role 与 tabIndex 保留。 */}
                    <div
                      onClick={() => {
                        // 切换时先清空旧详情，否则会短暂显示上一条的内容
                        setDetail(null)
                        setSel(r.id)
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault()
                          setDetail(null)
                          setSel(r.id)
                        }
                      }}
                      role="button"
                      tabIndex={0}
                      aria-current={sel === r.id ? "true" : undefined}
                      title={r.id}
                      className={cn(
                        "w-full rounded-md px-3 py-2 text-left transition-colors",
                        sel === r.id ? "bg-accent text-accent-foreground" : "hover:bg-accent/50"
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs">{shortId(r.id)}</span>
                        <span className={cn("shrink-0 text-[11px]", TONE[String(r.status)] ?? "")}>
                          {fmtStatus(r.status)}
                        </span>
                      </div>
                      {TERMINAL.has(String(r.status)) && (
                        <div className="mt-1 flex items-center gap-1">
                          <button
                            type="button"
                            data-testid="run-as-baseline"
                            onClick={(e) => {
                              e.stopPropagation()
                              setCmpBase(r.id)
                              setCmpWith(null)
                            }}
                            className={cn(
                              "rounded border px-1.5 py-0.5 text-[11px] transition-colors",
                              cmpBase === r.id
                                ? "border-primary bg-primary/10 text-primary"
                                : "border-border text-muted-foreground hover:bg-accent"
                            )}
                          >
                            {cmpBase === r.id ? "已设为基准" : "设为基准"}
                          </button>
                        </div>
                      )}
                      {/* 任务文本排在时间之前：区分两次运行靠的是"跑的什么"，
                          不是"几点跑的"。 */}
                      {taskOf(r) && (
                        <div className="mt-0.5 truncate text-[11px] text-foreground/80">
                          {taskOf(r)}
                        </div>
                      )}
                      {r.created_ts !== undefined && (
                        <div className="mt-0.5 text-[11px] text-muted-foreground">
                          {fmtTime(r.created_ts)}
                        </div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </ScrollArea>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">详情</CardTitle>
            {detail && <Badge variant="secondary">{fmtStatus(detail.status)}</Badge>}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {err && <p className="text-sm text-destructive">{userMessage(err)}</p>}

          {/* 比对入口放在详情顶部而不是藏在菜单里：
              后端早有 /api/compare，但界面若不给入口，能力等于不存在——
              用户不会为了看"本版比上一版强了多少"去翻命令行。 */}
          {cmpBase && (
            <div
              data-testid="compare-bar"
              className="rounded-md border bg-muted/40 px-3 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-muted-foreground">
                  基准：<span className="font-mono">{shortId(cmpBase)}</span>
                </span>
                {candidates.length === 0 ? (
                  <span className="text-muted-foreground">
                    暂无其他已完成的运行可比对
                  </span>
                ) : (
                  <select
                    data-testid="compare-target"
                    className="rounded border bg-background px-2 py-1 text-xs"
                    value={cmpWith ?? ""}
                    onChange={(e) => setCmpWith(e.target.value || null)}
                  >
                    <option value="">选择要比对的运行…</option>
                    {candidates.map((r) => (
                      <option key={r.id} value={r.id}>
                        {shortId(r.id)}
                        {taskOf(r) ? ` · ${taskOf(r)}` : ""}
                      </option>
                    ))}
                  </select>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setCmpBase(null)
                    setCmpWith(null)
                    setCmpData(null)
                  }}
                >
                  清除
                </Button>
              </div>
              {cmpBusy && (
                <p className="mt-1 text-muted-foreground">正在比对…</p>
              )}
              {!cmpBusy && cmpData && (
                <div data-testid="compare-result" className="mt-2">
                  <Structured data={cmpData} />
                </div>
              )}
              {!cmpBusy && !cmpData && cmpWith && (
                <p className="mt-1 text-muted-foreground">暂无比对结果</p>
              )}
            </div>
          )}
          {!err && sel && !detail && <DetailSkeleton />}
          {!err && !sel && !detail && (
            <EmptyState
              icon={History}
              title="选择左侧一条记录"
              description="这里会显示该次运行的完整事件流与最终报告。"
            />
          )}
          {detail && (
            <>
              {taskOf(detail) && (
                // data-testid 只用于渲染校验定位；不加的话校验只能查整页文本，
                // 而列表项同样显示任务，去掉详情这一处校验仍会全部通过。
                <div data-testid="run-task" className="rounded-md border bg-muted/40 px-3 py-2 text-sm">
                  <span className="text-muted-foreground">任务：</span>
                  <span className="break-all">{taskOf(detail)}</span>
                </div>
              )}
              <LogStream lines={(detail.log ?? []) as LogEntry[]} />
              <Structured data={detail.report} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
