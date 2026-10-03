/**
 * 十页真实渲染验证（jsdom + react-dom 挂载）。
 *
 * 为什么需要它：类型检查通过 ≠ 页面能用。本项目多次出现
 * "字段读错但类型检查不报错"（索引签名让漂移不可见）与
 * "页面恒为空但不崩溃"。只有真实挂载 + 真实后端响应形状
 * 才能证明字段真的读对了。
 *
 * 前置（沙盒内构建环境在 /tmp/fe，工作区写入极慢）：
 *   cd /tmp/fe && env npm_config_global=false npm install
 *   ./node_modules/.bin/esbuild pages-entry.tsx --bundle --format=cjs \
 *     --platform=node --outfile=/tmp/pages_bundle.cjs \
 *     --alias:@=<frontend>/src --loader:.css=empty --jsx=automatic \
 *     --external:react --external:react-dom
 *   node probes/frontend_pages_render.js
 *
 * 反例模式 REVERT=1：把 runs 的时间字段改回旧名 created（而非
 * created_ts），必须让 RunsPage 守卫变红——否则说明守卫是摆设。
 */
const { JSDOM } = require(process.env.FE_JSDOM || '/tmp/fe/node_modules/jsdom')

require.extensions['.css'] = () => {}

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://127.0.0.1:8787/',
  pretendToBeVisual: true,
})
global.window = dom.window
global.document = dom.window.document
global.navigator = dom.window.navigator
global.HTMLElement = dom.window.HTMLElement
global.Node = dom.window.Node
global.Event = dom.window.Event
global.requestAnimationFrame = (cb) => setTimeout(cb, 0)
global.cancelAnimationFrame = clearTimeout
global.IS_REACT_ACT_ENVIRONMENT = true
global.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
global.getComputedStyle = (el) => dom.window.getComputedStyle(el || dom.window.document.body)
if (!dom.window.HTMLElement.prototype.hasPointerCapture) {
  dom.window.HTMLElement.prototype.hasPointerCapture = () => false
  dom.window.HTMLElement.prototype.setPointerCapture = () => {}
  dom.window.HTMLElement.prototype.releasePointerCapture = () => {}
  dom.window.HTMLElement.prototype.scrollIntoView = () => {}
}
global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
global.DOMRect = dom.window.DOMRect
// radix / react-dom 挂载时会引用这些 DOM 构造函数：jsdom 有，global 上没有。
// 缺了会抛 "HTMLFormElement is not defined" —— 环境缺口，不是页面缺陷。
for (const k of ['HTMLFormElement', 'HTMLInputElement', 'HTMLButtonElement',
  'HTMLSelectElement', 'HTMLTextAreaElement', 'HTMLAnchorElement', 'HTMLDivElement',
  'HTMLSpanElement', 'MutationObserver', 'CustomEvent', 'DocumentFragment',
  'SVGSVGElement', 'Element']) {
  if (dom.window[k]) global[k] = dom.window[k]
}

const NM = process.env.FE_NODE_MODULES || '/tmp/fe/node_modules'
const React = require(NM + '/react')
const { createRoot } = require(NM + '/react-dom/client')
const { act } = React

/* ---------------- 真实后端响应形状（实测，非臆造） ---------------- */
const REVERT = process.env.REVERT === '1'
const run0 = REVERT
  // 反例：把时间字段改回旧名，页面应当读不到 → RunsPage 守卫必须变红
  ? { id: 'run-abc123', status: 'done', created: 1789912808, seq: 12,
      title: '蒸馏：如何写好提示词', mode: 'distill' }
  : { id: 'run-abc123', status: 'done', created_ts: 1789912808, seq: 12,
      title: '蒸馏：如何写好提示词', mode: 'distill' }

const MOCK = {
  '/api/status': {
    version: '0.1.0', mock_mode: true,
    models: { main: 'gpt-4o-mini', fast: 'gpt-4o-mini', judge: 'gpt-4o-mini' },
    jobs: 0,
    budget: { limit: 400000, spent: 0, remaining: 400000 },
  },
  '/api/usage': {
    total: 12345, entries: 7,
    by_phase: { distill: 9000, chat: 3345 },
    by_model: { 'gpt-4o-mini': 12345 },
    timeline: [0, 0, 0, 100, 200, 300, 150, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 50],
    bucket_seconds: 300, heatmap: {}, hottest_phase: 'distill',
  },
  '/api/conversations': {
    conversations: [
      { id: 'b4bbfe4426', title: '新对话', model_pref: 'auto', updated: 1789913705.97, count: 0 },
    ],
  },
  '/api/kb/list': {
    docs: [{ id: '3019deabab', type: 'note', title: 't1', text: '测试内容', tags: [], ts: 1789913705.96 }],
    total: 1,
  },
  '/api/tasks/list': {
    items: [{ id: '0024718c', text: '写周报', done: false, priority: 2, created: 1789913705.97, done_ts: null }],
    stats: { total: 1, pending: 1, done: 0 },
  },
  '/api/wiki/list': { pages: [{ slug: 'product-vision', title: '产品愿景', ts: 1789913705 }] },
  '/api/skills/list': {
    skills: [{ name: 'reviewer', description: '代码评审', version: '1.0', type: 'skill',
      category: '通用', risk: 'medium', when_to_use: '提交前' }],
  },
  '/api/personas': {
    categories: { '通用': [{ slug: 'reviewer', name: '评审官', description: '严格' }] },
    total: 1,
  },
  '/api/memory/recall': { memories: [{ id: 'm1', text: '我叫小明' }] },
  '/api/voice/status': { asr: { ready: false }, tts: { ready: false } },
  '/api/providers': { presets: [], active: null, current: {} },
  '/api/runs': { runs: [run0], total: 1 },
  '/api/tools/permissions': { permissions: {} },
  '/api/personas/list': { personas: [] },
}

global.fetch = async (url) => {
  const u = String(url || '')
  const p = u.replace(/^https?:\/\/[^/]+/, '').split('?')[0]
  const body = MOCK[p] !== undefined ? MOCK[p] : {}
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body), body: null }
}

const CASES = [
  { name: 'ForgePage', must: ['蒸馏工坊', '开始蒸馏'] },
  { name: 'ChatPage', must: ['对话', '新对话', '2026/9/20'] },
  { name: 'KnowledgePage', must: ['知识库', 't1', '测试内容'] },
  { name: 'TasksPage', must: ['写周报', '待办 1'] },
  { name: 'SkillsPage', must: ['已安装技能', 'reviewer', '评审官'] },
  { name: 'ArenaPage', must: ['竞技场', '选择一次运行'] },
  { name: 'GenomePage', must: ['基因组', '选择运行'] },
  // 时间列此前恒为空白：后端字段是 created_ts，页面读的却是另一个名字，
  // 索引签名让类型检查不报错。断言具体到时刻，才能钉住"读对了字段"。
  { name: 'RunsPage', must: ['run-abc123', '已完成', '2026/9/20 14:00:08'] },
  // by_phase 与 hottest_phase 都含 'distill'，只断言它会造成"互为掩护"：
  // by_phase 读错时 hottest_phase 仍提供该串，守卫假绿。必须断言独有值。
  { name: 'UsagePage', must: ['12,345', '9,000', '3,345', '7'] },
  { name: 'SettingsPage', must: ['模型供应商', '400,000'] },
]

;(async () => {
  const bundle = require(process.env.FE_BUNDLE || '/tmp/pages_bundle.cjs')
  let fail = 0
  for (const c of CASES) {
    const Comp = bundle[c.name]
    if (typeof Comp !== 'function') { console.log(`✗ ${c.name} 未导出组件`); fail++; continue }
    const host = dom.window.document.createElement('div')
    dom.window.document.body.appendChild(host)
    let root
    try {
      root = createRoot(host)
      await act(async () => { root.render(React.createElement(Comp)) })
      await act(async () => { await new Promise((r) => setTimeout(r, 500)) })
    } catch (e) {
      console.log(`✗ ${c.name} 渲染抛错: ${e.message}`)
      fail++
      continue
    }
    const txt = (host.textContent || '').replace(/\s+/g, ' ')
    const missing = c.must.filter((m) => !txt.includes(m))
    if (missing.length) {
      console.log(`✗ ${c.name} 缺少关键内容: ${missing.join(' / ')}`)
      console.log(`   实际渲染: ${txt.slice(0, 220)}`)
      fail++
    } else {
      console.log(`✓ ${c.name}（${txt.length} 字）`)
    }
    try { await act(async () => root.unmount()) } catch {}
  }
  console.log(REVERT ? `[REVERT 模式] ${fail} 个失败` : (fail === 0 ? '\nALL RENDER OK' : `\n${fail} 个页面渲染失败`))
  process.exit(fail === 0 ? 0 : 1)
})().catch((e) => { console.error('FATAL', e && e.stack); process.exit(1) })
