import * as React from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import { EmptyState } from "@/components/EmptyState"
import { userMessage } from "@/lib/format"
import {
  voiceStatus, voiceTts, voiceAsr, fetchPermissions, savePermissions,
  fetchToolAudit, voiceModelInstall, voiceModelProgress,
} from "@/lib/api"
import type { ToolAuditEntry } from "@/lib/api"

/**
 * 语音与工具权限面板。
 *
 * 为什么必须有这个面板
 * --------------------
 * 后端 /api/voice/{status,tts,asr} 与 /api/tools/permissions 是真实实现，
 * api.ts 里也有对应封装。缺了这一层，能力真实存在、接口验收全绿，
 * 用户在界面上却找不到入口——等同于假功能。
 *
 * 语音模型随安装包内置，故"模型缺失"属异常态而非故障：这里如实显示
 * 状态与获取方式，不把它伪装成可用，也不因为不可用而隐藏这个能力。
 */

type VoiceState = {
  model?: string
  ready?: boolean
  lib_installed?: boolean
  files_missing?: string[]
  error?: string
}

/** 语音运行库的安装命令。
 *
 * 为什么把命令放在这里而不是只写"请安装依赖"：
 *   后端在库缺失时给的是可自助修复的指引，用户照着做必须真的能装上。
 *   只说"请安装依赖"而找不到装什么，指引就名存实亡。
 * 该命令为英文，不属于面向用户的中文文案，故不作本地化处理。
 */
const LIB_INSTALL_CMD = "pip install sherpa-onnx"

/** 模型下载来源。指向上游发布页，不指向仓库内并不存在的文档。 */
const MODEL_SOURCE_URL =
  "https://github.com/k2-fsa/sherpa-onnx/releases"

function Reason({ st }: { st: VoiceState | undefined }) {
  if (!st) return null
  if (st.ready) return null
  const bits: string[] = []
  if (!st.lib_installed) bits.push("语音运行库未安装")
  if (st.files_missing && st.files_missing.length > 0) {
    bits.push(`缺少模型文件：${st.files_missing.join("、")}`)
  }
  if (st.error) bits.push(st.error)
  if (bits.length === 0) bits.push("模型未就绪")
  return (
    <div className="text-xs text-muted-foreground space-y-1">
      {bits.map((b) => (
        <div key={b}>· {b}</div>
      ))}
      {!st.lib_installed && (
        <div className="space-y-1">
          <div>安装包已含运行库；若仍提示缺失，在命令行执行下面这条命令：</div>
          <code className="block select-all rounded bg-muted px-2 py-1 font-mono text-[11px]">
            {LIB_INSTALL_CMD}
          </code>
        </div>
      )}
      <div>
        模型已随安装包内置；此处的安装按钮仅在模型缺失时用于修复。
      </div>
      <div>
        模型下载来源：
        <a
          className="underline"
          href={MODEL_SOURCE_URL}
          target="_blank"
          rel="noreferrer"
        >
          {MODEL_SOURCE_URL}
        </a>
      </div>
    </div>
  )
}

/** 一键下载模型。
 *
 * 为什么不能只给一条命令或"请自行下载"的链接
 * ---------------------------------------
 * 绝大多数用户不会用命令行，给命令等于没给。能力真实存在、接口验收全绿，
 * 而用户永远启不动——这就是假功能。因此这里必须是一键：点一下，后端后台
 * 下载，界面轮询进度。
 *
 * 为什么必须轮询而不是等请求返回
 * -----------------------------
 * 模型是几十到几百 MB，同步等待会让请求挂到超时，界面表现为"点了没反应"，
 * 而下载其实还在跑。下载因此在后端线程里进行，这里只查状态。
 */
function ModelDownload({ kind, onDone }: {
  kind: string
  onDone: () => void
}) {
  const [busy, setBusy] = React.useState(false)
  const [prog, setProg] = React.useState<Record<string, unknown> | null>(null)
  const [err, setErr] = React.useState("")
  const timer = React.useRef<ReturnType<typeof setInterval> | null>(null)

  const stop = React.useCallback(() => {
    if (timer.current !== null) {
      clearInterval(timer.current)
      timer.current = null
    }
    setBusy(false)
  }, [])

  // 卸载时必须停表：组件被切走后继续轮询会让界面在别的页面上弹错误提示，
  // 而用户完全没有触发过任何操作。
  React.useEffect(() => () => {
    if (timer.current !== null) clearInterval(timer.current)
  }, [])

  const poll = React.useCallback(() => {
    voiceModelProgress(kind)
      .then((d) => {
        setProg(d)
        const s = String(d.state ?? "")
        if (s === "done") {
          stop()
          setProg(null)
          onDone()
        } else if (s === "failed") {
          stop()
          // 后端落的是真实原因（镜像不可达 / 内容不是模型 / 全部失败）。
          // 这里换成一句笼统的话，等于把唯一能定位问题的信息丢掉。
          setErr(String(d.error || "下载失败"))
        }
      })
      .catch((e) => {
        stop()
        setErr(userMessage(e))
      })
  }, [kind, onDone, stop])

  const start = () => {
    setErr("")
    setBusy(true)
    voiceModelInstall(kind)
      .then(() => {
        poll()
        timer.current = setInterval(poll, 1000)
      })
      .catch((e) => {
        stop()
        setErr(userMessage(e))
      })
  }

  const idx = Number(prog?.file_index ?? 0)
  const total = Number(prog?.file_total ?? 0)

  return (
    <div className="space-y-1">
      <Button
        variant="outline"
        size="sm"
        onClick={start}
        disabled={busy}
        data-testid={`voice-install-${kind}`}
      >
        {busy ? "下载中…" : "一键安装模型"}
      </Button>
      {busy && (
        <div className="text-xs text-muted-foreground" data-testid={`voice-progress-${kind}`}>
          {total > 0 ? `正在下载第 ${idx + 1}/${total} 个文件` : "正在准备…"}
        </div>
      )}
      {err && <div className="text-xs text-destructive">{err}</div>}
    </div>
  )
}

export function VoicePanel() {
  const [st, setSt] = React.useState<{ asr?: VoiceState; tts?: VoiceState } | null>(null)
  const [notice, setNotice] = React.useState<string>("")
  const [text, setText] = React.useState("OmegaForge 语音合成自检。")
  const [busy, setBusy] = React.useState<"" | "tts" | "asr">("")
  const [asrText, setAsrText] = React.useState("")
  const audioRef = React.useRef<HTMLAudioElement | null>(null)

  const reload = React.useCallback(() => {
    voiceStatus()
      .then((d) => setSt({ asr: d.asr as VoiceState, tts: d.tts as VoiceState }))
      .catch((e) => setNotice(userMessage(e)))
  }, [])

  React.useEffect(reload, [reload])

  const onTts = () => {
    setBusy("tts")
    setNotice("")
    voiceTts({ text })
      .then((d) => {
        const b64 = String(d.audio_b64 ?? "")
        if (!b64) {
          setNotice("后端没有返回音频数据")
          return
        }
        if (audioRef.current) {
          audioRef.current.src = "data:audio/wav;base64," + b64
          audioRef.current.play().catch(() => setNotice("浏览器阻止了自动播放，请再次点击播放"))
        }
      })
      .catch((e) => setNotice(userMessage(e)))
      .finally(() => setBusy(""))
  }

  const onFile = (f: File | undefined) => {
    if (!f) return
    setBusy("asr")
    setNotice("")
    const reader = new FileReader()
    // 读取失败必须让用户看见：否则界面停留在"识别中"，与卡死无法区分。
    reader.onerror = () => {
      setNotice("读取文件失败，请换一个文件重试")
      setBusy("")
    }
    reader.onload = () => {
      const raw = String(reader.result ?? "")
      const b64 = raw.includes(",") ? raw.split(",", 2)[1] : raw
      voiceAsr({ audio_b64: b64 })
        .then((d) => setAsrText(String(d.text ?? "")))
        .catch((e) => setNotice(userMessage(e)))
        .finally(() => setBusy(""))
    }
    reader.readAsDataURL(f)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>语音</CardTitle>
        <CardDescription>
          离线合成与识别。模型需单独放置，未就绪时如实显示原因。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {notice && <div className="text-sm text-destructive">{notice}</div>}

        <div className="grid gap-2 sm:grid-cols-2">
          <div className="rounded-md border p-3 space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">合成</span>
              <Badge variant={st?.tts?.ready ? "secondary" : "outline"}>
                {st?.tts?.ready ? "可用" : "不可用"}
              </Badge>
            </div>
            <Reason st={st?.tts} />
          </div>
          <div className="rounded-md border p-3 space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">识别</span>
              <Badge variant={st?.asr?.ready ? "secondary" : "outline"}>
                {st?.asr?.ready ? "可用" : "不可用"}
              </Badge>
            </div>
            <Reason st={st?.asr} />
            <ModelDownload kind="asr" onDone={reload} />
          </div>
        </div>

        <div className="space-y-2">
          <Label>朗读</Label>
          <div className="flex gap-2">
            <Input value={text} onChange={(e) => setText(e.target.value)} />
            <Button variant="outline" onClick={onTts} disabled={busy !== ""}>
              {busy === "tts" ? "合成中…" : "试听"}
            </Button>
          </div>
          <audio ref={audioRef} controls className="w-full" />
        </div>

        <div className="space-y-2">
          <Label>识别音频文件（WAV）</Label>
          <Input
            type="file"
            accept="audio/*,.wav"
            onChange={(e) => onFile(e.target.files?.[0])}
          />
          {busy === "asr" && <div className="text-xs text-muted-foreground">识别中…</div>}
          {asrText && (
            <div className="rounded-md border p-2 text-sm">{asrText}</div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

const CAPABILITY_LABELS: Record<string, string> = {
  terminal: "运行终端命令",
  fs: "读写本地文件",
  web_fetch: "访问网页",
}

// 后端契约是 {key: bool}（见 tools/system_tools.py Permissions.load/save，
// 两者都对 PERMISSION_KEYS 做 bool() 收敛）。此处仍显式归一：
// 若将来返回非布尔，开关渲染必须落到确定状态，不能把 unknown 当作已开启
// 显示给用户——那会让界面显示与实际授权相反。
const asBoolMap = (d: unknown): Record<string, boolean> =>
  d && typeof d === "object"
    ? Object.fromEntries(
        Object.entries(d as Record<string, unknown>).map(([k, v]) => [k, Boolean(v)]),
      )
    : {}

export function PermissionsPanel() {
  const [perms, setPerms] = React.useState<Record<string, boolean> | null>(null)
  const [notice, setNotice] = React.useState("")
  const [busy, setBusy] = React.useState("")

  const load = React.useCallback(() => {
    fetchPermissions()
      .then((d) => setPerms(d.permissions ? asBoolMap(d.permissions) : null))
      .catch((e) => setNotice(userMessage(e)))
  }, [])

  React.useEffect(() => { load() }, [load])

  // 开关必须真的能保存。
  //
  // 为什么：工具执行的拒绝提示是「请在「设置 → 系统能力」中开启后重试」，
  // 用户按提示找到这里，看到的却是「此面板只读，改动请通过命令行或 MCP
  // 配置」——而命令行并无对应子命令。指引把他送来、这里又把他推走，
  // 能力永远用不上，与功能不存在无法区分。
  const toggle = (key: string, on: boolean) => {
    if (on && key === "terminal") {
      // 开启等于交出本机命令执行权，必须显式确认一次。
      const ok = typeof window !== "undefined" && window.confirm
        ? window.confirm("开启后应用可在本机运行终端命令。确认开启？")
        : true
      if (!ok) return
    }
    setBusy(key)
    setNotice("")
    savePermissions({ ...(perms ?? {}), [key]: on })
      .then((d) => setPerms(d.permissions ? asBoolMap(d.permissions) : null))
      .catch((e) => setNotice(userMessage(e)))
      .finally(() => setBusy(""))
  }

  const entries = Object.entries(perms ?? {})

  return (
    <Card>
      <CardHeader>
        <CardTitle>系统能力</CardTitle>
        <CardDescription>
          文件、终端与网页访问三类能力的开关。关闭时工具执行会被拒绝。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {notice && <div className="text-sm text-destructive">{notice}</div>}
        {entries.length === 0 ? (
          <EmptyState
            title="暂无权限数据"
            description={notice ? "请检查后端是否正常启动。" : "后端未返回权限配置。"}
          />
        ) : (
          <div className="space-y-2">
            {entries.map(([k, v]) => {
              const on = Boolean(v)
              return (
                <div key={k} className="flex items-center justify-between text-sm">
                  <div>
                    <div>{CAPABILITY_LABELS[k] ?? k}</div>
                    <div className="font-mono text-xs opacity-60">{k}</div>
                  </div>
                  <Button
                    size="sm"
                    disabled={busy === k}
                    onClick={() => toggle(k, !on)}
                  >
                    {on ? "已开启" : "已关闭"}
                  </Button>
                </div>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}


export function AuditPanel() {
  const [entries, setEntries] = React.useState<ToolAuditEntry[] | null>(null)
  const [notice, setNotice] = React.useState("")

  const load = React.useCallback(() => {
    setNotice("")
    fetchToolAudit(50)
      .then((d) => setEntries(d.entries ?? []))
      .catch((e) => {
        setEntries(null)
        setNotice(userMessage(e))
      })
  }, [])

  React.useEffect(load, [load])

  const rows = [...(entries ?? [])].reverse()

  return (
    <Card>
      <CardHeader>
        <CardTitle>工具审计</CardTitle>
        <CardDescription>
          最近的工具执行流水，含放行与被拒。此面板只读。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {notice && <div className="text-sm text-destructive">{notice}</div>}
        <div>
          <Button variant="outline" size="sm" onClick={load}>
            刷新
          </Button>
        </div>
        {rows.length === 0 ? (
          <EmptyState
            title="暂无审计记录"
            description={
              notice
                ? "读取失败，请检查后端是否正常启动。"
                : "还没有工具执行过。执行任意工具后这里会出现记录。"
            }
          />
        ) : (
          <div className="space-y-1">
            {rows.map((r, i) => (
              <div key={i} className="flex items-center justify-between text-sm gap-2">
                <span className="font-mono truncate">{r.tool ?? "-"}</span>
                <Badge
                  variant={
                    r.verdict === "deny"
                      ? "destructive"
                      : r.verdict === "allow" || r.verdict === "approved"
                        ? "secondary"
                        : "outline"
                  }
                >
                  {r.verdict ?? "-"}
                </Badge>
                <span className="text-muted-foreground text-xs truncate">
                  {r.reason ? r.reason : r.mode ?? ""}
                </span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
