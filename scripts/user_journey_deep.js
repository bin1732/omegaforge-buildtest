// 深度人类用户旅程：覆盖核心流水线五个页面。
//
// 与 user_journey.js 的区别：那个覆盖增/删/改类功能，
// 这个覆盖"跑一遍完整流水线"——蒸馏、基因组、竞技场、运行记录、用量。
// 这几个页面此前只在接口与管道层验过，界面上从未被点过一次。
//
// 为什么必须跑两次蒸馏：只跑一次验不出"产物与输入无关"。
// 两次输入不同、产物却同名同分时，单次跑照样全绿，
// 而用户拿到的是两个一模一样、分不清谁是谁的蒸馏体。
//
// 为什么必须等"蒸馏中…"变回"开始蒸馏"才断言：
// 按钮文案是流水线状态的唯一可见出口。提前断言等于在流水线还没结束时
// 就去读产物，读到的是空或旧值，症状却像"产物有问题"。
//
// 用法: node user_journey_deep.js <url>
// 输出: 每行一个 JSON 步骤结果，末行 {"done":true,"fail":N}
function loadPlaywright() {
  const candidates = [
    process.env.OF_PLAYWRIGHT,
    '/usr/local/lib/node_modules/playwright',
    'playwright',
    // CI 上把 playwright 装在工作区（frontend/）里，而不是全局。
    // 少这一个候选的结果是脚本直接退出"找不到 playwright"，
    // 表现为"这一步没跑"，与"这一步不需要跑"在日志上无法区分。
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
  await page.waitForTimeout(700);
}

async function mainText(page) {
  return page.evaluate(() => {
    const m = document.querySelector('main') || document.body;
    return (m.innerText || '').replace(/\s+/g, ' ').trim();
  });
}

async function clickButton(page, text) {
  const ok = await page.evaluate((t) => {
    const btn = Array.from(document.querySelectorAll('button'))
      .find((b) => (b.innerText || '').includes(t)
        || (b.getAttribute('title') || '').includes(t));
    if (!btn) return false;
    btn.click();
    return true;
  }, text);
  return ok;
}

// 等"蒸馏中…"变回"开始蒸馏"。
// 不能固定 sleep：流水线耗时随输入变化，固定等待要么浪费时间，
// 要么在还没完成时就去读产物——后者的症状是"产物空/旧"，方向会被带偏。
async function waitDistillDone(page, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const t = await mainText(page);
    if (t.includes('开始蒸馏') && !t.includes('蒸馏中…')) return t;
    await page.waitForTimeout(1500);
  }
  return null;
}

// 在 Radix 下拉里选一项。
// 必须走真实点击：直接改 React state 绕过渲染，
// 那么"下拉里根本没有这一项"这种失效就永远验不到。
async function selectFromDropdown(page, placeholder, optionIndex) {
  const trig = await page.$(`[role="combobox"]:has-text("${placeholder}")`);
  if (!trig) {
    // 退一步：页面上任何带该 placeholder 字样的 trigger
    const any = await page.$$('[role="combobox"]');
    if (!any.length) return { ok: false, why: '页面没有下拉框' };
    await any[0].click();
  } else {
    await trig.click();
  }
  await page.waitForTimeout(600);
  const opts = await page.$$('[role="option"]');
  if (!opts.length) return { ok: false, why: '下拉展开后没有任何选项' };
  const idx = Math.min(optionIndex, opts.length - 1);
  const label = await opts[idx].innerText();
  await opts[idx].click();
  await page.waitForTimeout(1200);
  return { ok: true, label, count: opts.length };
}

async function distillOnce(page, source, task, tagName) {
  await goNav(page, '蒸馏工坊');
  await page.waitForTimeout(500);
  const src = await page.$('[placeholder="直接粘贴源 Agent 的系统提示词…"]');
  if (!src) throw new Error('蒸馏页找不到源提示词输入框');
  await src.click();
  await src.fill(source);
  const tsk = await page.$('[placeholder="描述一个用于对照评测的真实任务…"]');
  if (tsk) { await tsk.click(); await tsk.fill(task); }
  const clicked = await clickButton(page, '开始蒸馏');
  if (!clicked) throw new Error('找不到「开始蒸馏」按钮');
  await page.waitForTimeout(1500);
  const t = await waitDistillDone(page, 180000);
  if (t === null) throw new Error(`${tagName} 蒸馏超时未完成`);
  return t;
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

  // ---- 蒸馏：跑两次不同源，两次都必须真完成 ----
  const t1 = await distillOnce(
    page,
    'You are ArxivScholar. 你是一个学术论文检索助手，负责按主题检索 arXiv 论文并整理成结构化清单。',
    '检索最近关于检索增强生成的论文并列出五篇',
    '甲',
  );
  step('蒸馏·第一次真完成', t1.includes('蒸馏报告'), t1.slice(0, 160));

  const t2 = await distillOnce(
    page,
    'You are HomeChef. 你是一个家庭烹饪助手，负责按现有食材给出可执行的菜谱步骤。',
    '用鸡蛋和番茄给出一道十分钟完成的菜',
    '乙',
  );
  step('蒸馏·第二次真完成', t2.includes('蒸馏报告'), t2.slice(0, 160));

  // 两次产物不得相同：同名同分意味着产物与输入无关，
  // 用户拿到两个分不清谁是谁的蒸馏体，而单次跑全绿。
  const same = t1.replace(/\s+/g, ' ').trim() === t2.replace(/\s+/g, ' ').trim();
  step('蒸馏·两次产物不同（非恒定）', !same,
    same ? '两次报告文本完全相同' : '两次报告文本不同');

  // ---- 运行记录：两次蒸馏都留下记录 ----
  // 条数按 li 计数，不能按"蒸馏"字样：列表里显示的是任务描述与编号，
  // 不含"蒸馏"二字，按字样判会得到"没有记录"的反向结论。
  await goNav(page, '运行记录');
  await page.waitForTimeout(1500);
  const liCount = await page.evaluate(() => document.querySelectorAll('main li').length);
  const runsText = await mainText(page);
  step('运行记录·至少有两条', liCount >= 2, `条目数=${liCount}；${runsText.slice(0, 120)}`);

  // ---- 运行记录：设为基准 + 选对照 → 真出比对结果 ----
  const baseOk = await clickButton(page, '设为基准');
  step('运行记录·有「设为基准」入口', baseOk, baseOk ? '存在' : '界面没有该入口');

  if (baseOk) {
    await page.waitForTimeout(1000);
    // 按 testid 定位，不按标签名：页面里可能另有 select，按标签名取到的是别的框，
    // 症状是"选了没反应"，而比对根本没被触发。
    const bar = await page.$('[data-testid="compare-bar"]');
    let cmpOk = false, cmpDetail = '点了设为基准后没有出现比对栏';
    if (bar) {
      const sel = await page.$('[data-testid="compare-target"]');
      if (!sel) {
        cmpDetail = '比对栏出现了，但没有对照下拉（可能无可比对运行）';
      } else {
        const vals = (await sel.$$('option')).length
          ? await page.evaluate(() => Array.from(
              document.querySelectorAll('[data-testid="compare-target"] option')
            ).map((o) => o.value).filter(Boolean))
          : [];
        if (!vals.length) {
          cmpDetail = '对照下拉里没有任何可选项';
        } else {
          await sel.selectOption(vals[0]);
          await page.waitForTimeout(3000);
          const has = await page.$('[data-testid="compare-result"]');
          const txt = await mainText(page);
          cmpOk = !!has;
          cmpDetail = cmpOk
            ? '比对结果已渲染'
            : (txt.includes('暂无比对结果') ? '界面显示"暂无比对结果"' : txt.slice(0, 200));
        }
      }
    }
    step('运行记录·比对真出结果', cmpOk, cmpDetail);
  }

  // ---- 基因组：选一次运行 → 出结构解析 ----
  await goNav(page, '基因组');
  await page.waitForTimeout(1200);
  const g0 = await mainText(page);
  step('基因组·有可解剖的运行（非"还没有"）',
    !g0.includes('还没有可解剖的运行记录'), g0.slice(0, 150));

  const gs = await selectFromDropdown(page, '选择运行…', 0);
  step('基因组·下拉能选到运行', gs.ok, gs.ok ? `选中 ${gs.label}` : gs.why);

  const g1 = await mainText(page);
  step('基因组·出结构解析', g1.includes('结构解析') && !g1.includes('还没有'),
    g1.slice(0, 200));

  // ---- 竞技场：选一次运行 → 出裁决 ----
  await goNav(page, '竞技场');
  await page.waitForTimeout(1200);
  const a0 = await mainText(page);
  step('竞技场·有可对照的运行（非"还没有"）',
    !a0.includes('还没有可对照的运行记录'), a0.slice(0, 150));

  const as = await selectFromDropdown(page, '选择运行…', 0);
  step('竞技场·下拉能选到运行', as.ok, as.ok ? `选中 ${as.label}` : as.why);

  await page.waitForTimeout(2000);
  const a1 = await mainText(page);
  step('竞技场·出对照报告', a1.includes('对照报告'), a1.slice(0, 200));

  // ---- 用量：跑过流水线后应有真实数字 ----
  await goNav(page, '用量');
  await page.waitForTimeout(1500);
  const u1 = await mainText(page);
  const nums = (u1.match(/\d[\d,\.]*/g) || []).map((s) => Number(s.replace(/,/g, '')));
  const hasReal = nums.some((n) => n > 0);
  step('用量·有真实非零数字', hasReal, u1.slice(0, 200));

  // ---- 控制台 ----
  step('控制台无报错', consoleErrors.length === 0,
    consoleErrors.length ? consoleErrors.slice(0, 3).join(' | ') : '无');

  console.log(JSON.stringify({ done: true, fail: fails.length, fails }));
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.log(JSON.stringify({ event: 'fatal', error: String(e && e.message || e) }));
  process.exit(2);
});
