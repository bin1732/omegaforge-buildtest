import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { ScrollArea } from "@/components/ui/scroll-area"
import { EmptyState } from "@/components/EmptyState"
import {
  fetchConversations,
  newConversation,
  deleteConversation,
  sendChat,
  streamChat,
  setConversationModel,
  type Conversation,
} from "@/lib/api"
import { userMessage, fmtTime, shortId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { MessagesSquare, Plus, Trash2, Send, Square } from "lucide-react"

type Msg = {
  role: "user" | "assistant"
  content: string
  /** 后端回传的元信息，仅助手消息有 */
  model?: string
  routing?: string
  latency_ms?: number
  pending?: boolean
}

/**
 * 解析 SSE 事件流。
 *
 * 后端事件形状：
 *   {"type":"start","conversation_id":..., "model":..., "routing":...}
 *   {"type":"delta","text":"..."}
 *   {"type":"done","finish_reason":...}
 *   {"type":"error","error":"..."}
 *
 * 关键：done 必须被消费。若不处理它，界面上的加载状态
 * 永远不结束——用户会一直看着转圈。error 与 done 成对出现时
 * 都要处理，且 done 上的 error 字段不能丢。
 */
async function readStream(
  res: Response,
  onDelta: (t: string) => void,
  onMeta: (m: { conversation_id?: string; model?: string; routing?: string }) => void,
  onError: (e: string) => void,
): Promise<{ finish_reason?: string; error?: string }> {
  const reader = res.body?.getReader()
  if (!reader) throw new Error("浏览器不支持流式读取")
  const dec = new TextDecoder()
  let buf = ""
  let out: { finish_reason?: string; error?: string } = {}
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    // SSE 以空行分隔事件
    let i: number
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, i)
      buf = buf.slice(i + 2)
      const line = raw.split("\n").find((l) => l.startsWith("data:")) || ""
      const payload = line.slice(5).trim()
      if (!payload) continue
      let o: Record<string, unknown>
      try {
        o = JSON.parse(payload)
      } catch {
        continue
      }
      const type = String(o.type || "")
      if (type === "start") {
        onMeta({
          conversation_id: typeof o.conversation_id === "string" ? o.conversation_id : undefined,
          model: typeof o.model === "string" ? o.model : undefined,
          routing: typeof o.routing === "string" ? o.routing : undefined,
        })
      } else if (type === "delta") {
        const t = typeof o.text === "string" ? o.text : ""
        if (t) onDelta(t)
      } else if (type === "error") {
        const e = typeof o.error === "string" ? o.error : ""
        if (e) onError(e)
      } else if (type === "done") {
        // done 上的 error 也要带出去：后端在 error 分支里
        // 同时推 error 与 done，只认 error 会漏掉兜底失败那一档。
        out = {
          finish_reason: typeof o.finish_reason === "string" ? o.finish_reason : undefined,
          error: typeof o.error === "string" ? o.error : undefined,
        }
      }
    }
  }
  return out
}

/**
 * 会话偏好模型的切换入口。
 *
 * 后端 /api/conversations/model 是真实实现。少了这一层入口，用户只能在
 * 徽章上"看到"当前模型却无从更换——能力存在而入口缺失。
 * 模型清单随供应商而异，这里让用户直接填模型标识，不做猜测式下拉：
 * 猜错的下拉会给出一个能选却用不了的列表。
 */
function ModelPicker({ value, onPick }: { value: string; onPick: (m: string) => void }) {
  const [open, setOpen] = React.useState(false)
  const [draft, setDraft] = React.useState(value)
  if (!open) {
    return (
      <Button variant="ghost" size="sm" onClick={() => { setDraft(value); setOpen(true) }}>
        切换模型
      </Button>
    )
  }
  const submit = () => {
    const m = draft.trim()
    if (!m) return
    onPick(m)
    setOpen(false)
  }
  return (
    <div className="flex items-center gap-1">
      <Input
        className="h-7 w-40 text-xs"
        value={draft}
        placeholder="模型标识"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit() }}
      />
      <Button variant="outline" size="sm" onClick={submit}>确定</Button>
      <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>取消</Button>
    </div>
  )
}

export function ChatPage() {
  const [convs, setConvs] = React.useState<Conversation[]>([])
  const [cur, setCur] = React.useState<string>("")
  const [msgs, setMsgs] = React.useState<Msg[]>([])
  const [draft, setDraft] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [err, setErr] = React.useState<string | null>(null)
  const [meta, setMeta] = React.useState<{ model?: string; routing?: string }>({})
  const abortRef = React.useRef<AbortController | null>(null)

  const load = React.useCallback(async () => {
    try {
      setConvs(await fetchConversations())
      setErr(null)
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  const open = React.useCallback(async (id: string) => {
    setCur(id)
    setMsgs([])
    setMeta({})
    try {
      const list = await fetchConversations()
      const c = list.find((x) => x.id === id)
      const raw = Array.isArray(c?.messages) ? c!.messages! : []
      setMsgs(
        raw.map((m) => ({
          role: (m.role === "assistant" ? "assistant" : "user") as Msg["role"],
          content: String(m.content ?? ""),
        })),
      )
      setErr(null)
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [])

  const create = React.useCallback(async () => {
    try {
      const c = await newConversation()
      await load()
      if (c?.id) {
        setCur(c.id)
        setMsgs([])
      }
      setErr(null)
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [load])

  const remove = React.useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id)
        if (cur === id) {
          setCur("")
          setMsgs([])
        }
        await load()
        setErr(null)
      } catch (e) {
        setErr(userMessage(e))
      }
    },
    [cur, load],
  )

  const stop = React.useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setBusy(false)
  }, [])

  const send = React.useCallback(async () => {
    const text = draft.trim()
    if (!text || busy) return
    setDraft("")
    setErr(null)
    setBusy(true)
    setMsgs((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: "", pending: true }])

    const patch = (fn: (m: Msg) => Msg) =>
      setMsgs((prev) => {
        const next = prev.slice()
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === "assistant") {
            next[i] = fn(next[i])
            break
          }
        }
        return next
      })

    // 优先流式；后端不可用时回退到一次性接口。
    try {
      const res = await streamChat({ message: text, conversation_id: cur || undefined })
      if (!res.ok) throw new Error(`连接失败（${res.status}）`)
      const out = await readStream(
        res,
        (t) => patch((m) => ({ ...m, content: m.content + t })),
        (m) => {
          if (m.conversation_id) setCur(m.conversation_id)
          setMeta({ model: m.model, routing: m.routing })
        },
        (e) => setErr(e),
      )
      patch((m) => ({ ...m, pending: false, model: meta.model, routing: meta.routing }))
      if (out.error) setErr(out.error)
      void load()
    } catch (e) {
      // 流式失败（CORS / 网络 / 服务端不支持）时不能什么都不做，
      // 否则用户只看到一条永远转圈的助手消息。
      try {
        const r = await sendChat({ message: text, conversation_id: cur || undefined })
        const reply = String((r as Record<string, unknown>)?.reply ?? "")
        patch((m) => ({ ...m, content: reply, pending: false }))
        const cid = (r as Record<string, unknown>)?.conversation_id
        if (typeof cid === "string" && cid) setCur(cid)
        void load()
      } catch (e2) {
        patch((m) => ({ ...m, pending: false, content: "" }))
        setErr(userMessage(e2))
      }
    } finally {
      setBusy(false)
    }
  }, [draft, busy, cur, load, meta.model, meta.routing])

  return (
    <div className="grid gap-4 lg:grid-cols-[240px_minmax(0,1fr)]">
      <Card className="h-fit">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">对话</CardTitle>
            <Button size="icon" variant="ghost" onClick={() => void create()} title="新建对话">
              <Plus className="size-4" />
            </Button>
          </div>
          <CardDescription className="text-xs">共 {convs.length} 条</CardDescription>
        </CardHeader>
        <CardContent className="space-y-1 p-2">
          {convs.length === 0 && (
            <p className="px-2 py-3 text-xs text-muted-foreground">还没有对话，点上方加号新建。</p>
          )}
          {convs.map((c) => (
            <div
              key={c.id}
              className={cn(
                "group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm",
                cur === c.id ? "bg-accent text-accent-foreground" : "hover:bg-accent/50",
              )}
            >
              <button className="min-w-0 flex-1 text-left" onClick={() => void open(c.id)}>
                <div className="truncate">{String(c.title ?? "未命名")}</div>
                <div className="text-[11px] text-muted-foreground">
                  {fmtTime(c.updated)} · {shortId(c.id, 6)}
                </div>
              </button>
              <Button
                size="icon"
                variant="ghost"
                className="size-6 shrink-0 opacity-0 group-hover:opacity-100"
                onClick={() => void remove(c.id)}
                title="删除"
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="space-y-3">
        {err && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {err}
          </div>
        )}
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">
                {cur ? `对话 ${shortId(cur, 6)}` : "新对话"}
              </CardTitle>
              <div className="flex items-center gap-1.5">
                {cur && (
                  <ModelPicker
                    value={meta.model ?? ""}
                    onPick={(m) => {
                      setConversationModel(cur, m)
                        .then(() => setMeta((prev) => ({ ...prev, model: m })))
                        .catch((e) => setErr(userMessage(e)))
                    }}
                  />
                )}
                {meta.model && <Badge variant="secondary">{meta.model}</Badge>}
                {meta.routing && (
                  <Badge variant="outline" className="max-w-[220px] truncate" title={meta.routing}>
                    {meta.routing}
                  </Badge>
                )}
              </div>
            </div>
          </CardHeader>
          <Separator />
          <CardContent className="p-0">
            <ScrollArea className="h-[420px]">
              <div className="space-y-3 p-4">
                {msgs.length === 0 && (
                  <EmptyState
                    icon={MessagesSquare}
                    title="还没开始"
                    description="在下方输入内容开始对话。"
                  />
                )}
                {msgs.map((m, i) => (
                  <div
                    key={i}
                    className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}
                  >
                    <div
                      className={cn(
                        "max-w-[80%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm",
                        m.role === "user"
                          ? "bg-[var(--primary-emphasis)] text-primary-foreground"
                          : "bg-muted",
                      )}
                    >
                      {m.content ||
                        (m.pending ? (
                          <span className="animate-pulse text-muted-foreground">思考中…</span>
                        ) : null)}
                    </div>
                  </div>
                ))}
              </div>
            </ScrollArea>
          </CardContent>
        </Card>

        <div className="flex items-end gap-2">
          <Textarea
            className="min-h-[64px] flex-1"
            placeholder="输入消息，回车发送（Shift+回车换行）"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
          />
          {busy ? (
            <Button onClick={stop} variant="secondary">
              <Square className="size-4" />
              停止
            </Button>
          ) : (
            <Button onClick={() => void send()} disabled={!draft.trim()}>
              <Send className="size-4" />
              发送
            </Button>
          )}
        </div>
        <p className="text-[11px] text-muted-foreground">
          模型由「设置」页统一配置；此处切换的仅为当前对话的偏好（后端按可用性自动裁决）。
        </p>
      </div>
    </div>
  )
}
