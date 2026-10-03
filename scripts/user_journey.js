// 人类用户旅程：真实浏览器打开真实构建产物，像人一样点按钮、填输入框、
// 并检查屏幕上真的出现了什么。
//
// 与逐页渲染核查的区别：那个只验"页面画得出来"，这个验"功能真的能用"。
// 与接口验收的区别：那个直接打 HTTP，这个走界面——用户不会用 curl。
// 界面上按钮写错了、点了没反应、数据写进去列表不刷新，只有这条路能发现。
//
// 每个功能必须用两次：只用一次验不出"覆盖而非累加"（第二条把第一条顶掉，
// 列表里只剩最新的，只写一次时列表有内容、读回有值，全绿）。
//
// 删除必须按"条目自身"定位按钮，不能按第几个：列表顺序是新的在前，
// 按序号点会把断言方向弄反，得出与事实相反的结论。
//
// 用法: node user_journey.js <url>
// 输出: 每行一个 JSON 步骤结果，末行 {"done":true,"fail":N}
// Playwright 位置因环境而异：沙盒里装在全局目录，CI 上装在工作区内。
// 两处都要能找到，否则脚本在另一处直接退出——而退出表现为"没跑这一步"，
// 与"这一步不需要跑"在日志上无法区分。
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

// 技能包必须由 scripts/make_journey_fixtures.py 生成并以环境变量传入。
// 不能用 '/tmp/jskill-a' 这种 POSIX 路径兜底：runner 上 shell 是 Git Bash，
// 它的 /tmp 与 Windows 版 Python 看到的 /tmp 不是同一个地方，兜底路径在
// runner 上永远不存在，报「缺少技能说明文件」——症状像"安装功能坏了"，
// 而根因是路径视图不同（run77 真实失败）。
// 缺变量必须立刻失败并说清原因；静默兜底会把根因藏起来。
for (const k of ['OF_SKILL_A', 'OF_SKILL_B']) {
  if (!process.env[k]) {
    console.error(`DRIVER-FAIL:缺少 ${k}；请先运行 scripts/make_journey_fixtures.py 并 source 其输出`);
    process.exit(2);
  }
}

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

async function fillByPlaceholder(page, ph, value) {
  const el = await page.$(`[placeholder="${ph}"]`);
  if (!el) throw new Error('找不到输入框: ' + ph);
  await el.click();
  await el.fill(value);
}

async function mainText(page) {
  return page.evaluate(() => {
    const m = document.querySelector('main') || document.body;
    return (m.innerText || '').replace(/\s+/g, ' ').trim();
  });
}

// 在页面内按文字点按钮。
// 不走 Playwright 的可操作性检查：页面有轮询刷新，节点会持续重渲染，
// 外部点击等到超时的症状是"点不动"，而按钮一直都在——会把人带去查按钮渲染。
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

// 在"包含指定文字的那个条目块"内点某个按钮。
// 必须按内容定位：列表顺序可变，按序号点会点到别的条目。
async function clickButtonInItem(page, itemText, btnText) {
  const handle = await page.evaluateHandle((args) => {
    // 必须取最内层的那个块：外层容器也含该文字，且里头往往有别的按钮。
    // 例如待办页外层含"显示已完成"，而"完成"是它的子串——按外层取
    // 会点到"显示已完成"，点了之后列表展现出乎意料，结论与事实相反。
    const blocks = Array.from(document.querySelectorAll('main div'))
      .filter((b) => (b.innerText || '').includes(args.itemText))
      .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    for (const b of blocks) {
        // 有些按钮是纯图标（只有 title，没有文字），只按 innerText 找会找不到，
        // 于是判定变成"界面没有这个入口"——与真缺失无法区分。
        const btn = Array.from(b.querySelectorAll('button'))
          .find((x) => (x.innerText || '').includes(args.btnText)
            || (x.getAttribute('title') || '').includes(args.btnText));
        if (btn) return btn;
    }
    return null;
  }, { itemText, btnText });
  const found = await page.evaluate((h) => !!h, handle);
  if (!found) throw new Error(`条目「${itemText}」里找不到按钮「${btnText}」`);
  // 在页面内点，不走 Playwright 的可操作性检查：
  // 列表会随数据重渲染，取出的节点可能已被替换，外部点击会一直等到超时，
  // 症状是"点不动"，而实际按钮一直都在。
  await page.evaluate((h) => h.click(), handle);
}

(async () => {
  const browser = await chromium.launch({
    // 未指定时用 Playwright 自带的 chromium
    executablePath: process.env.OF_CHROMIUM || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror:' + e.message));

  await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });

  // ---- 知识库：加两条、都在、删指定的一条、另一条还在 ----
  await goNav(page, '知识库');
  await fillByPlaceholder(page, '标题', '甲条知识');
  await fillByPlaceholder(page, '内容', '甲条正文');
  await clickButton(page, '添加');
  await page.waitForTimeout(900);

  let t = await mainText(page);
  step('知识库·添加第一条', t.includes('甲条知识'), t.slice(0, 120));

  await fillByPlaceholder(page, '标题', '乙条知识');
  await fillByPlaceholder(page, '内容', '乙条正文');
  await clickButton(page, '添加');
  await page.waitForTimeout(900);

  t = await mainText(page);
  // 关键：两条必须同时在——只验"有内容"的话，第二条覆盖第一条也全绿
  let both = t.includes('甲条知识') && t.includes('乙条知识');
  step('知识库·两条并存（非覆盖）', both, both ? '两条都在' : t.slice(0, 300));

  await clickButtonInItem(page, '乙条知识', '删除');
  await page.waitForTimeout(900);
  t = await mainText(page);
  const gone = !t.includes('乙条知识');
  const kept = t.includes('甲条知识');
  step('知识库·删除指定条目且未误删', gone && kept,
    `被删的没了=${gone} 留下的还在=${kept}`);

  // ---- 待办：建两个、完成一个、已完成里真有 ----
  await goNav(page, '待办');
  await fillByPlaceholder(page, '要做的事', '甲件待办');
  await clickButton(page, '添加');
  await page.waitForTimeout(800);
  await fillByPlaceholder(page, '要做的事', '乙件待办');
  await clickButton(page, '添加');
  await page.waitForTimeout(900);

  t = await mainText(page);
  both = t.includes('甲件待办') && t.includes('乙件待办');
  step('待办·两条并存（非覆盖）', both, both ? '两条都在' : t.slice(0, 300));

  const stats1 = await page.evaluate(() => {
    const m = (document.querySelector('main') || document.body).innerText || '';
    const mm = m.match(/共\s*(\d+)\s*项/);
    return mm ? parseInt(mm[1], 10) : -1;
  });
  step('待办·计数为 2', stats1 === 2, '实际=' + stats1);

  await clickButtonInItem(page, '乙件待办', '完成');
  await page.waitForTimeout(900);
  t = await mainText(page);
  // 完成后默认列表隐藏已完成：点"显示已完成"确认它真的进了已完成
  await clickButton(page, '显示已完成');
  await page.waitForTimeout(700);
  t = await mainText(page);
  step('待办·完成后进入已完成列表', t.includes('乙件待办'),
    t.includes('乙件待办') ? '已在已完成里' : '已完成里没有：' + t.slice(0, 250));

  // ---- 对话：发两条、两条都在 ----
  await goNav(page, '对话');
  // 输入框每次都要重新取：列表与消息区会随回复重渲染，
  // 复用上一次的句柄会填到已被替换掉的节点上——第二条根本没发出去，
  // 症状却是"第二条消息没有"，而实际是上一条还没答完。
  async function sendChat(text) {
    const boxes = await page.$$('main textarea, main input');
    if (boxes.length === 0) throw new Error('对话页找不到输入区');
    const box = boxes[boxes.length - 1];
    await box.click();
    await box.fill(text);
    await page.keyboard.press('Enter');
  }
  // 流式未结束时输入框不可用，必须等"思考中"消失再发下一条。
  async function waitReplyDone() {
    for (let i = 0; i < 40; i++) {
      const s = await mainText(page);
      if (!s.includes('思考中')) return true;
      await page.waitForTimeout(500);
    }
    return false;
  }
  const hasInput = (await page.$$('main textarea, main input')).length > 0;
  step('对话·有输入区', hasInput, hasInput ? '存在' : '无输入区');
  if (hasInput) {
    await sendChat('甲条消息');
    await waitReplyDone();
    let c = await mainText(page);
    step('对话·第一条发出并收到回复', c.includes('甲条消息'), c.slice(0, 120));

    await sendChat('乙条消息');
    await waitReplyDone();
    c = await mainText(page);
    both = c.includes('甲条消息') && c.includes('乙条消息');
    step('对话·两条并存（历史未丢）', both, both ? '都在' : c.slice(0, 250));
  }

  // ---- 技能：装两个、都在、调用一个 ----
  await goNav(page, '技能');
  const skillPaths = [
    [process.env.OF_SKILL_A, 'skill-a', '甲技能正文'],
    [process.env.OF_SKILL_B, 'skill-b', '乙技能正文'],
  ];
  for (const [p] of skillPaths) {
    await fillByPlaceholder(page, '在此粘贴目录路径', p);
    await clickButton(page, '安装');
    await page.waitForTimeout(1200);
  }
  t = await mainText(page);
  both = t.includes('skill-a') && t.includes('skill-b');
  step('技能·两个并存（非覆盖）', both, both ? '都在' : t.slice(0, 300));
  await clickButtonInItem(page, 'skill-a', '调用');
  await page.waitForTimeout(1200);
  t = await mainText(page);
  step('技能·调用返回提示词', t.includes('甲技能正文') || t.includes('调用提示词'),
    t.includes('甲技能正文') ? '拿到正文' : t.slice(0, 200));

  // ---- 系统能力：开启一次、离开再回来仍在（持久化）----
  await goNav(page, '设置');
  t = await mainText(page);
  step('设置·有系统能力面板', t.includes('系统能力'),
    t.includes('系统能力') ? '存在' : '缺少：' + t.slice(0, 200));
  // 文件系统那一行默认是"已关闭"：点它开启。
  // 必须真的能保存：工具执行的拒绝提示让用户来这里开，若开了不生效，
  // 与功能不存在无法区分。
  await clickButtonInItem(page, '读写本地文件', '已关闭');
  await page.waitForTimeout(1200);
  const onNow = await page.evaluate(() => {
    const blocks = Array.from(document.querySelectorAll('main div'));
    const b = blocks.find((x) => (x.innerText || '').includes('读写本地文件'));
    return b ? (b.innerText || '').includes('已开启') : false;
  });
  step('设置·能力开启生效', onNow, onNow ? '已开启' : '点了没变成已开启');

  // 离开再回来：开关必须还在开启状态。
  // 只验"点了变成已开启"的话，刷新即丢的实现也会通过——用户以为打开了，
  // 实际下一次进来还是关的，而界面当下确实是"已开启"。
  await goNav(page, '知识库');
  await goNav(page, '设置');
  await page.waitForTimeout(900);
  const stillOn = await page.evaluate(() => {
    const blocks = Array.from(document.querySelectorAll('main div'));
    const b = blocks.find((x) => (x.innerText || '').includes('读写本地文件'));
    return b ? (b.innerText || '').includes('已开启') : false;
  });
  step('设置·能力开启后持久化', stillOn,
    stillOn ? '离开再回来仍是已开启' : '回到设置后变回未开启');

  step('控制台无报错', consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(' | ') || '无');

  console.log(JSON.stringify({ event: 'done', fail: fails.length, steps: steps.length }));
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.error('DRIVER-FAIL:' + e.message.split('\n')[0]);
  process.exit(2);
});
