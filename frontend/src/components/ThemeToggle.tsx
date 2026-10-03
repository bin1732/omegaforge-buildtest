import * as React from "react"
import { Moon, Sun } from "lucide-react"
import { Button } from "@/components/ui/button"

type Mode = "dark" | "light"

const KEY = "omegaforge-theme"

export function ThemeToggle() {
  const [mode, setMode] = React.useState<Mode>("dark")

  React.useEffect(() => {
    const saved = localStorage.getItem(KEY) as Mode | null
    const next: Mode = saved ?? "dark"
    setMode(next)
    document.documentElement.setAttribute("data-theme", next)
  }, [])

  const toggle = () => {
    const next: Mode = mode === "dark" ? "light" : "dark"
    setMode(next)
    localStorage.setItem(KEY, next)
    document.documentElement.setAttribute("data-theme", next)
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggle}
      title={mode === "dark" ? "切换到浅色" : "切换到深色"}
      aria-label="切换主题"
    >
      {mode === "dark" ? (
        <Moon className="size-4" />
      ) : (
        <Sun className="size-4" />
      )}
    </Button>
  )
}
