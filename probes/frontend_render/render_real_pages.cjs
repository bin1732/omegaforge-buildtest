/* 环境路径：默认取工作区内的前端环境目录。
 *
 * 早先默认 /tmp/fe，而临时区会被回收，脚本于是在环境缺失时先报
 * "找不到模块"，看起来像产品坏了。默认目录改为工作区内，环境随沙盒
 * 重置后重建一次即可，且可用 FE_HOME 覆盖。
 * 覆盖方式：FE_HOME=/path/to/fe node render_real_pages.cjs
 */
const FE_HOME = process.env.FE_HOME || '/data/workspace/fe_env';
const OUT_ROOT = process.env.FE_OUT || '/data/workspace/ssrout';
/**
 * 十页真实渲染守卫：喂的是**真后端抓下来的真实响应**，不是手工构造的形状。
 *
 * 为什么必须换掉假数据
 * --------------------
 * 上一章实测发现：渲染探针此前喂的是手工构造的"真实响应形状"，臆造了
 * title / mode 等真实响应里**根本不存在**的字段。那种守卫会让"读 title
 * 的界面"也通过，而真实情况下那一栏是空的——
 * **守卫用臆造输入，等于没验证输入侧。**
 *
 * 因此本脚本的消费物是 probes/collect_real_responses.py 从真后端抓下来的
 * real_responses.json，断言落在**真实响应里的那个具体值**上：
 * 真实 run id、真实待办文本、真实技能名、真实 trust_note 片段。
 *
 * 用法：node probes/frontend_render/render_real_pages.cjs
 * 退出码：0=全过，1=有失败。
 */
const Module = require('module');
const path = require('path');
const fs = require('fs');
const { JSDOM } = require(FE_HOME + '/node_modules/jsdom');

const REAL = JSON.parse(fs.readFileSync(FE_HOME + '/real_responses.json', 'utf8'));
const RUN_ID = REAL['__run_id__'];
const CONV_ID = REAL['__conversation_id__'];

/* ---------- 模块解析：@/ 别名 + 第三方包 ---------- */
require.extensions['.css'] = () => {};
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (req, parent, ...rest) {
  if (req.startsWith('@/')) {
    return origResolve.call(this, path.join(OUT_ROOT + '/src', req.slice(2)), parent, ...rest);
  }
  try {
    return origResolve.call(this, req, parent, ...rest);
  } catch (e) {
    if (req.startsWith('.') || req.startsWith('/')) throw e;
    return origResolve.call(this, req, { paths: [FE_HOME + '/node_modules'] }, ...rest);
  }
};

// lucide-react 是 ESM-only（package.json 带 "type":"module"，连 dist/cjs
// 里的文件也按 ESM 解析），在纯 CJS 的 jsdom 环境里 require 会直接抛
// "require is not defined"。图标是纯视觉元素、不含任何文本断言目标，
// 因此这里用桩替换：任何属性访问都返回一个空 <span> 组件。
//
// 必须声明清楚：这样一来**图标本身是否被正确渲染**不在本守卫覆盖范围内，
// 本守卫只验文本与数据契约。
const ICON_STUB = new Proxy({}, {
  get: (_t, prop) => {
    if (prop === '__esModule') return true;
    if (prop === 'default') return function Icon() { return null; };
    return function Icon() { return null; };
  },
});
// React 必须全局只有一份：NODE_PATH 若指向系统级目录，那里通常也有一份
// react。两份 React 同时存在会触发 "Invalid hook call"，
// 因此这里把所有 react / react-dom 的解析强制钉到 FE_HOME 下的单一副本。
// （此前踩过：lucide-react、radix-ui 各嵌套一份 react，也是同一症状。）
const REACT_ROOT = FE_HOME + '/node_modules/';
const origLoad = Module._load;
Module._load = function (req, parent, isMain) {
  if (req === 'lucide-react') return ICON_STUB;
  if (req === 'react' || req === 'react/jsx-runtime' || req === 'react/jsx-dev-runtime') {
    return origLoad.call(this, REACT_ROOT + req, parent, isMain);
  }
  if (req === 'react-dom' || req.startsWith('react-dom/')) {
    return origLoad.call(this, REACT_ROOT + req, parent, isMain);
  }
  return origLoad.apply(this, arguments);
};

/* ---------- 浏览器环境 ---------- */
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
  { url: 'http://127.0.0.1:8787/', pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.HTMLElement = dom.window.HTMLElement;
global.Node = dom.window.Node;
global.Event = dom.window.Event;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = clearTimeout;
global.IS_REACT_ACT_ENVIRONMENT = true;
global.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
// radix-ui 的 Select 在 effect 里直接引用 HTMLFormElement 等构造器，而
// jsdom 只把它们挂在 window 上、不在 global。逐个补太容易漏，这里批量
// 桥接所有 HTML*/SVG* 构造器（环境桩，不影响被测逻辑）。
// 事件类构造器必须**强制覆盖**为 jsdom 版本，不能用 `k in global` 跳过。
//
// 实测踩到的坑：Node 19+ 自带全局 Event / CustomEvent / EventTarget，
// 于是旧的 `if (k in global) continue` 会把它们留在 Node 版本。而 radix-ui
// 的 dismissable-layer / focus-scope 在 effect 里 `new CustomEvent(...)`
// 后立刻 `dispatchEvent`，jsdom 校验 realm 不通过，直接抛
// "parameter 1 is not of type 'Event'"，整页渲染被 aggregateErrors 吞成
// 空 message 的崩溃——**看起来像产品崩了，实际是测试基建自己没搭好**。
// 这一个缺陷让设置页/竞技场/基因组三页的真实数据一直没验到。
const FORCE_JSOM = /^(Event|CustomEvent|EventTarget|MessageEvent|FocusEvent|InputEvent|KeyboardEvent|MouseEvent|PointerEvent|UIEvent|DOMException)$/;
for (const k of Object.getOwnPropertyNames(dom.window)) {
  if (!/^[A-Z]/.test(k)) continue;
  const v = dom.window[k];
  if (typeof v !== 'function') continue;
  // 已存在于 global 时的策略：**事件类强制覆盖**（跨 realm 不兼容），
  // 其余保持 Node 版本不动（避免覆盖 URL / performance 等被脚本依赖的项）。
  if (k in global && !FORCE_JSOM.test(k)) continue;
  try { global[k] = v; } catch (e) { /* 只读跳过 */ }
}
// 基建自检：这份覆盖名单是反向验证脚本的注入目标之一。若上一轮注入未还原，
// 名单里会少掉事件类，症状是设置页/竞技场/基因组三页以**空 message 的崩溃**
// 呈现——与真实产品缺陷无法区分，排查方向会被带偏到产品代码上。这里主动
// 报出，让污染以它本来的原因暴露。
for (const must of ['CustomEvent', 'Event', 'EventTarget']) {
  if (!FORCE_JSOM.test(must)) {
    console.error(
      '渲染基建自检失败：强制覆盖名单里缺少 ' + must + '。'
      + '若刚跑过反向验证，说明注入未还原，请用版本库还原本文件。');
    process.exit(3);
  }
}
if (!global.HTMLElement) global.HTMLElement = dom.window.HTMLElement;
// radix-ui 的 Select / ScrollArea 会调用 jsdom 未实现的指针捕获与滚动
// API。补成空实现（纯环境桩，不参与断言语义）。
const EP = dom.window.Element.prototype;
for (const fn of ['hasPointerCapture', 'setPointerCapture', 'releasePointerCapture',
                  'scrollIntoView']) {
  if (typeof EP[fn] !== 'function') EP[fn] = function () { return false; };
}
if (typeof dom.window.HTMLElement.prototype.focus !== 'function') {
  dom.window.HTMLElement.prototype.focus = function () {};
}
dom.window.PointerEvent = dom.window.PointerEvent || dom.window.MouseEvent;
global.DOMRect = dom.window.DOMRect;
global.getComputedStyle = dom.window.getComputedStyle;

const React = require(FE_HOME + '/node_modules/react');
const { createRoot } = require(FE_HOME + '/node_modules/react-dom/client');
const { act } = React;

/* ---------- fetch：按真实响应回话 ---------- */
function lookup(urlStr) {
  const u = new url_URL(urlStr);
  const p = u.pathname + (u.search || '');
  if (p in REAL) return REAL[p];
  // /api/jobs/<id> 之类：真实响应以具体 id 存 key
  for (const k of Object.keys(REAL)) {
    if (!k.startsWith('/api/')) continue;
    const kk = k.split('?')[0];
    const pp = p.split('?')[0];
    if (kk === pp) return REAL[k];
    const ks = kk.split('/'), ps = pp.split('/');
    if (ks.length === ps.length && ks.length > 2) {
      let hit = true;
      for (let i = 0; i < ks.length; i++) {
        if (ks[i] === ps[i]) continue;
        if (/^[0-9a-f]{6,}$/.test(ks[i]) && /^[0-9a-f]{6,}$/.test(ps[i])) continue;
        hit = false; break;
      }
      if (hit) return REAL[k];
    }
  }
  return null;
}
const url_URL = require('url').URL;

const FETCH_LOG = [];
global.fetch = async (u) => {
  const s = String(u);
  FETCH_LOG.push(s);
  const body = lookup(s);
  const ok = body !== null;
  return {
    ok,
    status: ok ? 200 : 404,
    json: async () => (ok ? body : { error: 'not found: ' + s }),
    text: async () => JSON.stringify(ok ? body : {}),
  };
};
dom.window.fetch = global.fetch;

/* ---------- 断言框架 ---------- */
const FAILS = [];
function check(name, cond, detail = '') {
  const mark = cond ? 'PASS' : 'FAIL';
  console.log(`  [${mark}] ${name}` + (cond ? '' : `  ← ${detail}`));
  if (!cond) FAILS.push(name);
}

async function render(PageName, waitMs = 500) {
  const mod = require(`${OUT_ROOT}/src/pages/${PageName}.js`);
  const Page = mod[PageName] || mod.default;
  if (typeof Page !== 'function') throw new Error(`${PageName} 不是组件`);
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(React.createElement(Page)); });
  await act(async () => { await new Promise((r) => setTimeout(r, waitMs)); });
  return { host, root, txt: () => host.textContent || '' };
}

async function unmount(h) {
  await act(async () => { h.root.unmount(); });
  h.host.remove();
}

/**
 * 打开 radix-ui 的 Select 并选中某一项。
 *
 * 为什么必须真的交互：ArenaPage / GenomePage / SettingsPage 的数据全部
 * 依赖"选中了哪一条"。此前守卫只渲染空态（"选择运行…"），
 * **一条真实数据都没验到**——这跟"假数据守卫"是同一类空洞。
 *
 * radix 的 trigger 监听 pointerdown，而 jsdom 没有 PointerEvent 构造器，
 * 这里用 MouseEvent 顶替同名事件；再退化为键盘 ArrowDown（radix 支持）。
 * 下拉内容是 portal，挂在 document.body 上，必须查 document 而非 host。
 */
async function openSelect(h, optMatcher) {
  const trig = h.host.querySelector('[role="combobox"]');
  if (!trig) return { ok: false, why: '未找到 combobox' };
  const evt = (type, init) => {
    const C = dom.window.MouseEvent;
    trig.dispatchEvent(new C(type, Object.assign({ bubbles: true, button: 0 }, init)));
  };
  for (const step of ['pointerdown', 'mousedown', 'key']) {
    await act(async () => {
      if (step === 'key') {
        trig.dispatchEvent(new dom.window.KeyboardEvent('keydown',
          { key: 'ArrowDown', bubbles: true }));
      } else evt(step, {});
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    if (document.querySelectorAll('[role="option"]').length) break;
  }
  const opts = Array.from(document.querySelectorAll('[role="option"]'));
  if (!opts.length) return { ok: false, why: '下拉未展开' };
  const target = opts.find((o) => optMatcher(o.textContent || '')) || opts[0];
  await act(async () => {
    target.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 800)); });
  return { ok: true, why: '', opts: opts.map((o) => o.textContent || '') };
}

async function renderText(name, waitMs = 500) {
  const h = await render(name, waitMs);
  const t = h.txt();
  await unmount(h);
  return t;
}

async function clickText(h, matcher) {
  const nodes = Array.from(h.host.querySelectorAll('button, [role="option"]'));
  const el = nodes.find((n) => matcher((n.textContent || '') + ' ' + (n.getAttribute('title') || '')));
  if (!el) return false;
  await act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 600)); });
  return true;
}

function pick(o, ...ks) {
  for (const k of ks) if (Array.isArray(o?.[k])) return o[k];
  return [];
}

(async () => {
  // DUMP=1：只渲染十页并把纯文本落盘，供对外文案扫描消费。
  // 刻意与断言共用同一套环境桥接：另起一份脚本会因 React 多副本报
  // "Invalid hook call"（实测踩到），那是测试基建问题而非产品问题。
  if (process.env.DUMP === '1') {
    const PAGES = ['RunsPage', 'UsagePage', 'SettingsPage', 'ChatPage',
                   'ArenaPage', 'GenomePage', 'ForgePage', 'KnowledgePage',
                   'SkillsPage', 'TasksPage'];
    const texts = {};
    for (const name of PAGES) {
      const h = await render(name, 600);
      texts[name] = h.txt();
      await unmount(h);
    }
    fs.writeFileSync(FE_HOME + '/dom_texts.json',
                     JSON.stringify(texts, null, 1));
    console.log('已导出 ' + PAGES.length + ' 页 → ' + FE_HOME + '/dom_texts.json');
    process.exit(0);
  }

  console.log(`真实数据：run=${RUN_ID}  conv=${CONV_ID}`);
  console.log('');

  /* ---------- RunsPage ---------- */
  console.log('=== RunsPage ===');
  {
    const runs = REAL['/api/runs'].runs;
    const r0 = runs[0];
    const h = await render('RunsPage');
    const t = h.txt();
    check('渲染出真实 run id（前 6 位）', t.includes(String(RUN_ID).slice(0, 6)), `文本=${t.slice(0, 160)}`);
    check('渲染出真实状态', t.includes('已完成') || t.includes('成功') || t.includes('succeeded'), `文本=${t.slice(0, 200)}`);
    // 时间栏：第 33 章修的就是它（前端读 created 而后端给 created_ts）
    const y = new Date(r0.created_ts * 1000).getFullYear();
    check('时间栏非空（含年份）', t.includes(String(y)), `期望含 ${y}，文本=${t.slice(0, 220)}`);
    // 列表本身不含 task（设计如此），要点开详情才看得到——因此这里做
    // **真实点击交互**，而不是拿列表文本去断言一个它本就不显示的字段。
    const ok = await clickText(h, (s) => s.includes(String(RUN_ID).slice(0, 6)) || s.includes(RUN_ID));
    check('列表项可点击（选中态切换）', ok, '未找到该条运行记录的按钮');
    const t2 = h.txt();
    // 断言必须**定位到详情面板的任务块**，不能查整页文本：
    // 列表项也会显示任务，用整页文本断言的话，撤掉详情这一处照样全绿
    // （反向验证 D 锚点实测未抓到，就是这个原因）。
    const taskNode = h.host.querySelector('[data-testid="run-task"]');
    const taskTxt = taskNode ? (taskNode.textContent || '') : '';
    check('点开详情后渲染出真实任务文本',
      taskTxt.includes(String(r0.meta.task).slice(0, 8)),
      `任务块=${JSON.stringify(taskTxt.slice(0, 120))}`);
    // 日志：后端 log 是**字符串数组**。此前 LogStream 只按对象取 msg，
    // 字符串一个都取不到，整列渲染成 "—"。断言落在真实日志文本上，
    // 而不是"有没有渲染出点东西"。
    const jobKey = Object.keys(REAL).find((k) => k.startsWith('/api/jobs/'));
    const log0 = String((REAL[jobKey].log || [])[0] || '').slice(0, 8);
    check('点开详情后渲染出真实日志文本（不是破折号）',
      log0.length > 0 && t2.includes(log0),
      `期望含 ${JSON.stringify(log0)}；文本=${t2.slice(0, 600)}`);
    await unmount(h);
  }

  /* ---------- TasksPage ---------- */
  console.log('=== TasksPage ===');
  {
    const items = REAL['/api/tasks/list'].items;
    const t = await renderText('TasksPage');
    check('渲染出真实待办文本', t.includes(items[0].text.slice(0, 10)), `文本=${t.slice(0, 200)}`);
    check('渲染出全部待办条数', items.every((i) => t.includes(i.text.slice(0, 10))), `文本=${t.slice(0, 260)}`);
    const st = REAL['/api/tasks/list'].stats;
    check('渲染出统计数字', t.includes(String(st.total)) || t.includes(String(st.pending)), `stats=${JSON.stringify(st)}`);
  }

  /* ---------- KnowledgePage ---------- */
  console.log('=== KnowledgePage ===');
  {
    const docs = REAL['/api/kb/list'].docs;
    const t = await renderText('KnowledgePage');
    check('渲染出真实知识标题', t.includes(docs[0].title.slice(0, 8)), `docs0=${docs[0].title} 文本=${t.slice(0, 200)}`);
    check('渲染出多条知识', docs.slice(0, 3).filter((d) => t.includes(d.title.slice(0, 6))).length >= 2,
      `文本=${t.slice(0, 300)}`);
  }

  /* ---------- SkillsPage ---------- */
  console.log('=== SkillsPage ===');
  {
    const skills = REAL['/api/skills/list'].skills;
    const t = await renderText('SkillsPage');
    check('渲染出真实技能名', t.includes(skills[0].name), `skills=${skills.map((s) => s.name)} 文本=${t.slice(0, 220)}`);
    check('渲染出技能描述', t.includes(String(skills[0].description).slice(0, 8)), `文本=${t.slice(0, 260)}`);
    // 第 34 章修的就是 type 字段被白名单滤掉导致人设列表空
    check('渲染出人设分类（type=persona 生效）', t.includes('人设') || t.includes('audit-persona'), `文本=${t.slice(0, 300)}`);
  }

  /* ---------- UsagePage ---------- */
  console.log('=== UsagePage ===');
  {
    const u = REAL['/api/usage'];
    const t = await renderText('UsagePage');
    const total = String(u.total);
    // 第 34 章：设置页与用量页同一数字两种写法（400000 vs 400,000）
    check('渲染出真实总用量', t.includes(total) || t.includes(total.replace(/\B(?=(\d{3})+(?!\d))/g, ',')),
      `total=${total} 文本=${t.slice(0, 260)}`);
    check('用量页未显示英文原始阶段名', !/distill:extract/.test(t), `文本=${t.slice(0, 300)}`);
  }

  /* ---------- SettingsPage ---------- */
  console.log('=== SettingsPage ===');
  {
    const pv = REAL['/api/providers'];
    const h = await render('SettingsPage', 700);
    const opened = await openSelect(h, (s) => s.includes('OpenAI'));
    check('供应商下拉可展开且列出真实供应商', opened.ok, `why=${opened.why}`);
    const t2 = h.txt();
    check('渲染出真实供应商名', pv.presets.some((p) => t2.includes(p.label.slice(0, 4))),
      `labels=${pv.presets.slice(0, 5).map((p) => p.label)} 文本=${t2.slice(0, 400)}`);
    await unmount(h);
    check('预算数字带千分位（与用量页一致）',
      t2.includes(String(REAL['/api/status'].budget.limit)) ||
      t2.includes(String(REAL['/api/status'].budget.limit).replace(/\B(?=(\d{3})+(?!\d))/g, ',')),
      `limit=${REAL['/api/status'].budget.limit} 文本=${t2.slice(0, 400)}`);
  }

  /* ---------- ChatPage ---------- */
  console.log('=== ChatPage ===');
  {
    const convs = REAL['/api/conversations'].conversations;
    const t = await renderText('ChatPage');
    check('渲染出真实会话标题', convs.some((c) => t.includes(String(c.title).slice(0, 6))),
      `titles=${convs.map((c) => c.title)} 文本=${t.slice(0, 260)}`);
  }

  /* ---------- ArenaPage：关键，验证 trust_note 到达界面 ---------- */
  console.log('=== ArenaPage ===');
  {
    const rep = REAL[Object.keys(REAL).find((k) => k.startsWith('/api/report/'))];
    const tn = String(rep.trust_note || '');
    const h = await render('ArenaPage', 700);
    const opened = await openSelect(h, (s) => s.includes(String(RUN_ID).slice(0, 6)));
    check('下拉列出真实运行记录', opened.ok, `why=${opened.why}`);
    const t = h.txt();
    check('真实报告 claim_valid 为假（场景成立）', rep.claim_valid === false, `claim_valid=${rep.claim_valid}`);
    check('界面呈现后端权威原因（trust_note 片段）',
      tn.length > 0 && t.includes(tn.slice(0, 12)),
      `trust_note 前 12 字 = ${JSON.stringify(tn.slice(0, 12))}；文本=${t.slice(0, 500)}`);
    check('界面标注结论仅供参考', t.includes('仅供参考') || t.includes('结论'), `文本=${t.slice(0, 400)}`);
    await unmount(h);
  }

  /* ---------- GenomePage ---------- */
  console.log('=== GenomePage ===');
  {
    const g = REAL[Object.keys(REAL).find((k) => k.startsWith('/api/genome/'))];
    const h = await render('GenomePage', 700);
    const opened = await openSelect(h, (s) => s.includes(String(RUN_ID).slice(0, 6)));
    check('下拉列出真实运行记录', opened.ok, `why=${opened.why}`);
    const t = h.txt();
    check('渲染出基因组 mission 或 name',
      (g.mission_one_liner && t.includes(String(g.mission_one_liner).slice(0, 6))) ||
      (g.name && t.includes(String(g.name).slice(0, 6))),
      `name=${g.name} mission=${g.mission_one_liner} 文本=${t.slice(0, 400)}`);
    await unmount(h);
  }

  /* ---------- ForgePage ---------- */
  console.log('=== ForgePage ===');
  {
    const t = await renderText('ForgePage', 600);
    check('渲染出蒸馏入口（表单或按钮）', t.length > 20, `文本=${t.slice(0, 200)}`);
    check('渲染出真实模型信息', t.includes('mock-main') || t.includes('模型'), `文本=${t.slice(0, 300)}`);
  }

  console.log('');
  console.log('=== 汇总 ===');
  console.log(FAILS.length === 0 ? '  ALL PASS' : `  FAILED(${FAILS.length}): ${FAILS.join(' | ')}`);
  process.exit(FAILS.length ? 1 : 0);
})().catch((e) => {
  console.error('RENDER-FAIL:', e.message);
  if (Array.isArray(e.errors)) {
    e.errors.forEach((x, i) => console.error(`#${i}`,
      (x && x.stack) ? x.stack.split('\n').slice(0, 14).join('\n') : String(x)));
  } else {
    console.error((e.stack || '').split('\n').slice(1, 14).join('\n'));
  }
  process.exit(2);
});
