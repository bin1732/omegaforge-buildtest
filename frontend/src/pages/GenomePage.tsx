import { userMessage, fmtTime, shortId } from '@/lib/format'
import * as React from "react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Structured } from "@/components/Structured"
import { EmptyState } from "@/components/EmptyState"
import { SelectFieldSkeleton, StructuredSkeleton } from "@/components/Skeletons"
import { fetchRuns, fetchGenome, type RunItem } from "@/lib/api"
import { Dna } from "lucide-react"

export function GenomePage() {
  const [runs, setRuns] = React.useState<RunItem[]>([])
  const [loading, setLoading] = React.useState(true)
  const [sel, setSel] = React.useState<string | null>(null)
  const [data, setData] = React.useState<Record<string, unknown> | null>(null)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    fetchRuns()
      .then(setRuns)
      .catch((e) => setErr(userMessage(e)))
      .finally(() => setLoading(false))
  }, [])

  const load = (id: string) => {
    setSel(id)
    setData(null)
    setErr(null)
    fetchGenome(id)
      .then(setData)
      .catch((e) => setErr(userMessage(e)))
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>基因组</CardTitle>
          <CardDescription>解剖 Prompt 的结构组成</CardDescription>
        </CardHeader>
        <CardContent className="max-w-md space-y-2">
          {loading && runs.length === 0 ? (
            <SelectFieldSkeleton />
          ) : runs.length === 0 ? (
            <div className="flex h-9 items-center rounded-md border border-dashed border-border px-3 text-xs text-muted-foreground">
              还没有可解剖的运行记录
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
        </CardContent>
      </Card>

      {err && <p className="text-sm text-destructive">{userMessage(err)}</p>}

      {data ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">结构解析</CardTitle>
            {sel && <CardDescription>{shortId(sel)}</CardDescription>}
          </CardHeader>
          <CardContent>
            <Structured data={data} />
          </CardContent>
        </Card>
      ) : sel && !err ? (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">结构解析</CardTitle>
            <CardDescription>{shortId(sel)}</CardDescription>
          </CardHeader>
          <CardContent>
            <StructuredSkeleton rows={7} />
          </CardContent>
        </Card>
      ) : (
        !err && (
          <EmptyState
            icon={Dna}
            title="还没有解析结果"
            description="选择一次已完成的运行，即可查看它的能力基因组：人格设定、工具、约束与工作流。"
          />
        )
      )}
    </div>
  )
}
