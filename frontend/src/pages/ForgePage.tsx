import { userMessage, fmtStatus } from '@/lib/format'
import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { PhaseBar } from "@/components/PhaseBar"
import { LogStream, type LogEntry } from "@/components/LogStream"
import { Structured } from "@/components/Structured"
import { EmptyState } from "@/components/EmptyState"
import { SelectFieldSkeleton } from "@/components/Skeletons"
import { fetchProviders, startDistill, fetchJob } from "@/lib/api"
import { FlaskConical } from "lucide-react"

const POLL_MS = 1200

export function ForgePage() {
  const [source, setSource] = React.useState("")
  const [task, setTask] = React.useState("")
  // 当前生效的主模型（只读展示）。若做成下拉框并把选中值放进请求里，
  // 后端并不接收该字段，控件点了不会有任何效果。
  const [activeModel, setActiveModel] = React.useState("")
  // 加载失败若静默置空，用户分不清"还在加载"和"真的没有"。
  const [loadingModels, setLoadingModels] = React.useState(true)
  const [budget, setBudget] = React.useState("")
  const [jobId, setJobId] = React.useState<string | null>(null)
  const [log, setLog] = React.useState<LogEntry[]>([])
  const [phase, setPhase] = React.useState<string | undefined>(undefined)
  const [status, setStatus] = React.useState<string | undefined>(undefined)
  const [result, setResult] = React.useState<unknown>(null)
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    fetchProviders()
      .then((p) => {
        // 当前生效的主模型：优先取 current.models.main（后端真实生效配置），
        // 取不到再回退到当前供应商预设的 main。只读展示，不参与提交。
        const cur = (p.current?.models ?? {}) as Record<string, unknown>
        const list = p.presets ?? []
        const first = list.find((x) => (x.id ?? x.name) === p.active) ?? list[0]
        const presetMain = (first?.models as Record<string, unknown> | undefined)?.main
        setActiveModel(String(cur.main ?? presetMain ?? ""))
      })
      .catch(() => setActiveModel(""))
      .finally(() => setLoadingModels(false))
  }, [])

  // 轮询：后端 Run 事件流已即时落盘，这里每 1.2s 拉一次，
  // 日志随到随渲染 —— 不再是"结束才一次性刷出"。
  React.useEffect(() => {
    if (!jobId) return
    let alive = true
    const tick = async () => {
      try {
        const j = await fetchJob(jobId)
        if (!alive) return
        setStatus(j.status)
        // 真实只有一个 phase（字符串）。若再回退到 j.phases（数组），
        // 后端从不返回该字段，Array.isArray 恒为假，是条永远走不到的路径。
        setPhase((j as Record<string, unknown>).phase as string | undefined)
        setLog(Array.isArray(j.log) ? (j.log as LogEntry[]) : [])
        if (j.report !== undefined) setResult(j.report)
        const s = String(j.status)
        if (s === "succeeded" || s === "done" || s === "failed" || s === "error") {
          setBusy(false)
          return
        }
      } catch (e) {
        if (!alive) return
        setErr(userMessage(e))
        setBusy(false)
        return
      }
      if (alive) window.setTimeout(tick, POLL_MS)
    }
    void tick()
    return () => {
      alive = false
    }
  }, [jobId])

  const run = async () => {
    setErr(null)
    setResult(null)
    setLog([])
    setBusy(true)
    try {
      // 启动蒸馏只接受 source_prompt/source、task、budget、
      // rounds、gens、eval_set —— **不接受 model**。模型来自全局配置，
      // 在「模型供应商」页统一设置。若照发 model，后端会静默丢弃，
      // 界面上的模型下拉框点了不会有任何效果。
      // 这里不发送不存在的字段；模型改为只读展示（见下方 JSX）。
      const payload: Record<string, unknown> = { source_prompt: source, task }
      if (budget) payload.budget = Number(budget)
      // 真实返回 {job, run} —— 不是 job_id
      const r = await startDistill(payload)
      setJobId(r.job)
    } catch (e) {
      setErr(userMessage(e))
      setBusy(false)
    }
  }

  const running = busy && !!jobId

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      <Card>
        <CardHeader>
          <CardTitle>蒸馏工坊</CardTitle>
          <CardDescription>把源 Agent 的能力提炼成更省 Token 的蒸馏体</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="src">源 Agent 提示词</Label>
            <Textarea
              id="src"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="直接粘贴源 Agent 的系统提示词…"
              className="min-h-40 font-mono text-xs"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="task">评测任务</Label>
            <Textarea
              id="task"
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder="描述一个用于对照评测的真实任务…"
              className="min-h-20"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label>模型（只读）</Label>
              {/* 只读展示：后端 distill 不接受 model 参数，模型由
                  「模型供应商」页统一配置。留一个能点的下拉框，
                  等于承诺一个后端并不支持的能力。 */}
              {loadingModels ? (
                <SelectFieldSkeleton withLabel={false} />
              ) : (
                <p className="pt-2 font-mono text-xs text-muted-foreground">
                  {activeModel || "未获取到，将使用后端默认模型"}
                </p>
              )}
              <p className="text-[11px] text-muted-foreground">
                如需更换，请在「模型供应商」页切换
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="budget">预算上限（Token）</Label>
              <Input
                id="budget"
                inputMode="numeric"
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
                placeholder="留空用默认"
              />
            </div>
          </div>
          <Button
            className="w-full"
            onClick={run}
            disabled={busy || source.trim().length === 0}
          >
            {running ? "蒸馏中…" : "开始蒸馏"}
          </Button>
          {err && (
            <Alert variant="destructive">
              <AlertTitle>失败</AlertTitle>
              <AlertDescription className="break-words">{userMessage(err)}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      <div className="space-y-4">
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">运行进度</CardTitle>
              {status && <Badge variant="secondary">{fmtStatus(status)}</Badge>}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {!jobId ? (
              // 未开始时只摆七个灰阶段点，用户看不出该做什么。
              // 先给一句引导，开始之后才切到真实的阶段与日志。
              <EmptyState
                icon={FlaskConical}
                title="尚未开始蒸馏"
                description="在左侧填入源 Agent 提示词 与评测任务，点「开始蒸馏」后，这里会实时显示七个阶段与事件流。"
              />
            ) : (
              <>
                <PhaseBar current={phase} status={status} />
                <LogStream lines={log} />
              </>
            )}
          </CardContent>
        </Card>

        {result !== null && (
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">蒸馏报告</CardTitle>
            </CardHeader>
            <CardContent>
              <Structured data={result} />
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  )
}
