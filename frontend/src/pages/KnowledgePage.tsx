import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { EmptyState } from "@/components/EmptyState"
import { kbList, kbAdd, kbDelete, kbSearch, rememberMemory, recallMemory, wikiList, wikiPage, wikiSave, wikiDelete } from "@/lib/api"
import { userMessage, fmtTime, shortId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { BookOpen, Brain, FileText, Search, Plus, Trash2 } from "lucide-react"

/**
 * 真实响应字段：
 *   /api/kb/list     → {"docs":[{"id","type","title","text","tags","ts"}],"total":n}
 *   /api/wiki/list   → {"pages":[...]}
 *   /api/memory/recall → {"memories":[...]}
 * 若按别的名字读（如 items / list），宽松的索引签名让编译期
 * 不报错，运行时却恒为空——页面永远显示"暂无数据"。这里统一按
 * 真实字段解析，且解析失败时显式报"返回格式异常"，不静默显示空。
 */

type Doc = {
  id?: string
  type?: string
  title?: string
  text?: string
  tags?: string[]
  ts?: number
  [k: string]: unknown
}

/** 从 unknown 响应里取出指定数组字段。取不到返回 null 以区分"空"与"格式异常"。 */
function arrayField(d: unknown, key: string): Doc[] | null {
  const o = (d ?? {}) as Record<string, unknown>
  const v = o[key]
  if (v === undefined || v === null) return []
  if (!Array.isArray(v)) return null
  return v as Doc[]
}

export function KnowledgePage() {
  const [docs, setDocs] = React.useState<Doc[]>([])
  const [pages, setPages] = React.useState<Doc[]>([])
  const [memories, setMemories] = React.useState<Doc[]>([])
  const [err, setErr] = React.useState<string | null>(null)
  const [q, setQ] = React.useState("")
  const [title, setTitle] = React.useState("")
  const [text, setText] = React.useState("")
  const [fact, setFact] = React.useState("")
  const [slug, setSlug] = React.useState("")
  const [body, setBody] = React.useState("")
  const [openWiki, setOpenWiki] = React.useState<string | null>(null)
  const [wikiText, setWikiText] = React.useState("")
  const [busy, setBusy] = React.useState(false)

  const load = React.useCallback(async (query?: string) => {
    setErr(null)
    try {
      // 知识库：有查询词走检索，否则走列表。
      //
      // 两个接口的响应字段不同：列表给 docs，检索给 results。若统一按 docs 取，
      // 检索结果会被解析成空数组——后端命中了、界面显示 0 条且不报错。
      // 取值字段必须随请求分支走，不能沿用列表那套。
      const d = query ? await kbSearch(query) : await kbList()
      const got = arrayField(d, query ? "results" : "docs")
      if (got === null) {
        setErr("知识库返回格式异常，无法解析")
        setDocs([])
      } else {
        setDocs(got)
      }
    } catch (e) {
      setErr(userMessage(e))
    }
    try {
      const w = await wikiList()
      const got = arrayField(w, "pages")
      if (got === null) setErr("词条返回格式异常，无法解析")
      else setPages(got)
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  const search = React.useCallback(async () => {
    setBusy(true)
    await load(q.trim() || undefined)
    setBusy(false)
  }, [q, load])

  const recall = React.useCallback(async () => {
    setBusy(true)
    setErr(null)
    try {
      const d = await recallMemory(q.trim() || "")
      const got = arrayField(d, "memories")
      if (got === null) setErr("记忆返回格式异常，无法解析")
      else setMemories(got)
    } catch (e) {
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [q])

  const addDoc = React.useCallback(async () => {
    if (!title.trim() || !text.trim()) {
      setErr("请填写标题与内容")
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await kbAdd({ title: title.trim(), text: text.trim() })
      setTitle("")
      setText("")
      await load()
    } catch (e) {
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [title, text, load])

  const remember = React.useCallback(async () => {
    if (!fact.trim()) {
      setErr("请填写要记住的内容")
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await rememberMemory({ fact: fact.trim() })
      setFact("")
      await recall()
    } catch (e) {
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [fact, recall])

  const delDoc = React.useCallback(
    async (id: string) => {
      setBusy(true)
      setErr(null)
      try {
        await kbDelete(id)
        await load()
      } catch (e) {
        setErr(userMessage(e))
      }
      setBusy(false)
    },
    [load],
  )

  const delWiki = React.useCallback(
    async (slug: string) => {
      setBusy(true)
      setErr(null)
      try {
        await wikiDelete(slug)
        if (openWiki === slug) setOpenWiki(null)
        await load()
      } catch (e) {
        setErr(userMessage(e))
      }
      setBusy(false)
    },
    [load, openWiki],
  )

  const saveWiki = React.useCallback(async () => {
    if (!slug.trim() || !body.trim()) {
      setErr("请填写词条标识与标题")
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await wikiSave({ slug: slug.trim(), title: slug.trim(), body: body.trim() })
      setSlug("")
      setBody("")
      await load()
    } catch (e) {
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [slug, body, load])

  const viewWiki = React.useCallback(async (name: string) => {
    setErr(null)
    try {
      const d = (await wikiPage(name)) as Record<string, unknown>
      setOpenWiki(name)
      setWikiText(String(d?.body ?? d?.text ?? JSON.stringify(d, null, 2)))
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [])

  return (
    <div className="space-y-4">
      {err && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {err}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Input
          className="flex-1"
          placeholder="检索知识库 / 记忆"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void search()
          }}
        />
        <Button variant="secondary" onClick={() => void search()} disabled={busy}>
          <Search className="size-4" />
          检索
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            setQ("")
            void load()
          }}
        >
          重置
        </Button>
      </div>

      <Tabs defaultValue="kb">
        <TabsList>
          <TabsTrigger value="kb">
            <BookOpen className="mr-1 size-3.5" />
            知识库 {docs.length}
          </TabsTrigger>
          <TabsTrigger value="memory">
            <Brain className="mr-1 size-3.5" />
            记忆
          </TabsTrigger>
          <TabsTrigger value="wiki">
            <FileText className="mr-1 size-3.5" />
            词条 {pages.length}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="kb" className="space-y-3">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">新增条目</CardTitle>
              <CardDescription className="text-xs">标题与内容均为必填</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              <Input placeholder="标题" value={title} onChange={(e) => setTitle(e.target.value)} />
              <Textarea
                className="min-h-[80px]"
                placeholder="内容"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <Button onClick={() => void addDoc()} disabled={busy}>
                <Plus className="size-4" />
                添加
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">条目列表</CardTitle>
              <CardDescription className="text-xs">共 {docs.length} 条</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {docs.length === 0 && (
                <EmptyState icon={BookOpen} title="暂无条目" description="添加后可被检索与引用。" />
              )}
              {docs.map((d, i) => (
                <div key={String(d.id ?? i)} className="rounded-md border border-border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0 truncate text-sm font-medium">
                      {String(d.title ?? "未命名")}
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {d.type && <Badge variant="outline">{String(d.type)}</Badge>}
                      <span className="text-[11px] text-muted-foreground">{fmtTime(d.ts)}</span>
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {shortId(d.id, 6)}
                      </span>
                    </div>
                  </div>
                  <p className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">
                    {String(d.text ?? "")}
                  </p>
                  {/* 加错了要能删掉：后端 KB.delete() 早已存在，
                      缺的只是这条出口。 */}
                  <div className="mt-1.5">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => d.id && void delDoc(String(d.id))}
                    >
                      <Trash2 className="mr-1 size-3.5" />
                      删除
                    </Button>
                  </div>
                  {Array.isArray(d.tags) && d.tags.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {d.tags.map((t, j) => (
                        <Badge key={j} variant="secondary" className="text-[10px]">
                          {String(t)}
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="memory" className="space-y-3">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">记住一条</CardTitle>
              <CardDescription className="text-xs">
                记忆用于长期记住事实，会参与后续检索
              </CardDescription>
            </CardHeader>
            <CardContent className="flex items-end gap-2">
              <Input
                className="flex-1"
                placeholder="例如：我叫小明"
                value={fact}
                onChange={(e) => setFact(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void remember()
                }}
              />
              <Button onClick={() => void remember()} disabled={busy}>
                记住
              </Button>
              <Button variant="secondary" onClick={() => void recall()} disabled={busy}>
                检索
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-2 p-4">
              {memories.length === 0 && (
                <EmptyState icon={Brain} title="暂无记忆" description="点上方检索可拉取已有记忆。" />
              )}
              {memories.map((m, i) => (
                <div key={String(m.id ?? i)} className="rounded-md border border-border p-3 text-sm">
                  {String(m.text ?? m.fact ?? m.title ?? "")}
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="wiki" className="space-y-3">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">保存词条</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <Input placeholder="标识（小写字母、数字与连字符）" value={slug} onChange={(e) => setSlug(e.target.value)} />
              <Textarea
                className="min-h-[80px]"
                placeholder="正文（支持 [[词条]] 反向链接）"
                value={body}
                onChange={(e) => setBody(e.target.value)}
              />
              <Button onClick={() => void saveWiki()} disabled={busy}>
                保存
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">词条列表</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {pages.length === 0 && (
                <EmptyState icon={FileText} title="暂无词条" description="保存后可在此查看。" />
              )}
              {pages.map((p, i) => {
                const name = String(p.slug ?? p.title ?? p.id ?? "")
                return (
                  <div
                    key={String(p.id ?? i)}
                    className={cn(
                      "w-full rounded-md border border-border p-3 text-left text-sm transition-colors",
                      "hover:bg-accent/50",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <button
                        className="min-w-0 flex-1 truncate text-left"
                        onClick={() => void viewWiki(name)}
                      >
                        {name}
                      </button>
                      <span className="text-[11px] text-muted-foreground">{fmtTime(p.ts)}</span>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-7 shrink-0"
                        disabled={busy}
                        onClick={() => void delWiki(name)}
                        title="删除词条"
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                  </div>
                )
              })}
            </CardContent>
          </Card>

          {openWiki && (
            <Card>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">{openWiki}</CardTitle>
                  <Button variant="ghost" size="sm" onClick={() => setOpenWiki(null)}>
                    关闭
                  </Button>
                </div>
              </CardHeader>
              <CardContent>
                <pre className="whitespace-pre-wrap text-xs">{wikiText}</pre>
              </CardContent>
            </Card>
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}
