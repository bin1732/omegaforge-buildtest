import type { ReactNode } from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

/**
 * 布局骨架屏。
 *
 * 设计约束（不是装饰，是硬要求）：
 * 1. 尺寸对齐真实内容。骨架与真实元素高度/行数不一致会产生布局位移（CLS），
 *    看起来比"空白等待"更廉价。下面每个组件的尺寸都对着对应页面的真实结构写死。
 * 2. 语义可访问。外层 role=status + aria-busy，屏幕阅读器读到"加载中"，
 *    内部方块全部 aria-hidden，避免读出一串无意义的空节点。
 * 3. 尊重 prefers-reduced-motion。脉冲动画在 globals.css 中被统一降级为静态色块，
 *    这里不单独处理，避免两处规则打架。
 */

function LoadingRegion({ label, className, children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <div role="status" aria-busy="true" className={cn("contents", className)}>
      <span className="sr-only">{label}</span>
      <div aria-hidden="true" className="contents">
        {children}
      </div>
    </div>
  )
}

/** 运行记录列表：与 RunsPage 左侧条目同高（两行文本 + py-2）。 */
export function RunListSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <LoadingRegion label="正在加载运行记录">
      <ul className="space-y-1 p-3 pt-0">
        {Array.from({ length: rows }).map((_, i) => (
          <li key={i} className="rounded-md px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <Skeleton className="h-3 w-28" />
              <Skeleton className="h-4 w-14 rounded-full" />
            </div>
            <Skeleton className="mt-1.5 h-2.5 w-36" />
          </li>
        ))}
      </ul>
    </LoadingRegion>
  )
}

/** 下拉选择控件：与 SelectTrigger（h-9）+ 上方 Label 对齐。 */
export function SelectFieldSkeleton({ withLabel = true }: { withLabel?: boolean }) {
  return (
    <LoadingRegion label="正在加载选项">
      <div className="space-y-2">
        {withLabel && <Skeleton className="h-3 w-14" />}
        <Skeleton className="h-9 w-full" />
      </div>
    </LoadingRegion>
  )
}

/** 日志流：与 LogStream 的等宽行对齐。 */
export function LogSkeleton({ lines = 6 }: { lines?: number }) {
  return (
    <LoadingRegion label="正在加载日志">
      <div className="space-y-2">
        {Array.from({ length: lines }).map((_, i) => (
          <Skeleton key={i} className={cn("h-3", ["w-3/4", "w-1/2", "w-5/6", "w-2/3", "w-4/5", "w-1/3"][i % 6])} />
        ))}
      </div>
    </LoadingRegion>
  )
}

/** 键值结构块：与 Structured 渲染的 key/value 行对齐。 */
export function StructuredSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <LoadingRegion label="正在加载结构化数据">
      <div className="space-y-2.5">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex items-center justify-between gap-4">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="h-3 w-40" />
          </div>
        ))}
      </div>
    </LoadingRegion>
  )
}

/** 详情面板：日志 + 结构块的组合，用于 RunsPage 右侧。 */
export function DetailSkeleton() {
  return (
    <div className="space-y-4">
      <LogSkeleton lines={5} />
      <StructuredSkeleton rows={4} />
    </div>
  )
}

/** 预算卡片：数值 + 进度条，与 SettingsPage 预算区对齐。 */
export function BudgetSkeleton() {
  return (
    <LoadingRegion label="正在加载预算">
      <div className="space-y-3">
        <div className="flex items-baseline justify-between">
          <Skeleton className="h-3 w-16" />
          <Skeleton className="h-5 w-24" />
        </div>
        <Skeleton className="h-2 w-full rounded-full" />
        <div className="flex justify-between">
          <Skeleton className="h-2.5 w-20" />
          <Skeleton className="h-2.5 w-20" />
        </div>
      </div>
    </LoadingRegion>
  )
}
