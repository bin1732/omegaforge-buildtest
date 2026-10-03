import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { EmptyState } from "@/components/EmptyState"
import { skillsList, skillInstall, skillInvoke, fetchPersonas } from "@/lib/api"
import { userMessage } from "@/lib/format"
import { Blocks, UserRound, FolderInput } from "lucide-react"

/**
 * 真实响应字段：
 *   /api/skills/list → {"skills":[{"name","description","version",
 *                        "when_to_use","required_scopes","risk",
 *                        "type","category"}]}
 *   /api/personas    → {"categories":{"分类":[{"slug","name","description"}]},
 *                       "total":n}
 *
 * 注：type 字段曾因白名单收紧被一并滤掉，导致 /api/personas 恒返回
 * 空——界面显示"暂无人设"而不报错。此处按分类分组渲染。
 */

type Skill = {
  name?: string
  description?: string
  version?: string
  when_to_use?: string
  required_scopes?: string
  risk?: string
  type?: string
  category?: string
  [k: string]: unknown
}

type Persona = { slug?: string; name?: string; description?: string }

export function SkillsPage() {
  const [skills, setSkills] = React.useState<Skill[]>([])
  const [cats, setCats] = React.useState<Record<string, Persona[]>>({})
  const [err, setErr] = React.useState<string | null>(null)
  const [path, setPath] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [invokeOut, setInvokeOut] = React.useState<string | null>(null)

  const load = React.useCallback(async () => {
    setErr(null)
    try {
      const d = (await skillsList()) as Record<string, unknown>
      setSkills(Array.isArray(d?.skills) ? (d.skills as Skill[]) : [])
    } catch (e) {
      setErr(userMessage(e))
    }
    try {
      const p = (await fetchPersonas()) as Record<string, unknown>
      const c = (p?.categories ?? {}) as Record<string, Persona[]>
      setCats(c && typeof c === "object" ? c : {})
    } catch (e) {
      setErr(userMessage(e))
    }
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  const install = React.useCallback(async () => {
    if (!path.trim()) {
      setErr("请填写技能包路径")
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await skillInstall({ path: path.trim() })
      setPath("")
      await load()
    } catch (e) {
      // 安装失败必须是可见的：静默失败会让用户以为装好了。
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [path, load])

  const invoke = React.useCallback(async (name: string) => {
    setBusy(true)
    setErr(null)
    try {
      const d = (await skillInvoke({ name })) as Record<string, unknown>
      setInvokeOut(String(d?.prompt ?? ""))
    } catch (e) {
      setErr(userMessage(e))
    }
    setBusy(false)
  }, [])

  const personaCount = Object.values(cats).reduce((n, v) => n + (Array.isArray(v) ? v.length : 0), 0)

  return (
    <div className="space-y-4">
      {err && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {err}
        </div>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">从目录安装</CardTitle>
          <CardDescription className="text-xs">
            填写本地技能包目录的绝对路径（不支持符号链接）
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-end gap-2">
          <Input
            className="flex-1"
            placeholder="在此粘贴目录路径"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void install()
            }}
          />
          <Button onClick={() => void install()} disabled={busy}>
            <FolderInput className="size-4" />
            安装
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <Blocks className="size-4" />
            <CardTitle className="text-base">已安装技能</CardTitle>
            <Badge variant="secondary">{skills.length}</Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-2">
          {skills.length === 0 && (
            <EmptyState icon={Blocks} title="暂无技能" description="安装后可在此查看与调用。" />
          )}
          {skills.map((s, i) => (
            <div key={String(s.name ?? i)} className="rounded-md border border-border p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0 truncate text-sm font-medium">{String(s.name ?? "未命名")}</div>
                <div className="flex shrink-0 items-center gap-1.5">
                  {s.type && <Badge variant="outline">{String(s.type)}</Badge>}
                  {s.category && <Badge variant="secondary">{String(s.category)}</Badge>}
                  {s.risk && <Badge variant="outline">风险 {String(s.risk)}</Badge>}
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void invoke(String(s.name ?? ""))}>
                    调用
                  </Button>
                </div>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{String(s.description ?? "")}</p>
              {s.when_to_use && (
                <p className="mt-1 text-[11px] text-muted-foreground">
                  适用场景：{String(s.when_to_use)}
                </p>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      {invokeOut && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">调用提示词</CardTitle>
              <Button variant="ghost" size="sm" onClick={() => setInvokeOut(null)}>
                关闭
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-xs">{invokeOut}</pre>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <UserRound className="size-4" />
            <CardTitle className="text-base">人设</CardTitle>
            <Badge variant="secondary">{personaCount}</Badge>
          </div>
          <CardDescription className="text-xs">
            类型为 persona 的技能会作为人设出现
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {personaCount === 0 && (
            <EmptyState icon={UserRound} title="暂无人设" description="安装类型为人设的技能后可见。" />
          )}
          {Object.entries(cats).map(([cat, list]) => (
            <div key={cat}>
              <div className="mb-1.5 text-xs font-medium text-muted-foreground">{cat}</div>
              <div className="space-y-1.5">
                {(Array.isArray(list) ? list : []).map((p, j) => (
                  <div key={j} className="rounded-md border border-border p-2.5">
                    <div className="text-sm font-medium">{String(p.name ?? "")}</div>
                    <p className="text-xs text-muted-foreground">{String(p.description ?? "")}</p>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
