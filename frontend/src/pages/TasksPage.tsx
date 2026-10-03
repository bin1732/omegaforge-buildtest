import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { EmptyState } from "@/components/EmptyState"
import { tasksList, taskAdd, taskDone, taskDelete } from "@/lib/api"
import { userMessage, fmtTime, shortId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { ListTodo, Plus, Check, Trash2 } from "lucide-react"

/**
 * 真实响应字段（GET /api/tasks/list）：
 *   {"items":[{"id","text","done","priority","created","done_ts"}],
 *    "stats":{"total":n,"pending":n,"done":n}}
 * 新增走 POST /api/tasks/add（text 必填，priority 1~5）；
 * 完成走 POST /api/tasks/done（参数名是 task_id，不是 id）。
 */

type Task = {
  id?: string
  text?: string
  done?: boolean
  priority?: number
  created?: number
  done_ts?: number | null
  [k: string]: unknown
}

const PRIORITY_LABEL: Record<number, string> = {
  1: "紧急",
  2: "普通",
  3: "较低",
  4: "很低",
  5: "最低",
}

export function TasksPage() {
  const [items, setItems] = React.useState<Task[]>([])
  const [stats, setStats] = React.useState<{ total: number; pending: number; done: number }>({
    total: 0,
    pending: 0,
    done: 0,
  })
  const [text, setText] = React.useState("")
  const [priority, setPriority] = React.useState(2)
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [showDone, setShowDone] = React.useState(false)

  const load = React.useCallback(async () => {
    setErr(null)
    try {
      const d = (await tasksList()) as Record<string, unknown>
      const arr = Array.isArray(d?.items) ? (d.items as Task[]) : []
      setItems(arr)
      const s = (d?.stats ?? {}) as Record<string, unknown>
      setStats({
        total: typeof s.total === "number" ? s.total : arr.length,
        pending: typeof s.pending === "number" ? s.pending : arr.filter((t) => !t.done).length,
        done: typeof s.done === "number" ? s.done : arr.filter((t) => t.done).length,
      })
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  const add = React.useCallback(async () => {
    if (!text.trim()) {
      setErr("请填写任务内容")
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await taskAdd({ text: text.trim(), priority })
      setText("")
      await load()
    } catch (e) {
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [text, priority, load])

  const del = React.useCallback(
    async (id: string) => {
      setBusy(true)
      setErr(null)
      try {
        await taskDelete(id)
        await load()
      } catch (e) {
        setErr(userMessage(e))
      }
      setBusy(false)
    },
    [load],
  )

  const done = React.useCallback(
    async (id: string) => {
      setBusy(true)
      setErr(null)
      try {
        await taskDone(id)
        await load()
      } catch (e) {
        setErr(userMessage(e))
      }
      setBusy(false)
    },
    [load],
  )

  const visible = showDone ? items : items.filter((t) => !t.done)
  const pct = stats.total > 0 ? Math.round((stats.done / stats.total) * 100) : 0

  return (
    <div className="space-y-4">
      {err && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {err}
        </div>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">概览</CardTitle>
          <CardDescription className="text-xs">
            共 {stats.total} 项 · 待办 {stats.pending} · 已完成 {stats.done}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Progress value={pct} className="h-2" />
          <p className="mt-1.5 text-[11px] text-muted-foreground">完成度 {pct}%</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">新增任务</CardTitle>
          <CardDescription className="text-xs">优先级 1 最紧急，5 最低</CardDescription>
        </CardHeader>
        <CardContent className="flex items-end gap-2">
          <Input
            className="flex-1"
            placeholder="要做的事"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void add()
            }}
          />
          <div className="flex gap-1">
            {[1, 2, 3].map((p) => (
              <Button
                key={p}
                size="sm"
                variant={priority === p ? "default" : "outline"}
                onClick={() => setPriority(p)}
              >
                {PRIORITY_LABEL[p]}
              </Button>
            ))}
          </div>
          <Button onClick={() => void add()} disabled={busy}>
            <Plus className="size-4" />
            添加
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">任务列表</CardTitle>
            <Button variant="ghost" size="sm" onClick={() => setShowDone((v) => !v)}>
              {showDone ? "隐藏已完成" : "显示已完成"}
            </Button>
          </div>
          <CardDescription className="text-xs">当前显示 {visible.length} 项</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {visible.length === 0 && (
            <EmptyState icon={ListTodo} title="暂无任务" description="添加一条开始跟踪。" />
          )}
          {visible.map((t, i) => (
            <div
              key={String(t.id ?? i)}
              className={cn(
                "flex items-center gap-3 rounded-md border border-border p-3",
                t.done && "opacity-60",
              )}
            >
              <Button
                size="icon"
                variant={t.done ? "secondary" : "outline"}
                className="size-7 shrink-0"
                disabled={t.done || busy}
                onClick={() => t.id && void done(t.id)}
                title={t.done ? "已完成" : "标记完成"}
              >
                <Check className="size-3.5" />
              </Button>
              <div className="min-w-0 flex-1">
                <div className={cn("truncate text-sm", t.done && "line-through")}>
                  {String(t.text ?? "")}
                </div>
                <div className="text-[11px] text-muted-foreground">
                  {fmtTime(t.created)} · {shortId(t.id, 6)}
                </div>
              </div>
              <Badge variant="outline">{PRIORITY_LABEL[Number(t.priority ?? 2)] ?? "普通"}</Badge>
              {/* 没有删除入口时，建错一条待办就永远留在列表里：
                  后端 Tasks.delete() 早已存在，缺的只是这条出口。 */}
              <Button
                size="icon"
                variant="ghost"
                className="size-7 shrink-0"
                disabled={busy}
                onClick={() => t.id && void del(t.id)}
                title="删除"
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
