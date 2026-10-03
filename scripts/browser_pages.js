/* 逐页真机渲染核查：用真实浏览器打开真实构建产物，逐个点击侧边导航，
   记录每页的渲染结果与控制台报错。

   界面没有 URL 路由，页面只能靠点击切换——不点就永远停在首页。
   因此逐页核查必须能点击，只读首屏的写法覆盖不到其余九页。

   用法: node browser_pages.js <url> <outdir>
   输出: 每行一个 JSON（page 事件），末行 {"done":true,...}
*/
// 不写死绝对路径：本脚本只在沙盒的 Linux 上跑过，而 CI 的 runner 是 Windows，
// 硬编码 '/usr/local/lib/node_modules/playwright' 在那里必然 require 失败，
// 硬编码 '/usr/local/bin/chromium' 同样不存在。表现是"像素校验失败"，
// 真因却是驱动定位——会把排查方向带到渲染本身。
// 改为多候选 + 环境变量，与 user_journey.js 一致：playwright 模块由 node
// 从脚本所在目录向上解析（CI 上装在仓库根），浏览器由 playwright 自己找，
// 未显式指定 executablePath 时用它内置的那份。
function loadPlaywright() {
  const candidates = [
    process.env.OF_PLAYWRIGHT,
    '/usr/local/lib/node_modules/playwright',
    'playwright',
    'frontend/node_modules/playwright',
    'node_modules/playwright',
  ].filter(Boolean);
  for (const c of candidates) {
    try { return require(c); } catch (e) { /* 换下一个候选 */ }
  }
  throw new Error('找不到 playwright：已试 ' + candidates.join(', '));
}
const { chromium } = loadPlaywright();
const fs = require('fs');
const path = require('path');

const url = process.argv[2];
const outdir = process.argv[3] || '/tmp/pageshot';
fs.mkdirSync(outdir, { recursive: true });

const EXEC = process.env.OF_CHROMIUM || undefined;

(async () => {
  const browser = await chromium.launch({
    executablePath: EXEC,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });

  const errors = [];
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console:' + m.text());
  });
  page.on('pageerror', (e) => errors.push('pageerror:' + e.message));

  const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
  console.log(JSON.stringify({ event: 'goto', status: resp && resp.status() }));

  // 侧边导航按钮：界面无 URL 路由，取 nav 下的全部 button
  // 窄屏只留首字，宽屏显示全名：textContent 会把两者拼在一起，
  // 必须按可见文本取，否则标签带前缀、与期望清单对不上。
  const labels = await page.$$eval('nav button', (bs) =>
    bs.map((b) => (b.innerText || '').trim()).filter(Boolean));
  console.log(JSON.stringify({ event: 'nav', labels }));

  const results = [];
  for (const label of labels) {
    errors.length = 0;
    const btn = await page.$(`nav button:has-text("${label}")`);
    if (!btn) { console.log(JSON.stringify({ event: 'page', label, error: 'nav button not found' })); continue; }
    await btn.click();
    await page.waitForTimeout(900);
    const info = await page.evaluate(() => {
      const cur = document.querySelector('nav button[aria-current="page"]');
      const main = document.querySelector('main') || document.body;
      const txt = (main.innerText || '').replace(/\s+/g, ' ').trim();
      return {
        current: cur ? (cur.innerText || '').trim() : null,
        textLen: txt.length,
        nodes: document.querySelectorAll('main *').length,
        sample: txt.slice(0, 160),
        bodyNodes: document.querySelectorAll('body *').length,
      };
    });
    const shot = path.join(outdir, label.replace(/[^\w\u4e00-\u9fa5]/g, '_') + '.png');
    await page.screenshot({ path: shot });
    results.push({ label, ...info, errors: errors.slice(), shot });
    console.log(JSON.stringify({ event: 'page', label, ...info, errors: errors.slice(), shot }));
  }

  console.log(JSON.stringify({ event: 'done', count: results.length }));
  await browser.close();
})().catch((e) => {
  console.error('DRIVER-FAIL:' + e.message.split('\n')[0]);
  process.exit(1);
});
