import { userMessage, fmtVerdict, fmtTime, shortId } from '@/lib/format'
import * as React from "react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Structured } from "@/components/Structured"
import { EmptyState } from "@/components/EmptyState"
import { SelectFieldSkeleton, StructuredSkeleton } from "@/components/Skeletons"
import { fetchRuns, fetchReport, type RunItem } from "@/lib/api"
import { Swords } from "lucide-react"

/**
 * 诚实展示：竞技场结论带成立标记。
 * 当对照组无法拿到源 Agent 原始提示词时，后端会降级为简化对照
 * 并把成立标记置为 false —— 此时“更强”不成立，界面必须说清，
 * 而不是照旧挂一个胜利徽章。
 */
export function ArenaPage() {
  const [runs, setRuns] = React.useState<RunItem[]>([])
  const [loading, setLoading] = React.useState(true)
  const [sel, setSel] = React.useState<string | null>(null)
  const [report, setReport] = React.useState<Record<string, unknown> | null>(null)
  const [err, setErr] = React.useState<string | null>(null)

  const refresh = React.useCallback(() => {
    setLoading(true)
    fetchRuns()
      .then(setRuns)
      .catch((e) => setErr(userMessage(e)))
      .finally(() => setLoading(false))
  }, [])

  React.useEffect(refresh, [refresh])

  const load = (id: string) => {
    setSel(id)
    setReport(null)
    setErr(null)
    fetchReport(id).then(setReport).catch((e) => setErr(userMessage(e)))
  }

  const arena = (report?.arena ?? report) as Record<string, unknown> | null
  const verdict = arena ? String(arena.verdict ?? "") : ""
  const valid = arena ? arena.claim_valid !== false : false
  // 结论不成立的原因以后端返回的说明为准，它会针对具体情况给出
  // 对应的处理办法（例如更换裁判模型、提供自有考题、重新运行）。
  // 只有在后端未给出说明时，才退回下面这句通用描述。
  const trustNote =
    typeof report?.trust_note === "string" ? report.trust_note : ""
  const baselineNote =
    typeof report?.baseline_note === "string" ? report.baseline_note : ""
  const reason = trustNote || baselineNote || ""

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>竞技场</CardTitle>
          <CardDescription>蒸馏体与源 Agent 的同题对照裁决</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="min-w-64 space-y-2">
            <label className="text-xs font-medium text-muted-foreground">选择一次运行</label>
            {loading && runs.length === 0 ? (
              <SelectFieldSkeleton withLabel={false} />
            ) : runs.length === 0 ? (
              <div className="flex h-9 items-center rounded-md border border-dashed border-border px-3 text-xs text-muted-foreground">
                还没有可对照的运行记录
              </div>
            ) : (
              <Select value={sel ?? ""} onValueChange={load}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="选择运行…" />
                </SelectTrigger>
                <SelectContent>
                  {runs.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {shortId(r.id)} · {fmtTime(r.created_ts, "—")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          {verdict && (
            <div className="space-y-1 pb-2">
              <Badge variant={valid ? "default" : "secondary"}>{fmtVerdict(verdict)}</Badge>
              {!valid && <div className="text-[11px] text-muted-foreground">结论仅供参考</div>}
            </div>
          )}
          {runs.length > 0 && (
            <Button variant="ghost" size="sm" onClick={refresh}>
              刷新列表
            </Button>
          )}
        </CardContent>
      </Card>

      {arena && valid === false && (
        <Alert variant="destructive">
          <AlertTitle>该结论不可用于宣称“更强”</AlertTitle>
          <AlertDescription>
            {reason ||
              "对照组未取到源 Agent 的原始提示词，已改用简化对照。这种情况下提炼结果更容易胜出，属于实验设计造成的偏差，不能作为可验证的结论。"}
          </AlertDescription>
        </Alert>
      )}

      {report ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">对照报告</CardTitle>
            {sel && <CardDescription>{shortId(sel)}</CardDescription>}
          </CardHeader>
          <CardContent>
            <Structured data={report} />
          </CardContent>
        </Card>
      ) : sel && !err ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">对照报告</CardTitle>
            <CardDescription>{shortId(sel)}</CardDescription>
          </CardHeader>
          <CardContent>
            <StructuredSkeleton rows={7} />
          </CardContent>
        </Card>
      ) : (
        !err && (
          <EmptyState
            icon={Swords}
            title="选择一次运行以查看对照结果"
            description="竞技场把蒸馏体与源 Agent 放在同一个任务上对照评分，给出裁决与理由。"
          />
        )
      )}

      {err && (
        <Card>
          <CardContent className="pt-6">
            <Button variant="outline" onClick={() => runs[0] && load(runs[0].id)}>
              重试
            </Button>
            <p className="mt-2 text-sm text-destructive">{userMessage(err)}</p>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
