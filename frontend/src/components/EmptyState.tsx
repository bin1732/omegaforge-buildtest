import type { LucideIcon } from "lucide-react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/**
 * 统一空状态。
 *
 * 空白区域是最容易被当作"软件坏了"的地方：列表为空、尚未开始、加载失败
 * 三种情况如果长得一样，用户无法判断下一步该做什么。这里要求每种空状态
 * 都给出一句话说明 + 可选的引导动作。
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon
  title: string
  description?: string
  action?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center",
        className,
      )}
    >
      {Icon && (
        <div className="mb-1 grid size-9 place-items-center rounded-full bg-muted text-muted-foreground">
          <Icon className="size-4" />
        </div>
      )}
      <p className="text-sm font-medium">{title}</p>
      {description && (
        <p className="max-w-sm text-xs leading-relaxed text-muted-foreground">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}
