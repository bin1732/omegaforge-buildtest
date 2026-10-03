import { userMessage, fmtNumber } from '@/lib/format'
import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Progress } from "@/components/ui/progress"
import { Structured } from "@/components/Structured"
import { EmptyState } from "@/components/EmptyState"
import { VoicePanel, PermissionsPanel, AuditPanel } from "@/components/VoicePanel"
import { SelectFieldSkeleton, BudgetSkeleton } from "@/components/Skeletons"
import { fetchProviders, fetchProviderModels, applyProvider, testProvider, fetchStatus } from "@/lib/api"
import { ServerCog, Wallet } from "lucide-react"

type Notice = { tone: "ok" | "err"; text: string }

/**
 * 把后端返回体翻译成一句中文结果。
 *
 * 响应体若原样序列化后展示，用户看到的是一串难以理解的内部结构。
 * 这里只挑认得的字段做中文摘要，其余一律不展示。
 */
function summarize(kind: "apply" | "test", r: unknown): string {
  const o = (r ?? {}) as Record<string, unknown>
  if (kind === "apply") {
    // 真实返回是 {"applied": "openai", "config": {...}}，
    // 没有 ok，也没有 restart_required。若去读这两个不存在的字段，
    // 成功时因为取不到值恰好落到"已应用，立即生效"（结果碰巧对、
    // 理由却是错的），失败时更无法区分。因此以 applied 为准。
    const name = typeof o.applied === "string" ? o.applied : ""
    return name ? `已应用「${name}」，立即生效` : "已保存"
  }
  // test：真实字段是 chat_ok / endpoint.online / chat_error / latency_ms。
  // reachable / ok 这两个字段后端都不返回；若去读它们，
  // 连通失败也会显示"探测完成"，而后端明明给了中文原因。
  // 这里以真实字段为准，并把后端给的原因直接呈现，不吞掉。
  const ep = (o.endpoint ?? {}) as Record<string, unknown>
  if (o.chat_ok === true || ep.online === true) {
    const ms = typeof o.latency_ms === "number" ? `，耗时 ${o.latency_ms} 毫秒` : ""
    return `连接正常${ms}`
  }
  const why =
    (typeof o.chat_error === "string" && o.chat_error) ||
    (typeof ep.error === "string" && ep.error) ||
    ""
  return why || "无法连通，请检查接口地址与密钥"
}

export function SettingsPage() {
  const [presets, setPresets] = React.useState<Array<Record<string, unknown>>>([])
  const [active, setActive] = React.useState<string>("")
  const [pick, setPick] = React.useState<string>("")
  const [catalog, setCatalog] = React.useState<unknown>(null)
  const [key, setKey] = React.useState("")
  const [notice, setNotice] = React.useState<Notice | null>(null)
  const [budget, setBudget] = React.useState<{ limit: number; spent: number; remaining: number } | null>(null)
  // 两个请求各自独立：供应商列表加载中、预算加载中。
  // 两者若都没有独立状态，首屏就是一片空白加一句"加载中…"，与卡死无法区分。
  const [loadingProviders, setLoadingProviders] = React.useState(true)
  const [loadingBudget, setLoadingBudget] = React.useState(true)
  // 后端只在响应顶层的 current 里给 has_key（表示"当前生效这家是否已配密钥"），
  // **每个 preset 上并没有这个字段**。若在遍历时读每个条目的 has_key，
  // 是永远取不到值的死引用，"· 已配置密钥"将始终不显示。
  const [hasKey, setHasKey] = React.useState(false)

  const reload = React.useCallback(() => {
    setLoadingProviders(true)
    fetchProviders()
      .then((p) => {
        setPresets((p.presets ?? []) as Array<Record<string, unknown>>)
        setActive(p.active ?? "")
        setPick((prev) => prev || p.active || "")
        setHasKey(Boolean((p.current ?? {}).has_key))
      })
      .catch((e) => setNotice({ tone: "err", text: userMessage(e) }))
      .finally(() => setLoadingProviders(false))

    setLoadingBudget(true)
    fetchStatus()
      .then((s) => setBudget(s.budget ?? null))
      .catch(() => setBudget(null))
      .finally(() => setLoadingBudget(false))
  }, [])

  React.useEffect(reload, [reload])

  const onPick = (v: string) => {
    setPick(v)
    setNotice(null)
    setCatalog(null)
    fetchProviderModels(v)
      .then(setCatalog)
      .catch((e) => setNotice({ tone: "err", text: userMessage(e) }))
  }

  const used = budget && budget.limit > 0 ? (budget.spent / budget.limit) * 100 : 0

  // 供应商显示名：后端每个 preset 同时给 name（内部 id，如 "openai"）与
  // label（中文名，如 "OpenAI" / "智谱 BigModel"）。若下拉直接显示 name，
  // 用户在列表里看到的就是一串英文标识，中文名没有被用上。
  const labelOf = (p: Record<string, unknown> | undefined) =>
    String(p?.label ?? p?.name ?? "")
  const idOf = (p: Record<string, unknown>) => String(p?.id ?? p?.name ?? "")
  const activeLabel = labelOf(presets.find((p) => idOf(p) === active))

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>模型供应商</CardTitle>
          <CardDescription>当前生效：{activeLabel || active || "未设置"}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {loadingProviders && presets.length === 0 ? (
            <SelectFieldSkeleton />
          ) : presets.length === 0 ? (
            <EmptyState
              icon={ServerCog}
              title="没有可用的供应商"
              description="刷新重试一次；若仍为空，请检查后端是否正常启动。"
              action={
                <Button variant="outline" size="sm" onClick={reload}>
                  刷新
                </Button>
              }
            />
          ) : (
            <div className="space-y-2">
              <Label>供应商</Label>
              <Select value={pick} onValueChange={onPick}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="选择供应商…" />
                </SelectTrigger>
                <SelectContent>
                  {presets.map((p) => {
                    const id = idOf(p)
                    return (
                      <SelectItem key={id} value={id}>
                        <span className="flex items-center gap-1.5">
                          <span>{labelOf(p) || id}</span>
                          {/* 密钥状态只有"当前生效的这家"才有意义：后端把 has_key
                              放在响应顶层 current 里，不是每家各自一份。 */}
                          {active === id && hasKey ? (
                            <span className="text-xs text-muted-foreground">· 已配置密钥</span>
                          ) : null}
                        </span>
                      </SelectItem>
                    )
                  })}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="k">API 密钥</Label>
            <Input
              id="k"
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="留空则不修改"
            />
          </div>

          <div className="flex gap-2">
            <Button
              onClick={() => {
                applyProvider({ name: pick, api_key: key || undefined })
                  .then((r) => setNotice({ tone: "ok", text: summarize("apply", r) }))
                  .catch((e) => setNotice({ tone: "err", text: userMessage(e) }))
                  .finally(reload)
              }}
              disabled={!pick}
            >
              应用
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                // api.ts 里 testProvider 的 base_url 是必填（探测连通性必须有目标地址）。
                // 只传 name 会缺少必填参数，构建阶段就会失败。
                const bu = String(
                  presets.find((p) => String(p.id ?? p.name ?? "") === pick)?.base_url ?? "",
                )
                if (!bu) {
                  setNotice({ tone: "err", text: "该供应商未配置接口地址，无法探测" })
                  return
                }
                testProvider({ name: pick, base_url: bu, api_key: key || undefined })
                  .then((r) => setNotice({ tone: "ok", text: summarize("test", r) }))
                  .catch((e) => setNotice({ tone: "err", text: userMessage(e) }))
              }}
              disabled={!pick}
            >
              测试连接
            </Button>
          </div>

          {notice && (
            <Alert variant={notice.tone === "err" ? "destructive" : "default"}>
              <AlertDescription className="break-words">{notice.text}</AlertDescription>
            </Alert>
          )}

          {catalog !== null && (
            <div className="space-y-2">
              <Label>可用模型</Label>
              <Structured data={catalog} />
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>预算</CardTitle>
          <CardDescription>预算硬熔断，超出即停止</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {loadingBudget ? (
            <BudgetSkeleton />
          ) : budget ? (
            <>
              <Progress value={Math.min(100, used)} />
              <div className="grid grid-cols-3 gap-2 text-sm">
                <div>
                  <div className="text-xs text-muted-foreground">上限</div>
                  <div className="font-mono tabular-nums">{fmtNumber(budget.limit)}</div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">已用</div>
                  <div className="font-mono tabular-nums">{fmtNumber(budget.spent)}</div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">剩余</div>
                  <div className="font-mono tabular-nums">{fmtNumber(budget.remaining)}</div>
                </div>
              </div>
              <Badge variant={used > 90 ? "destructive" : "secondary"}>
                {used.toFixed(1)}% 已使用
              </Badge>
            </>
          ) : (
            <EmptyState
              icon={Wallet}
              title="暂无预算数据"
              description="后端未返回预算信息，通常意味着服务刚启动或尚未产生消耗。"
            />
          )}
        </CardContent>
      </Card>
      <VoicePanel />
      <PermissionsPanel />
      <AuditPanel />
    </div>
  )
}
