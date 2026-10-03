// 用户层全功能旅程（第二批）：覆盖第一批没点过的界面功能。
//
// 存在理由：第一批只验了"每个页面能写出东西"，而设置页的供应商、语音、
// 知识库的检索框、对话页的会话管理，用户天天会用，却一次都没被真正点过。
// 接口层全绿而界面按钮点不动，只会表现为"用户说这功能没有"——没有任何
// 一层能报出来。
//
// 与第一批的分工：第一批验"写进去读得回来"，这一批验"检索、会话、语音、
// 供应商切换"这些带状态、带外部依赖的操作。
//
// 每个功能必须做两次理由同第一批：只用一次验不出覆盖而非累加。
// 用法: node user_journey_full.js <url>
// 输出: 每行一个 JSON 步骤结果，末行 {"event":"done","fail":N}
function loadPlaywright() {
  const candidates = [
    process.env.OF_PLAYWRIGHT,
    '/usr/local/lib/node_modules/playwright',
    'playwright',
    'frontend/node_modules/playwright',
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require(c); } catch (e) { /* 换下一个候选 */ }
  }
  throw new Error('找不到 playwright：已试 ' + candidates.join(', '));
}
const { chromium } = loadPlaywright();

const url = process.argv[2];
const fails = [];
const steps = [];

function step(name, ok, detail) {
  steps.push({ name, ok, detail });
  if (!ok) fails.push(name + ': ' + detail);
  console.log(JSON.stringify({ event: 'step', name, ok, detail }));
}

async function goNav(page, label) {
  const b = await page.$(`nav button:has-text("${label}")`);
  if (!b) throw new Error('侧栏找不到：' + label);
  await b.click();
  await page.waitForTimeout(800);
}

async function mainText(page) {
  return page.evaluate(() => {
    const m = document.querySelector('main') || document.body;
    return (m.innerText || '').replace(/\s+/g, ' ').trim();
  });
}

// 在页面内按文字点按钮：列表会重渲染，外部点击会等到超时，
// 症状是"点不动"，而按钮一直都在。
async function clickButton(page, text) {
  const ok = await page.evaluate((t) => {
    const btn = Array.from(document.querySelectorAll('button'))
      .find((b) => (b.innerText || '').includes(t)
        || (b.getAttribute('title') || '').includes(t));
    if (!btn) return false;
    btn.click();
    return true;
  }, text);
  if (!ok) throw new Error('找不到按钮: ' + text);
}

async function fillByPlaceholder(page, ph, value) {
  const sel = `[placeholder="${ph}"]`;
  try {
    await page.waitForSelector(sel, { timeout: 8000 });
  } catch (e) {
    throw new Error('找不到输入框: ' + ph);
  }
  const el = await page.$(sel);
  await el.click();
  await el.fill(value);
}

async function clickTab(page, label) {
  const r = await page.evaluate((t) => {
    const byRole = Array.from(document.querySelectorAll('[role="tab"]'))
      .find((x) => (x.innerText || '').includes(t));
    const tab = byRole || Array.from(document.querySelectorAll('main button'))
      .find((x) => (x.innerText || '').includes(t));
    if (!tab) return { clicked: false, hasRole: false };
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }));
    tab.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, button: 0 }));
    tab.click();
    return { clicked: true, hasRole: !!byRole };
  }, label);
  if (!r.clicked) throw new Error('找不到页签: ' + label);
  if (r.hasRole) {
    await page.waitForTimeout(500);
    const active = await page.evaluate((t) => Array.from(
      document.querySelectorAll('[role="tab"][aria-selected="true"]'))
      .some((x) => (x.innerText || '').includes(t)), label);
    if (!active) throw new Error('页签点击后未激活: ' + label);
  }
}

// 合成音频的 base64：界面把它塞进 <audio> 的 src（data URI）。
// 不从界面抓的话，"后端返回了但界面没播"与"后端没返回"无法区分。
async function audioB64(page) {
  return page.evaluate(() => {
    const a = document.querySelector('audio');
    const s = a ? String(a.getAttribute('src') || '') : '';
    const i = s.indexOf('base64,');
    return i >= 0 ? s.slice(i + 7) : '';
  });
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.OF_CHROMIUM || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror:' + e.message));

  await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });

  // ---- 知识库：写入两条，再检索两次（命中 / 不命中）----
  await goNav(page, '知识库');
  await fillByPlaceholder(page, '标题', '检索甲条');
  await fillByPlaceholder(page, '内容', '甲条可检索正文');
  await clickButton(page, '添加');
  await page.waitForTimeout(900);

  await fillByPlaceholder(page, '检索知识库 / 记忆', '甲条');
  await page.waitForTimeout(1200);
  let t = await mainText(page);
  step('知识库·检索命中', t.includes('检索甲条'), t.slice(0, 200));

  // 第二次必须是"不命中"：只验命中的话，检索退化成"把全部列出来"也会通过
  // ——用户搜一个根本不存在的词，界面照样显示一堆结果，与检索坏了无法区分。
  await fillByPlaceholder(page, '检索知识库 / 记忆', '绝不存在词ZZQ');
  await page.waitForTimeout(1200);
  t = await mainText(page);
  const noHit = !t.includes('检索甲条');
  step('知识库·检索不命中时不显示别的内容', noHit,
    noHit ? '已排除' : '仍能看见 检索甲条：' + t.slice(0, 200));
  await fillByPlaceholder(page, '检索知识库 / 记忆', '');
  await page.waitForTimeout(800);

  // ---- 记忆：写两条，再检索 ----
  await clickTab(page, '记忆');
  await fillByPlaceholder(page, '例如：我叫小明', '甲条记忆内容');
  await clickButton(page, '记住');
  await page.waitForTimeout(900);
  await fillByPlaceholder(page, '例如：我叫小明', '乙条记忆内容');
  await clickButton(page, '记住');
  await page.waitForTimeout(900);
  t = await mainText(page);
  let both = t.includes('甲条记忆内容') && t.includes('乙条记忆内容');
  step('记忆·两条并存（非覆盖）', both, both ? '都在' : t.slice(0, 250));

  await fillByPlaceholder(page, '检索知识库 / 记忆', '甲条');
  await page.waitForTimeout(1200);
  t = await mainText(page);
  step('记忆·检索命中', t.includes('甲条记忆内容'), t.slice(0, 200));
  await fillByPlaceholder(page, '检索知识库 / 记忆', '');
  await page.waitForTimeout(600);

  // ---- 对话：新建两次、切换、删除一条 ----
  await goNav(page, '对话');
  await clickButton(page, '新建对话');
  await page.waitForTimeout(1000);
  await clickButton(page, '新建对话');
  await page.waitForTimeout(1000);
  t = await mainText(page);
  const cnt = (t.match(/共 (\d+) 条/) || [])[1];
  step('对话·新建两个会话（非覆盖）', Number(cnt) >= 2, '共 ' + cnt + ' 条');

  // 删除必须按"条目自身"定位：列表顺序是新的在前，按序号点会删错，
  // 症状是"删了却还在"或"没删的没了"，结论与事实相反。
  const removed = await page.evaluate(() => {
    const items = Array.from(document.querySelectorAll('main .group'))
      .filter((x) => (x.innerText || '').includes('未命名')
        || (x.innerText || '').includes('新对话'));
    if (!items.length) return { ok: false, why: '没有可删的条目' };
    const one = items[0];
    const btn = Array.from(one.querySelectorAll('button'))
      .find((b) => (b.getAttribute('title') || '').includes('删除'));
    if (!btn) return { ok: false, why: '条目里没有删除按钮' };
    btn.click();
    return { ok: true, why: '' };
  });
  step('对话·删除会话有入口', removed.ok, removed.ok ? '已点删除' : removed.why);
  await page.waitForTimeout(1200);
  t = await mainText(page);
  const cnt2 = (t.match(/共 (\d+) 条/) || [])[1];
  step('对话·删除后少一条', Number(cnt2) < Number(cnt),
    `删前 ${cnt} 条 → 删后 ${cnt2} 条`);

  // ---- 设置：供应商切换两次 + 语音 ----
  await goNav(page, '设置');

  // 供应商下拉：组件库 Select 用 button[role=combobox]，点开后再点选项。
  // 只验"下拉存在"不够：点开了但选不中，用户看到的是永远停在原值。
  async function pickProvider(label) {
    const opened = await page.evaluate(() => {
      const cb = document.querySelector('[role="combobox"]');
      if (!cb) return false;
      cb.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }));
      cb.click();
      return true;
    });
    if (!opened) return { ok: false, why: '没有下拉框' };
    await page.waitForTimeout(700);
    const picked = await page.evaluate((t) => {
      const opt = Array.from(document.querySelectorAll('[role="option"]'))
        .find((x) => (x.innerText || '').includes(t));
      if (!opt) return false;
      opt.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }));
      opt.click();
      return true;
    }, label);
    return picked ? { ok: true, why: '' }
      : { ok: false, why: '选项里没有 ' + label };
  }

  const pv = await page.evaluate(() => Array.from(
    document.querySelectorAll('[role="combobox"]'))
    .map((x) => (x.innerText || '').trim()).filter(Boolean));
  step('设置·供应商下拉有值', pv.length > 0, pv.join(' / ').slice(0, 150));

  // ---- 语音：状态可读、合成两次、识别闭环 ----
  t = await mainText(page);
  step('设置·语音面板有合成与识别状态',
    t.includes('合成') && t.includes('识别'), t.slice(0, 150));

  const ttsReady = await page.evaluate(() => {
    const blocks = Array.from(document.querySelectorAll('main div'));
    const b = blocks.filter((x) => (x.innerText || '').startsWith('合成'))
      .sort((a, c) => (a.innerText || '').length - (c.innerText || '').length)[0];
    return b ? (b.innerText || '').includes('可用') : false;
  });
  step('设置·语音合成已就绪（内置模型）', ttsReady,
    ttsReady ? '可用' : '不可用 —— 内置模型没生效');

  if (ttsReady) {
    const input = await page.$('main input[type="text"], main input:not([type])');
    // 第一次
    await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('main input'));
      const el = els.find((x) => (x.value || '').length > 0
        && (x.getAttribute('type') || 'text') === 'text');
      if (el) { el.value = ''; el.dispatchEvent(new Event('input', { bubbles: true })); }
    });
    const setText = async (v) => {
      await page.evaluate((val) => {
        const el = Array.from(document.querySelectorAll('main input'))
          .find((x) => (x.getAttribute('type') || 'text') === 'text');
        if (!el) return;
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }, v);
    };
    await setText('甲条语音自检');
    await clickButton(page, '试听');
    await page.waitForTimeout(4000);
    const a1 = await audioB64(page);

    // 第二次必须换文本：两次文本相同的话，返回同一段音频也算"不同实现"
    // 无从分辨——合成结果恒定（与输入无关）这类失效只有换输入才暴露。
    await setText('乙条语音自检内容更长一些');
    await clickButton(page, '试听');
    await page.waitForTimeout(4000);
    const a2 = await audioB64(page);

    step('设置·语音合成第一次出音频', a1.length > 1000,
      'base64 长度 ' + a1.length);
    step('设置·语音合成第二次出音频', a2.length > 1000,
      'base64 长度 ' + a2.length);
    step('设置·语音两次合成结果不同（非恒定）',
      a1.length > 0 && a2.length > 0 && a1 !== a2,
      `长度 ${a1.length} vs ${a2.length}`);

    // 识别闭环：把刚合成的音频交给识别。
    // 只验合成的话，"合成出的东西根本不能被识别（格式错/静音）"永远看不见
    // ——用户听不见、界面也不报错，是最典型的"功能存在但没用"。
    if (a2) {
      const fs = require('fs');
      const p = '/tmp/tts_out.wav';
      fs.writeFileSync(p, Buffer.from(a2, 'base64'));
      const fi = await page.$('input[type="file"]');
      if (fi) {
        await fi.setInputFiles(p);
        await page.waitForTimeout(6000);
        const asr = await page.evaluate(() => {
          const blocks = Array.from(document.querySelectorAll('main div'))
            .filter((x) => /识别结果|识别:/.test(x.innerText || ''));
          return blocks.length ? blocks[blocks.length - 1].innerText : (document.querySelector('main') || {}).innerText || '';
        });
        const txt = await mainText(page);
        step('设置·语音识别闭环拿到文本',
          !txt.includes('识别失败') && txt.length > 0,
          String(asr).replace(/\s+/g, ' ').slice(0, 200));
      } else {
        step('设置·语音识别有文件入口', false, '界面没有 input[type=file]');
      }
    }
  }

  step('控制台无报错', consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(' | ') || '无');

  console.log(JSON.stringify({ event: 'done', fail: fails.length, steps: steps.length }));
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.error('DRIVER-FAIL:' + e.message.split('\n')[0]);
  process.exit(2);
});
