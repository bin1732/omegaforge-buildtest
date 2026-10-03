import { cn } from "@/lib/utils"
import { phaseLabel } from "@/lib/labels"

export const PHASES = ["ingest", "extract", "compress", "synthesize", "gen_eval", "arena", "finalize"] as const

// 中文若各自写死在用到的地方，同一份映射会散成多份，改一处忘一处，
// 界面上同一个阶段出现两种译法。
// 现在统一走 labels.ts 的 phaseLabel——本文件列出的每一项阶段都必须收录在
// 那边的映射表内，由校验环节保证。

export function PhaseBar({ current, status }: { current?: string; status?: string }) {
  const done = status === "succeeded" || status === "done"
  const failed = status === "failed" || status === "error"
  const idx = current ? PHASES.indexOf(current as (typeof PHASES)[number]) : -1

  return (
    <ol className="flex flex-wrap items-center gap-1.5">
      {PHASES.map((p, i) => {
        const isDone = done || (idx >= 0 && i < idx)
        const isNow = !done && !failed && idx === i
        return (
          <li
            key={p}
            className={cn(
              "rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors",
              isDone && "bg-[var(--phase-done)]/15 text-[var(--phase-done)]",
              isNow && "bg-[var(--phase-active)]/20 text-[var(--phase-active)] ring-1 ring-[var(--phase-active)]/40",
              !isDone && !isNow && "bg-muted text-muted-foreground"
            )}
          >
            {i + 1}. {phaseLabel(p)}
          </li>
        )
      })}
      {failed && (
        <li className="rounded-full bg-destructive/15 px-2.5 py-1 text-[11px] font-medium text-destructive">
          失败
        </li>
      )}
    </ol>
  )
}
