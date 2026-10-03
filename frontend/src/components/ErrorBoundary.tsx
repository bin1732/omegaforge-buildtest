/**
 * 全局渲染错误边界 —— 表层最后一道防线。
 *
 * 没有它，任一组件抛错会导致整棵 React 树卸载，
 * 用户看到的是纯白屏（连报错都看不到），这是最糟的表层事故。
 *
 * 行为：
 * - 捕获异常，渲染中文降级卡片，提供「重新加载」「返回首页」两个出口
 * - 技术细节默认收起，仅写入 console（开发者可见，用户不可见）
 * - 不展示 stack / 组件名 / 异常类名
 */

import React from 'react'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'

interface Props {
  children: React.ReactNode
  /** 出错区域名称，用于按钮文案与内部日志定位 */
  area?: string
  /** 自定义降级 UI；不传则用默认卡片 */
  fallback?: React.ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // 只进控制台，不进 UI
    console.error(
      `[ErrorBoundary${this.props.area ? `:${this.props.area}` : ''}]`,
      error,
      info.componentStack,
    )
  }

  private reset = () => this.setState({ error: null })

  private reload = () => window.location.reload()

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback) return this.props.fallback

    const area = this.props.area ?? '此区域'

    return (
      <div className="flex min-h-[240px] w-full items-center justify-center p-6">
        <div className="w-full max-w-md space-y-4">
          <Alert variant="destructive">
            <AlertTitle>{area}暂时无法显示</AlertTitle>
            <AlertDescription>
              界面遇到了意外问题，你的数据没有丢失。可以重试加载，或重新打开应用。
            </AlertDescription>
          </Alert>
          <div className="flex gap-2">
            <Button variant="default" onClick={this.reset}>
              重试
            </Button>
            <Button variant="outline" onClick={this.reload}>
              重新打开
            </Button>
          </div>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
