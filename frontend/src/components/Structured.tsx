import * as React from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { fieldLabel, hasLabel } from "@/lib/labels"
import { fmtNumber } from "@/lib/format"

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v)
}

function Label({ k }: { k: string }) {
  // 键名一律走中文字典；未收录的保持小写原名并弱化，
  // 不做 uppercase——那会把英文键名放大成标题，是最显眼的技术残留。
  return (
    <span
      className={cn(
        "shrink-0 text-xs font-medium tracking-wide",
        hasLabel(k) ? "text-muted-foreground" : "text-muted-foreground/60",
      )}
    >
      {fieldLabel(k)}
    </span>
  )
}

function Scalar({ k, v }: { k: string; v: unknown }) {
  // 布尔值用语义色，避免 true/false 在长列表里被扫读漏掉
  if (typeof v === "boolean") {
    return (
      <div className="flex items-center justify-between gap-3 py-1">
        <Label k={k} />
        <Badge variant={v ? "default" : "secondary"}>{v ? "是" : "否"}</Badge>
      </div>
    )
  }
  if (typeof v === "number") {
    return (
      <div className="flex items-center justify-between gap-3 py-1">
        <Label k={k} />
        <span className="font-mono text-sm tabular-nums">{fmtNumber(v)}</span>
      </div>
    )
  }
  // 空值不能渲染成 "null" / "undefined"
  if (v === null || v === undefined || v === "") {
    return (
      <div className="flex items-center justify-between gap-3 py-1">
        <Label k={k} />
        <span className="text-sm text-muted-foreground">—</span>
      </div>
    )
  }
  const s = String(v)
  const long = s.length > 80
  return (
    <div className={cn("py-1", long ? "space-y-1" : "flex items-center justify-between gap-3")}>
      <Label k={k} />
      <span
        className={cn(
          long
            ? "block rounded-md bg-muted/50 p-2 text-sm leading-relaxed whitespace-pre-wrap"
            : "text-sm"
        )}
      >
        {s}
      </span>
    </div>
  )
}

export function Structured({ data, depth = 0 }: { data: unknown; depth?: number }) {
  if (data === null || data === undefined) {
    return <p className="text-sm text-muted-foreground">暂无数据</p>
  }
  if (!isObj(data) && !Array.isArray(data)) {
    if (data === "" ) return <p className="text-sm text-muted-foreground">—</p>
    return <p className="text-sm whitespace-pre-wrap">{String(data)}</p>
  }
  if (Array.isArray(data)) {
    if (data.length === 0) {
      return <p className="text-sm text-muted-foreground">（空）</p>
    }
    return (
      <ul className="space-y-1">
        {data.map((it, i) => (
          <li key={i} className="rounded-md bg-muted/40 px-2 py-1 text-sm">
            {isObj(it) || Array.isArray(it) ? (
              <Structured data={it} depth={depth + 1} />
            ) : it === null || it === undefined || it === "" ? (
              "—"
            ) : (
              String(it)
            )}
          </li>
        ))}
      </ul>
    )
  }
  const entries = Object.entries(data)
  return (
    <div className="space-y-2">
      {entries.map(([k, v]) => {
        if (isObj(v) || Array.isArray(v)) {
          if (depth >= 2) {
            return (
              <div key={k} className="rounded-md bg-muted/40 p-2">
                <Label k={k} />
                {/*
                  原始 JSON 只对需要排查的人有意义。
                  默认收起，用户不主动展开就看不到花括号和字段名。
                */}
                <details className="mt-1">
                  <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
                    查看原始数据
                  </summary>
                  <pre className="mt-1 overflow-x-auto rounded bg-muted p-2 text-xs text-muted-foreground">
                    {JSON.stringify(v, null, 2)}
                  </pre>
                </details>
              </div>
            )
          }
          return (
            <Card key={k} className="border-border/60">
              <CardHeader className="py-2">
                {/*
                  卡片标题同样必须走中文字典。若直接把键名下划线换成空格、
                  再用 uppercase 放大，就绕过了 fieldLabel，值为数组或对象的
                  字段会把内部字段名放大成标题给用户看。字典里已有对应译名，
                  一律取译名，只有未收录的键才回退原名。
                */}
                <CardTitle className="text-xs font-medium tracking-wide text-muted-foreground">
                  {fieldLabel(k)}
                </CardTitle>
              </CardHeader>
              <CardContent className="py-2">
                <Structured data={v} depth={depth + 1} />
              </CardContent>
            </Card>
          )
        }
        return <Scalar key={k} k={k} v={v} />
      })}
    </div>
  )
}
