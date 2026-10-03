import * as React from "react"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { ThemeToggle } from "@/components/ThemeToggle"
import { ForgePage } from "@/pages/ForgePage"
import { ArenaPage } from "@/pages/ArenaPage"
import { GenomePage } from "@/pages/GenomePage"
import { RunsPage } from "@/pages/RunsPage"
import { ChatPage } from "@/pages/ChatPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { TasksPage } from "@/pages/TasksPage"
import { SkillsPage } from "@/pages/SkillsPage"
import { UsagePage } from "@/pages/UsagePage"
import { SettingsPage } from "@/pages/SettingsPage"
import { fetchStatus, type BackendStatus } from "@/lib/api"
import { cn } from "@/lib/utils"
import { userMessage } from "@/lib/format"
import { AlertTriangle } from "lucide-react"

const NAV = [
  { id: "forge", label: "蒸馏工坊", hint: "从源 Agent 提炼能力" },
  { id: "chat", label: "对话", hint: "直接与模型对话" },
  { id: "knowledge", label: "知识库", hint: "条目 / 记忆 / 词条" },
  { id: "tasks", label: "待办", hint: "任务跟踪" },
  { id: "skills", label: "技能与人设", hint: "安装与调用" },
  { id: "arena", label: "竞技场", hint: "同题对照裁决" },
  { id: "genome", label: "基因组", hint: "解剖 Prompt 结构" },
  { id: "runs", label: "运行记录", hint: "历史与事件流" },
  { id: "usage", label: "用量", hint: "Token 与额度" },
  { id: "settings", label: "设置", hint: "供应商与预算" },
] as const

type NavId = (typeof NAV)[number]["id"]

function StatusPill({ status, error }: { status: BackendStatus | null; error: string | null }) {
  if (error) {
    return <Badge variant="destructive">服务未连接</Badge>
  }
  if (!status) {
    return <Badge variant="secondary">连接中…</Badge>
  }
  return (
    <div className="flex items-center gap-2">
      {status.mock_mode && (
        <Badge
          variant="outline"
          className="cursor-help border-amber-500/40 text-amber-600 dark:text-amber-400"
          title="未配置模型密钥，当前展示的全部内容为本地演示数据，不代表真实模型表现"
        >
          演示数据 · 未接入模型
        </Badge>
      )}
      <Badge variant="secondary">v{status.version}</Badge>
    </div>
  )
}

export default function App() {
  const [active, setActive] = React.useState<NavId>("forge")
  const [status, setStatus] = React.useState<BackendStatus | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const s = await fetchStatus()
        if (!alive) return
        setStatus(s)
        setError(null)
      } catch (e) {
        if (!alive) return
        setError(userMessage(e))
      }
    }
    void tick()
    const t = window.setInterval(tick, 5000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground">
        {/* 窄屏收为图标条：侧边栏若固定占 240px 且不可收缩，390px 视口下
            内容区只剩约 100px，全页横向溢出。 */}
        <aside className="flex w-14 shrink-0 flex-col border-r border-border bg-card md:w-60">
          <div className="flex items-center justify-center gap-2.5 px-2 py-5 md:justify-start md:px-5">
            <div className="grid size-8 shrink-0 place-items-center rounded-lg bg-[var(--primary-emphasis)] text-sm font-bold text-primary-foreground">
              Ω
            </div>
            <div className="hidden leading-tight md:block">
              <div className="text-sm font-semibold tracking-tight">OmegaForge</div>
              <div className="text-[11px] text-muted-foreground">Studio</div>
            </div>
          </div>

          <Separator />

          <nav className="flex-1 space-y-0.5 p-3">
            {NAV.map((n) => {
              const on = active === n.id
              return (
                <Tooltip key={n.id}>
                  <TooltipTrigger asChild>
                    <button
                      onClick={() => setActive(n.id)}
                      aria-current={on ? "page" : undefined}
                      className={cn(
                        "w-full rounded-md px-3 py-2 text-center text-sm transition-colors md:text-left",
                        on
                          ? "bg-accent font-medium text-accent-foreground"
                          : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                      )}
                    >
                      {/* 窄屏只留首字，完整名称由悬浮提示给出 */}
                      <span className="md:hidden">{n.label.slice(0, 1)}</span>
                      <span className="hidden md:inline">{n.label}</span>
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="right">
                    <span className="md:hidden">{n.label} · </span>
                    {n.hint}
                  </TooltipContent>
                </Tooltip>
              )
            })}
          </nav>

          <div className="space-y-2 p-2 md:p-3">
            <div className="flex justify-center md:block">
              <StatusPill status={status} error={error} />
            </div>
            <div className="flex items-center justify-center gap-2 md:justify-between">
              <span className="hidden text-[11px] text-muted-foreground md:inline">
                {status ? (status.jobs > 0 ? `${status.jobs} 个任务运行中` : "空闲") : "—"}
              </span>
              <ThemeToggle />
            </div>
          </div>
        </aside>

        <main className="min-w-0 flex-1 overflow-y-auto">
          {error && (
            <div className="flex items-center gap-2 border-b border-destructive/30 bg-destructive/10 px-6 py-2 text-xs text-destructive">
              <AlertTriangle className="size-3.5" />
              <span>{error}</span>
            </div>
          )}
          <div className="mx-auto max-w-6xl p-4 md:p-6">
            {active === "forge" && <ForgePage />}
            {active === "chat" && <ChatPage />}
            {active === "knowledge" && <KnowledgePage />}
            {active === "tasks" && <TasksPage />}
            {active === "skills" && <SkillsPage />}
            {active === "arena" && <ArenaPage />}
            {active === "genome" && <GenomePage />}
            {active === "runs" && <RunsPage />}
            {active === "usage" && <UsagePage />}
            {active === "settings" && <SettingsPage />}
          </div>
        </main>
      </div>
    </TooltipProvider>
  )
}
