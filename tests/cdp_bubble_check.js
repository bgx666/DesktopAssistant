// cdp_bubble_check.js —— 悬浮球交互端到端检查：单击=放大（展开面板）
// 用法：node cdp_bubble_check.js <cdpPort> [backendPort]
// 退出码：0 = 正常；1 = 单击未展开 / 入口缺失（回归时 e2e 必须红）
//
// 背景：单击行为从"看最近回复气泡"改为"放大面板"（最近气泡移到右键菜单）。
// 纯渲染层事件逻辑，pytest 测不到——这里用真·受信输入（CDP Input domain）实测。
const http = require('http');

const CDP_PORT = process.argv[2] || '9226';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${CDP_PORT}${path}`, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

async function waitPage(substr, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const pages = await getJson('/json');
      const p = pages.find((x) => x.url.includes(substr));
      if (p) return p;
    } catch { /* 未就绪 */ }
    await sleep(300);
  }
  throw new Error('未找到页面: ' + substr);
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let id = 0;
    ws.onopen = () => resolve({
      send(method, params = {}) {
        return new Promise((res) => {
          const mid = ++id;
          pending.set(mid, res);
          ws.send(JSON.stringify({ id: mid, method, params }));
        });
      },
      close: () => ws.close(),
    });
    ws.onerror = () => reject(new Error('ws error'));
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    };
  });
}

let failures = 0;
function check(name, ok, detail = '') {
  console.log((ok ? '  OK  ' : ' FAIL ') + name + (detail ? '  → ' + detail : ''));
  if (!ok) failures += 1;
}

(async () => {
  const bubble = await waitPage('bubble.html', 25000);
  const b = await connect(bubble.webSocketDebuggerUrl);
  await b.send('Runtime.enable');
  await sleep(1200);
  const evalJs = async (expr, awaitPromise = false) => {
    const r = await b.send('Runtime.evaluate',
      { expression: expr, returnByValue: true, awaitPromise });
    return r.result && r.result.result ? r.result.result.value : undefined;
  };

  check('preload：onShowReply（右键菜单「显示最近的气泡」入口）已暴露',
    (await evalJs("typeof window.planner.onShowReply === 'function'")) === true);

  // 收起面板 → 悬浮球形态；等动画稳定（防止迟到的 morph-out-done 覆盖随后展开）
  await evalJs(`(async () => {
    for (let i = 0; i < 30; i += 1) {
      const st = await window.planner.getPanelState();
      if (st === 'hidden') return 'hidden';
      if (st === 'shown') window.planner.hidePanel();
      await new Promise((r) => setTimeout(r, 300));
    }
    return await window.planner.getPanelState();
  })()`, true);
  const st0 = await evalJs("window.planner.getPanelState()", true);
  check('面板已收起（悬浮球形态）', st0 === 'hidden', String(st0));
  await sleep(1500);
  check('收起动画已稳定（仍为 hidden）',
    (await evalJs("window.planner.getPanelState()", true)) === 'hidden');

  // 真·受信单击（CDP Input domain）→ 应展开面板
  const rectRaw = await evalJs(`(() => {
    const r = document.getElementById('core').getBoundingClientRect();
    return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
  })()`);
  const { x, y } = JSON.parse(rectRaw);
  await b.send('Input.dispatchMouseEvent',
    { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
  await sleep(60);
  await b.send('Input.dispatchMouseEvent',
    { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
  let st1 = '';
  for (let i = 0; i < 40; i += 1) {
    st1 = await evalJs("window.planner.getPanelState()", true);
    if (st1 === 'shown') break;
    await sleep(300);
  }
  check('单击悬浮球 → 放大（展开面板）', st1 === 'shown', String(st1));
  b.close();

  console.log(failures ? `\n共 ${failures} 项失败` : '\n全部通过');
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error('FATAL:', e.message); process.exit(1); });
