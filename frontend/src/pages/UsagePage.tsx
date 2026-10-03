import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { EmptyState } from "@/components/EmptyState"
import { fetchUsage, fetchStatus } from "@/lib/api"
import { userMessage, fmtNumber } from "@/lib/format"
import { fieldLabel, phaseLabel } from "@/lib/labels"
import { Activity, Wallet } from "lucide-react"

/**
 * 真实响应字段（GET /api/usage）：
 *   {"total":n,"entries":n,"by_phase":{},"by_model":{},
 *    "timeline":[24 个数],"bucket_seconds":300,"heatmap":{},
 *    "hottest_phase":"-"}
 *
 * 预算来自 GET /api/status 的 budget：{"limit","spent","remaining"}。
 * 用量数据里若混入一条无法解析的记录，按整体解析会让本页永久打不开，
 * 且用户无从清理——因此后端按行容错；这里额外保证：
 * 用量接口失败时只影响本页，不连带其它页面。
 */

type Usage = {
  total?: number
  entries?: number
  by_phase?: Record<string, number>
  by_model?: Record<string, number>
  timeline?: number[]
  bucket_seconds?: number
  heatmap?: Record<string, number>
  hottest_phase?: string
}

export function UsagePage() {
  const [u, setU] = React.useState<Usage | null>(null)
  const [budget, setBudget] = React.useState<{ limit: number; spent: number; remaining: number } | null>(null)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = (await fetchUsage()) as Usage
        if (alive) {
          setU(d)
          setErr(null)
        }
      } catch (e) {
        if (alive) setErr(userMessage(e))
      }
      try {
        // BackendStatus.budget 是结构化类型，直接读即可；
        // 不必强转 Record<string, unknown>（那样类型检查会报错且丢失约束）。
        const s = await fetchStatus()
        if (alive && s.budget && typeof s.budget.limit === "number") {
          setBudget({
            limit: s.budget.limit,
            spent: typeof s.budget.spent === "number" ? s.budget.spent : 0,
            remaining: typeof s.budget.remaining === "number" ? s.budget.remaining : 0,
          })
        }
      } catch {
        /* 预算取不到不影响用量展示 */
      }
    }
    void tick()
    const t = window.setInterval(tick, 10000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])

  const phases = Object.entries(u?.by_phase ?? {}).sort((a, b) => b[1] - a[1])
  const models = Object.entries(u?.by_model ?? {}).sort((a, b) => b[1] - a[1])
  const timeline = Array.isArray(u?.timeline) ? u!.timeline! : []
  const maxTl = Math.max(1, ...timeline)
  const pctOfLimit =
    budget && budget.limit > 0 ? Math.min(100, Math.round((budget.spent / budget.limit) * 100)) : 0

  return (
    <div className="space-y-4">
      {err && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {err}
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs">{fieldLabel("total")}</CardDescription>
            <CardTitle className="text-2xl">{fmtNumber(u?.total ?? 0)}</CardTitle>
          </CardHeader>
          <CardContent className="text-[11px] text-muted-foreground">累计消耗 Token</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs">{fieldLabel("entries")}</CardDescription>
            <CardTitle className="text-2xl">{fmtNumber(u?.entries ?? 0)}</CardTitle>
          </CardHeader>
          <CardContent className="text-[11px] text-muted-foreground">记账条目数</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs">{fieldLabel("hottest_phase")}</CardDescription>
            <CardTitle className="text-2xl">
              {u?.hottest_phase ? phaseLabel(u.hottest_phase) : "—"}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-[11px] text-muted-foreground">消耗最多的阶段</CardContent>
        </Card>
      </div>

      {budget && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Wallet className="size-4" />
              <CardTitle className="text-base">{fieldLabel("limit")}</CardTitle>
            </div>
            <CardDescription className="text-xs">
              已用 {fmtNumber(budget.spent)} / 上限 {fmtNumber(budget.limit)} · 剩余{" "}
              {fmtNumber(budget.remaining)}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Progress value={pctOfLimit} className="h-2" />
            <p className="mt-1.5 text-[11px] text-muted-foreground">已用 {pctOfLimit}%</p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <Activity className="size-4" />
            <CardTitle className="text-base">{fieldLabel("timeline")}</CardTitle>
          </div>
          <CardDescription className="text-xs">
            近 {timeline.length} 个区间，每区间 {String(u?.bucket_seconds ?? "—")} 秒
          </CardDescription>
        </CardHeader>
        <CardContent>
          {timeline.length === 0 ? (
            <EmptyState icon={Activity} title="暂无用量" description="跑一次蒸馏或对话后可见。" />
          ) : (
            <div className="flex h-24 items-end gap-1">
              {timeline.map((v, i) => (
                <div
                  key={i}
                  className="flex-1 rounded-t bg-[var(--primary-emphasis)]/70"
                  style={{ height: `${Math.max(2, (v / maxTl) * 100)}%` }}
                  title={`${fmtNumber(v)} tokens`}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{fieldLabel("by_phase")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5">
            {phases.length === 0 && (
              <p className="text-xs text-muted-foreground">暂无数据</p>
            )}
            {phases.map(([k, v]) => (
              <div key={k} className="flex items-center justify-between text-sm">
                <span className="truncate">{phaseLabel(k)}</span>
                <Badge variant="secondary">{fmtNumber(v)}</Badge>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{fieldLabel("by_model")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5">
            {models.length === 0 && <p className="text-xs text-muted-foreground">暂无数据</p>}
            {models.map(([k, v]) => (
              <div key={k} className="flex items-center justify-between text-sm">
                <span className="truncate font-mono text-xs">{k}</span>
                <Badge variant="secondary">{fmtNumber(v)}</Badge>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
