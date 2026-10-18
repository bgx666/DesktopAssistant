// cdp_panel_check.js —— 变形面板（index.html）端到端检查：加载无异常 + 事件已绑定
// 用法：node cdp_panel_check.js <cdpPort> [backendPort]
//   backendPort 传入时额外走一遍 ask_user 弹窗全链路（mock 模式 POST /chat「考考我」触发）
// 退出码：0 = 面板正常；1 = 有加载期异常 / 面板不可交互（回归时 e2e 必须红）
//
// 为什么需要它：面板 DOM 与脚本的顺序、渲染层 JS 异常这类问题，pytest 测不到、
// 只查 bubble/toast 页面的旧 e2e 也测不到——历史上真出过「弹窗 DOM 放在 <script>
// 之后 → app.js 加载即 TypeError → 面板全哑（能显示、点不动）」的事故。
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CDP_PORT = process.argv[2] || '9226';
const BACKEND_PORT = process.argv[3] || '';
// 1x1 透明 PNG（真实字节；图片检查与看图检查共用）
const PNG_1PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64');

function postJson(url, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body || {});
    const req = http.request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
    }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

function getJson(path) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${CDP_PORT}${path}`, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

function backendGet(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitPage(substr, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const pages = await getJson('/json');
      const p = pages.find((x) => x.url.includes(substr));
      if (p) return p;
    } catch { /* 端口未就绪 */ }
    await sleep(300);
  }
  throw new Error('未找到页面: ' + substr);
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    const events = [];
    let id = 0;
    ws.onopen = () => resolve({
      events,
      send(method, params = {}, sessionId) {
        return new Promise((res) => {
          const mid = ++id;
          pending.set(mid, res);
          const msg = { id: mid, method, params };
          if (sessionId) msg.sessionId = sessionId;   // flatten 子会话（OOPIF 等）
          ws.send(JSON.stringify(msg));
        });
      },
      close: () => ws.close(),
    });
    ws.onerror = () => reject(new Error('ws error'));
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
      else if (msg.method) events.push(msg);
    };
  });
}

(async () => {
  // 1) 确保面板处于展开态：现在启动即自动展开，不要对已展开的面板再 toggle（那会收回）
  const bubble = await waitPage('bubble.html', 25000);
  const b = await connect(bubble.webSocketDebuggerUrl);
  await b.send('Runtime.enable');
  for (let i = 0; i < 25; i += 1) {
    const st = await b.send('Runtime.evaluate', {
      expression: 'window.planner.getPanelState()',
      returnByValue: true,
      awaitPromise: true,
    });
    const state = st.result && st.result.result ? st.result.result.value : '';
    if (state === 'shown') break;
    if (state === 'hidden') {
      await b.send('Runtime.evaluate', {
        expression: 'window.planner && window.planner.togglePanel && window.planner.togglePanel()',
        returnByValue: true,
      });
      await sleep(500);
    } else {
      await sleep(400);   // morphing_* → 等动画自己收尾
    }
  }
  b.close();

  // 2) 面板页：重载并捕获加载期异常
  const panel = await waitPage('index.html', 25000);
  const p = await connect(panel.webSocketDebuggerUrl);
  await p.send('Runtime.enable');
  await p.send('Log.enable');
  await p.send('Page.enable');
  await p.send('Page.reload', { ignoreCache: true });
  await sleep(4000);

  const errs = p.events.filter((e) => e.method === 'Runtime.exceptionThrown');
  errs.forEach((e) => {
    const d = e.params.exceptionDetails;
    const desc = (d.exception && d.exception.description || '').split('\n').slice(0, 3).join(' | ');
    console.error('  异常:', d.text, '||', desc, '|| at', d.url, d.lineNumber);
  });

  // 3) 交互验证：点日历 tab（app.js 绑定事件才会切 active）
  const click = await p.send('Runtime.evaluate', {
    expression: `(() => {
      const tab = document.querySelector('#tabs .tab[data-tab="calendar"]');
      if (!tab) return 'NO_TAB';
      tab.click();
      return tab.classList.contains('active') ? 'CLICK_OK' : 'CLICK_DEAD';
    })()`,
    returnByValue: true,
  });
  const clickResult = click.result && click.result.result && click.result.result.value;

  // 4) 状态条目录 chip 存在（未启用工作区时显示"未启用代码"）
  const badge = await p.send('Runtime.evaluate', {
    expression: "document.getElementById('cb-dir') ? 'BADGE_OK' : 'BADGE_MISSING'",
    returnByValue: true,
  });
  const badgeResult = badge.result && badge.result.result && badge.result.result.value;

  // 5) 面板尺寸：默认大窗（>600）+ 拖拽把手存在 + 程序化缩放真的改变窗口宽度
  const sizeCheck = await p.send('Runtime.evaluate', {
    expression: `(async () => {
      const handles = document.querySelectorAll('#resize-handles .rs').length;
      const before = window.innerWidth;
      if (typeof window.planner.setPanelSize !== 'function') return 'NO_SET';
      window.planner.setPanelSize(900, 700);
      await new Promise((r) => setTimeout(r, 450));
      const mid = window.innerWidth;
      window.planner.setPanelSize(680, 760);
      await new Promise((r) => setTimeout(r, 450));
      const after = window.innerWidth;
      const stage = getComputedStyle(document.documentElement).getPropertyValue('--stage-w').trim();
      if (handles !== 8) return 'HANDLES_BAD ' + handles;
      if (!(before > 600)) return 'SMALL_BAD ' + before;
      if (!(mid > 800)) return 'RESIZE_BAD mid=' + mid;
      if (!(after > 600 && after !== mid)) return 'RESTORE_BAD ' + after;
      if (stage !== after + 'px') return 'STAGE_BAD ' + stage + ' vs ' + after;
      return 'SIZE_OK';
    })()`,
    returnByValue: true,
    awaitPromise: true,
  });
  const sizeResult = sizeCheck.result && sizeCheck.result.result && sizeCheck.result.result.value;

  // 7) 输入框下方状态条：四个 chip 存在且已填充；菜单要**贴在按钮正上方**（回归：曾固定右对齐）
  const chips = await p.send('Runtime.evaluate', {
    expression: `(async () => {
      const ids = ['cb-dir', 'cb-mode', 'cb-model', 'cb-effort'];
      const missing = ids.filter((id) => !document.getElementById(id));
      if (missing.length) return 'CHIPS_MISSING ' + missing.join(',');
      // 模型 chip 文本来自 /llm/providers（异步）→ 等它从占位「模型」变成真实模型名
      let model = '';
      for (let i = 0; i < 24; i += 1) {
        model = (document.getElementById('cb-model').textContent || '').trim();
        if (model && model !== '模型') break;
        await new Promise((r) => setTimeout(r, 250));
      }
      const effort = (document.getElementById('cb-effort').textContent || '').trim();
      if (!model || model === '模型' || model.includes('—')) return 'MODEL_BAD ' + model;
      if (!['少想', '默认', '多想'].includes(effort)) return 'EFFORT_BAD ' + effort;
      // 布局（opencode 式）：chips 与发送按钮在输入框容器内，发送按钮贴底不留空档
      const bar = document.getElementById('input-bar');
      if (!bar
          || !bar.contains(document.getElementById('composer-bar'))
          || !bar.contains(document.getElementById('send'))
          || !bar.contains(document.getElementById('voice-btn'))) {
        return 'LAYOUT_OUTSIDE';
      }
      const brBottom = bar.getBoundingClientRect().bottom;
      const sendBottom = document.getElementById('send').getBoundingClientRect().bottom;
      if (brBottom - sendBottom > 14) return 'SEND_GAP ' + Math.round(brBottom - sendBottom);
      // 点开强度菜单 → 位置应贴在按钮正上方且在面板内
      const btn = document.getElementById('cb-effort');
      btn.click();
      await new Promise((r) => setTimeout(r, 200));
      const menu = document.getElementById('effort-menu');
      if (!menu || menu.classList.contains('hidden')) return 'MENU_HIDDEN';
      const br = btn.getBoundingClientRect();
      const mr = menu.getBoundingClientRect();
      const pr = document.getElementById('panel-ui').getBoundingClientRect();
      menu.classList.add('hidden');
      if (mr.bottom > br.top + 2) return 'MENU_NOT_ABOVE';
      if (Math.abs(mr.left - br.left) > 12) return 'MENU_OFFSET ' + Math.round(mr.left - br.left);
      if (mr.right > pr.right + 1 || mr.left < pr.left - 1) return 'MENU_OVERFLOW';
      return 'CHIPS_OK';
    })()`,
    returnByValue: true,
    awaitPromise: true,
  });
  const chipsResult = chips.result && chips.result.result && chips.result.result.value;

  // 8) 变更 tab：可切换 + 列表渲染（有记录或空占位）+ 刷新/清空按钮存在（回归：tab 切换要触发刷新）
  const changes = await p.send('Runtime.evaluate', {
    expression: `(async () => {
      const tab = document.querySelector('#tabs .tab[data-tab="changes"]');
      if (!tab) return 'NO_CHANGES_TAB';
      tab.click();
      await new Promise((r) => setTimeout(r, 900));
      if (!tab.classList.contains('active')) return 'CHANGES_TAB_DEAD';
      if (!document.getElementById('changes').classList.contains('active')) return 'CHANGES_VIEW_DEAD';
      if (!document.getElementById('changes-refresh') || !document.getElementById('changes-clear')) {
        return 'CHANGES_BTNS_MISSING';
      }
      const items = document.querySelectorAll('#changes-list .chg-item');
      if (!items.length) return 'CHANGES_EMPTY_RENDER';
      if (document.getElementById('changes-list').textContent.includes('加载失败')) {
        return 'CHANGES_LOAD_FAIL';
      }
      return 'CHANGES_OK';
    })()`,
    returnByValue: true,
    awaitPromise: true,
  });
  const changesResult = changes.result && changes.result.result && changes.result.result.value;

  // 9) 思考流（thinking_stream）：mock 发「多想一下」→ 思考块出现（流式文本 + 正文出现后折叠 + 点击可展开）
  //    注意顺序：必须先测思考、再测 ask_user —— mock 的「考我」触发词看的是全部历史消息，
  //    反过来的话「多想一下」会被历史里的「考考我」带偏去走提问分支。
  let thinkResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '多想一下' });
    } catch (e) {
      thinkResult = 'CHAT_FAILED ' + e.message;
    }
    if (thinkResult === 'SKIP') {
      const th = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          let block = null;
          for (let i = 0; i < 40 && !block; i += 1) {
            block = document.querySelector('.thinking-block');
            if (!block) await new Promise((r) => setTimeout(r, 250));
          }
          if (!block) return 'NO_THINKING_BLOCK';
          if (!block.classList.contains('collapsed')) return 'NOT_DEFAULT_COLLAPSED';
          const ticker = block.querySelector('.tb-ticker');
          const body = block.querySelector('.tb-body');
          if (!ticker || !body) return 'STRUCTURE_BAD';
          let ttext = ticker.textContent || '';
          for (let i = 0; i < 20 && !ttext.includes('mock 思考流'); i += 1) {
            await new Promise((r) => setTimeout(r, 250));
            ttext = ticker.textContent || '';
          }
          if (!ttext.includes('mock 思考流')) return 'TICKER_BAD ' + ttext.slice(0, 40);
          if (!(body.textContent || '').includes('mock 思考流')) return 'BODY_MISSING';
          block.querySelector('.tb-line').click();   // 点击展开全文
          await new Promise((r) => setTimeout(r, 120));
          if (block.classList.contains('collapsed')) return 'TOGGLE_FAIL';
          if (getComputedStyle(body).display === 'none') return 'BODY_NOT_VISIBLE';
          if (getComputedStyle(ticker).display !== 'none') return 'TICKER_NOT_HIDDEN';
          block.querySelector('.tb-line').click();   // 收回
          await new Promise((r) => setTimeout(r, 120));
          if (!block.classList.contains('collapsed')) return 'RE_TOGGLE_FAIL';
          return 'THINK_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      thinkResult = th.result && th.result.result && th.result.result.value;
    }
    // 重载页面（≈ 重开面板）→ 历史重建后思考行还必须存在（用户回归：之前思考不落历史）
    if (thinkResult === 'THINK_OK') {
      await p.send('Page.reload', { ignoreCache: false });
      const hist = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          for (let i = 0; i < 60; i += 1) {
            const ready = document.readyState === 'complete'
              && !!document.getElementById('composer-bar');
            const ticker = document.querySelector('.thinking-block .tb-ticker');
            if (ready && ticker && (ticker.textContent || '').includes('mock 思考流')) {
              return 'THINK_HISTORY_OK';
            }
            await new Promise((r) => setTimeout(r, 250));
          }
          const ticker = document.querySelector('.thinking-block .tb-ticker');
          return 'HISTORY_MISSING ' + (ticker ? (ticker.textContent || '').slice(0, 40) : 'no-block');
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      const histResult = hist.result && hist.result.result && hist.result.result.value;
      if (histResult !== 'THINK_HISTORY_OK') thinkResult = histResult;
      else thinkResult = 'THINK_OK+HIST';
    }
  }

  // 10) ask_user 提问卡全链路（指定 backendPort 时）：mock 模式发「考考我」→
  //     对话区内联提问卡（非遮罩弹窗）→ 点选项 → 卡片收起 + 工具结果
  let askResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '考考我' });
    } catch (e) {
      askResult = 'CHAT_FAILED ' + e.message;
    }
    if (askResult === 'SKIP') {
      const ask = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const card = () => document.querySelector('.question-inline:not(.resolved)');
          for (let i = 0; i < 40 && !card(); i += 1) {
            await new Promise((r) => setTimeout(r, 250));
          }
          const c = card();
          if (!c) return 'CARD_NOT_SHOWN';
          // 内联卡片：必须是消息流的一部分，且不得有任何全屏遮罩弹窗
          if (!c.closest('#messages')) return 'CARD_NOT_INLINE';
          if (document.getElementById('question')) return 'OLD_MODAL_STILL_EXISTS';
          const text = (c.querySelector('.question-text') || {}).textContent || '';
          if (!text.includes('先做哪个')) return 'TEXT_BAD ' + text.slice(0, 40);
          const btns = c.querySelectorAll('.question-options button');
          if (btns.length < 2) return 'OPTIONS_BAD ' + btns.length;
          btns[0].click();
          await new Promise((r) => setTimeout(r, 600));
          const c2 = card();
          if (c2) return 'CARD_NOT_SETTLED';
          const settled = document.querySelector('.question-inline.resolved');
          if (!settled || !(settled.textContent || '').includes('已选择')) return 'SETTLE_STATUS_BAD';
          for (let i = 0; i < 25; i += 1) {
            const all = [...document.querySelectorAll('#messages .msg, #messages .tool-line')]
              .map((e) => e.textContent).join(' ');
            if (all.includes('用户选择了')) return 'ASK_OK';
            await new Promise((r) => setTimeout(r, 300));
          }
          return 'TOOL_RESULT_MISSING';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      askResult = ask.result && ask.result.result && ask.result.result.value;
    }
  }

  // 11) 状态条合并 chip：原始消息上下文 + 缓存命中 + TPS（近 50 轮平均）；
  //     点击展开合并面板：上下文分解 + 用量明细（mock 用量确定性）
  let statsResult = 'SKIP';
  if (BACKEND_PORT) {
    const st = await p.send('Runtime.evaluate', {
      expression: `(async () => {
        const chip = document.getElementById('cb-stats');
        if (!chip) return 'NO_STATS_CHIP';
        if (chip.classList.contains('hidden')) return 'CHIP_HIDDEN';   // 曾因忘摘 hidden 而用户看不到
        let text = chip.textContent || '';
        for (let i = 0; i < 30 && text.includes('—'); i += 1) {
          await new Promise((r) => setTimeout(r, 300));
          text = chip.textContent || '';
        }
        if (!text.includes('原始') || !text.includes('命中') || !text.includes('t/s')) {
          return 'CHIP_BAD ' + text;
        }
        chip.click();
        await new Promise((r) => setTimeout(r, 500));
        const panel = document.getElementById('stats-panel');
        if (!panel || panel.classList.contains('hidden')) return 'PANEL_HIDDEN';
        // 合并面板：上半 = 上下文占用（单条堆叠 + 4 项图例），下半 = 用量明细
        const segs = document.querySelectorAll('#stats-panel #ctx-bars .ctx-stack i').length;
        if (segs < 1) return 'CTX_STACK_BAD ' + segs;
        const legend = document.querySelectorAll('#stats-panel #ctx-bars .ctx-legend-item').length;
        if (legend !== 4) return 'CTX_LEGEND_BAD ' + legend;
        const foot = document.getElementById('ctx-foot').textContent || '';
        if (!foot.includes('100 万窗口') || !foot.includes('token')) return 'CTX_FOOT_BAD ' + foot.slice(0, 40);
        const sum = document.getElementById('stats-summary').textContent || '';
        if (!sum.includes('缓存命中') || !sum.includes('平均 TPS')) return 'SUM_BAD ' + sum.slice(0, 60);
        const rows = document.querySelectorAll('#stats-list .stats-row').length;
        if (rows < 1) return 'NO_ROWS';
        document.getElementById('stats-close').click();
        return 'STATS_OK';
      })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    statsResult = st.result && st.result.result && st.result.result.value;
  }

  // 12) 工具组：相邻工具调用合并成一行（默认折叠，行内列出工具名）→ 展开逐行显示，
  //     组内每行可单独展开参数/结果；没有多工具组时退化为单行检查
  //     （放在各 mock 生成之后跑：此时必有工具调用行——heartbeat/add_schedule 等）
  const group = await p.send('Runtime.evaluate', {
    expression: `(async () => {
      let g = null;
      for (let i = 0; i < 20 && !g; i += 1) {
        g = document.querySelector('.tool-group:not(.single)');
        if (!g) await new Promise((r) => setTimeout(r, 250));
      }
      if (g) {
        if (g.classList.contains('open')) return 'GROUP_ALREADY_OPEN';
        const head = g.querySelector('.tg-line');
        const count = (head.querySelector('.tg-count').textContent || '');
        const names = (head.querySelector('.tg-title').textContent || '');
        if (!/\\d+ 个操作/.test(count)) return 'GROUP_COUNT_BAD ' + count.slice(0, 40);
        if (!names.trim()) return 'GROUP_NAMES_EMPTY';
        if (names.includes('…')) return 'NAMES_TRUNCATED ' + names.slice(-30);
        if (names.includes('×')) return 'NAMES_HAS_COUNT ' + names.slice(0, 40);
        head.click();
        await new Promise((r) => setTimeout(r, 150));
        if (!g.classList.contains('open')) return 'GROUP_NO_EXPAND';
        const body = g.querySelector('.tg-body');
        if (getComputedStyle(body).display === 'none') return 'GROUP_BODY_HIDDEN';
        const n = g.querySelectorAll('.tool-line').length;
        if (n < 2) return 'GROUP_TOO_FEW ' + n;
        // 组内每行各占一行；任一行可单独展开
        const one = g.querySelector('.tool-line');
        one.querySelector('.tl-line').click();
        await new Promise((r) => setTimeout(r, 120));
        if (!one.classList.contains('expanded')) return 'GROUP_ITEM_NO_EXPAND';
        if (one.querySelector('.tl-body').classList.contains('hidden')) return 'GROUP_ITEM_BODY_HIDDEN';
        one.querySelector('.tl-line').click();
        // 再点组头收起
        head.click();
        await new Promise((r) => setTimeout(r, 120));
        if (g.classList.contains('open')) return 'GROUP_NO_COLLAPSE';
        return 'GROUP_OK';
      }
      // 退化为单行检查（只有单个工具调用的场景）
      let el = document.querySelector('.tool-line');
      if (!el) return 'NO_TOOLS';
      if (el.querySelector('.tool-card')) return 'CARD_REGRESSION';
      if (el.classList.contains('expanded')) return 'TOOL_NOT_COLLAPSED';
      el.querySelector('.tl-line').click();
      await new Promise((r) => setTimeout(r, 150));
      if (!el.classList.contains('expanded')) return 'TOOL_NO_EXPAND';
      if (el.querySelector('.tl-body').classList.contains('hidden')) return 'BODY_STILL_HIDDEN';
      el.querySelector('.tl-line').click();
      await new Promise((r) => setTimeout(r, 100));
      if (el.classList.contains('expanded')) return 'TOOL_NO_COLLAPSE';
      return 'TOOLS_OK';
    })()`,
    returnByValue: true,
    awaitPromise: true,
  });
  const groupResult = group.result && group.result.result && group.result.result.value;

  // 13) 工具「修改前后」diff：启用工作区（auto 免审批）→ mock 写文件 →
  //     折叠行预览 +N −M，展开体有 unified diff 行号着色
  let diffResult = 'SKIP';
  if (BACKEND_PORT) {
    // 每次唯一的工作区目录：同名文件已存在且内容相同 → before==after → 无 diff（避免误判）
    const wsDir = path.join(os.tmpdir(), `planner_e2e_ws_${process.pid}_${Date.now()}`);
    fs.mkdirSync(wsDir, { recursive: true });
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { permission_mode: 'auto' } });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsDir });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '改文件' });
    } catch (e) {
      diffResult = 'SETUP_FAIL ' + e.message;
    }
    if (diffResult === 'SKIP') {
      const dd = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          let line = null;
          for (let i = 0; i < 60 && !line; i += 1) {
            line = document.querySelector('.tool-line[data-tool="write_file"]');
            if (!line) await new Promise((r) => setTimeout(r, 250));
          }
          if (!line) return 'NO_WRITE_TOOL_LINE';
          let prev = '';
          for (let i = 0; i < 40; i += 1) {
            const el = line.querySelector('.tl-preview');
            prev = el ? (el.textContent || '') : '';
            if (prev.startsWith('+') && prev.includes('−')) break;
            await new Promise((r) => setTimeout(r, 250));
          }
          if (!(prev.startsWith('+') && prev.includes('−'))) {
            return 'PREVIEW_BAD ' + prev.slice(0, 30);
          }
          line.querySelector('.tl-line').click();
          await new Promise((r) => setTimeout(r, 250));
          const diffs = line.querySelectorAll('.tool-diff .diff-line');
          if (!diffs.length) return 'NO_DIFF_LINES';
          const text = (line.querySelector('.tool-diff').textContent || '');
          if (!text.includes('+第一行内容')) return 'DIFF_TEXT_BAD ' + text.slice(0, 40);
          return 'DIFF_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      diffResult = dd.result && dd.result.result && dd.result.result.value;
    }
    // 清理本轮工作区目录（文件是 mock 模拟写的，没有别处引用）
    try { fs.rmSync(wsDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 14) 图片展示（show_image）：工作区放一张真 png → mock「画图」→ 图片气泡加载成功；
  //     再重载页面 → 历史重建后图片仍在（持久化）
  let imgResult = 'SKIP';
  if (BACKEND_PORT) {
    const wsDir2 = path.join(os.tmpdir(), `planner_e2e_img_${process.pid}_${Date.now()}`);
    fs.mkdirSync(wsDir2, { recursive: true });
    fs.writeFileSync(path.join(wsDir2, 'plot.png'), PNG_1PX);
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { permission_mode: 'auto' } });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsDir2 });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '画图' });
    } catch (e) {
      imgResult = 'SETUP_FAIL ' + e.message;
    }
    const waitImage = `(async () => {
      let img = null;
      for (let i = 0; i < 60; i += 1) {
        const imgs = [...document.querySelectorAll('.msg.img-msg img')];
        img = imgs.find((el) => el.naturalWidth > 0);
        if (img) break;
        await new Promise((r) => setTimeout(r, 250));
      }
      if (!img) {
        const any = document.querySelector('.msg.img-msg');
        return any ? 'IMG_NOT_LOADED ' + (any.textContent || '').slice(0, 30) : 'NO_IMG_MSG';
      }
      return 'IMG_OK';
    })()`;
    if (imgResult === 'SKIP') {
      const im = await p.send('Runtime.evaluate',
        { expression: waitImage, returnByValue: true, awaitPromise: true });
      imgResult = im.result && im.result.result && im.result.result.value;
    }
    if (imgResult === 'IMG_OK') {
      // 重载（≈重开面板）→ 历史重建后图片还在
      await p.send('Page.reload', { ignoreCache: false });
      const im2 = await p.send('Runtime.evaluate',
        { expression: waitImage, returnByValue: true, awaitPromise: true });
      const r2 = im2.result && im2.result.result && im2.result.result.value;
      if (r2 === 'IMG_OK') imgResult = 'IMG_OK+HIST';
      else imgResult = 'HIST_' + r2;
    }
    try { fs.rmSync(wsDir2, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 15) 看图（view_image）：本地图读入模型上下文——工具行完成；**不出现图片气泡**
  //     （与 show_image「给用户看」区分；mock 无视觉 → OCR 回退文案）
  let viewResult = 'SKIP';
  if (BACKEND_PORT) {
    const wsDir3 = path.join(os.tmpdir(), `planner_e2e_view_${process.pid}_${Date.now()}`);
    fs.mkdirSync(wsDir3, { recursive: true });
    fs.writeFileSync(path.join(wsDir3, 'plot.png'), PNG_1PX);
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsDir3 });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '看图' });
    } catch (e) {
      viewResult = 'SETUP_FAIL ' + e.message;
    }
    if (viewResult === 'SKIP') {
      const vw = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const before = document.querySelectorAll('.msg.img-msg').length;
          let line = null;
          for (let i = 0; i < 60 && !line; i += 1) {
            line = document.querySelector('.tool-line[data-tool="view_image"]');
            if (!line) await new Promise((r) => setTimeout(r, 250));
          }
          if (!line) return 'NO_VIEW_LINE';
          for (let i = 0; i < 40 && !line.classList.contains('done'); i += 1) {
            await new Promise((r) => setTimeout(r, 250));
          }
          if (!line.classList.contains('done')) return 'VIEW_NOT_DONE';
          const prev = (line.querySelector('.tl-preview') || {}).textContent || '';
          if (!prev.includes('图片') && !prev.includes('OCR')) {
            return 'VIEW_PREVIEW_BAD ' + prev.slice(0, 30);
          }
          const after = document.querySelectorAll('.msg.img-msg').length;
          if (after !== before) return 'SPURIOUS_BUBBLE ' + before + '->' + after;
          return 'VIEW_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      viewResult = vw.result && vw.result.result && vw.result.result.value;
    }
    try { fs.rmSync(wsDir3, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 16) HTML 预览卡：mock 回复带 ```html（按钮+计数器脚本）→ 沙箱 iframe；
  //     安全断言（allow-scripts 且绝无 allow-same-origin）+ 在 iframe 上下文里点按钮验证可交互
  let htmlResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '做个网页' });
    } catch (e) {
      htmlResult = 'CHAT_FAILED ' + e.message;
    }
    if (htmlResult === 'SKIP') {
      let has = false;
      for (let i = 0; i < 60 && !has; i += 1) {
        const r = await p.send('Runtime.evaluate', {
          expression: "document.querySelectorAll('.html-preview iframe').length",
          returnByValue: true,
        });
        has = (r.result && r.result.result && r.result.result.value) >= 1;
        if (!has) await sleep(300);
      }
      if (!has) {
        htmlResult = 'NO_PREVIEW';
      } else {
        // src 是异步（POST /preview 往返后才设置）→ 轮询等它落定再断言
        let meta = {};
        for (let i = 0; i < 30; i += 1) {
          const info = await p.send('Runtime.evaluate', {
            expression: `(() => {
              const f = document.querySelector('.html-preview iframe');
              return JSON.stringify({ src: f.getAttribute('src') || '',
                                      sandbox: f.getAttribute('sandbox') || '' });
            })()`,
            returnByValue: true,
          });
          meta = JSON.parse((info.result && info.result.result
            && info.result.result.value) || '{}');
          if (meta.src) break;
          await sleep(300);
        }
        if (meta.sandbox.includes('allow-same-origin')) htmlResult = 'UNSAFE_SAME_ORIGIN';
        else if (!meta.sandbox.includes('allow-scripts')) htmlResult = 'NO_SCRIPTS ' + meta.sandbox;
        else if (!/^http:\/\/127\.0\.0\.1:\d+\/preview\//.test(meta.src || '')) {
          htmlResult = 'SRC_BAD ' + String(meta.src).slice(0, 60);
        }
      }
    }
    if (htmlResult === 'SKIP') {
      // 预览是 http 文档（OOPIF 独立 target）→ attach 到它，验证 CSP meta + 点按钮
      await p.send('Target.setDiscoverTargets', { discover: true });
      let sid = null;
      for (let i = 0; i < 30 && !sid; i += 1) {
        const tg = await p.send('Target.getTargets');
        const t = (tg.result.targetInfos || [])
          .find((x) => x.type === 'iframe' && x.url.includes('/preview/'));
        if (t) {
          const at = await p.send('Target.attachToTarget',
            { targetId: t.targetId, flatten: true });
          sid = at.result && at.result.sessionId;
        }
        if (!sid) await sleep(300);
      }
      if (!sid) {
        htmlResult = 'NO_IFRAME_TARGET';
      } else {
        const res = await p.send('Runtime.evaluate', {
          expression: `(() => {
            const csp = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
            if (!csp) return 'NO_CSP';
            const btn = document.getElementById('btn');
            if (!btn) return 'NO_BTN';
            btn.click();
            return document.getElementById('out').textContent;
          })()`,
          returnByValue: true,
        }, sid);
        const val = res.result && res.result.result && res.result.result.value;
        htmlResult = (val === '1') ? 'HTML_OK' : 'CLICK_BAD ' + JSON.stringify(val);
      }
    }
  }

  // 17) 工作目录面板：点目录项面板不消失（回归）+ 按钮动态状态 + 「打开文件夹」按钮存在
  let wsPanelResult = 'SKIP';
  if (BACKEND_PORT) {
    const wsBase = path.join(os.tmpdir(), `planner_e2e_wsp_${process.pid}_${Date.now()}`);
    fs.mkdirSync(path.join(wsBase, 'sub'), { recursive: true });
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsBase });
    } catch (e) {
      wsPanelResult = 'SETUP_FAIL ' + e.message;
    }
    if (wsPanelResult === 'SKIP') {
      const wantWs = path.basename(wsBase);   // 等 chip 标题反映新工作区（避免用旧路径去取目录列表）
      const res = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          const api = async (p, opts) =>
            JSON.parse((await window.planner.apiFetch(p, opts)).text);
          const chip = document.getElementById('cb-dir');
          for (let i = 0; i < 40; i += 1) {
            if (!(chip.textContent || '').includes('未启用')
                && (chip.title || '').includes(${JSON.stringify(wantWs)})) break;
            await sleep(250);
          }
          const panel = document.getElementById('ws-panel');
          const openPanel = async () => {
            // 面板关闭可能是异步的（切目录/回根后 await refreshWs → closePopovers）：
            // 先等它真正收起再点开，否则会在"关闭还没落地"时跳过 click，
            // 650ms 后观察到已关闭 → 假失败 REOPEN1_FAIL（历史 flake）。
            for (let i = 0; i < 15 && !panel.classList.contains('hidden'); i += 1) {
              await sleep(200);
            }
            if (!panel.classList.contains('hidden')) return false;   // 3s 还没收起 = 真 bug
            chip.click();
            await sleep(650);
            return !panel.classList.contains('hidden');
          };
          if (!(await openPanel())) return 'PANEL_NOT_OPEN';
          if (!document.getElementById('ws-open')) return 'NO_OPEN_BTN';
          // 点进 sub 子目录：面板必须保持打开（历史 bug：点一下就整个消失）
          const items = [...document.querySelectorAll('#ws-dirs .ws-item')];
          const sub = items.find((el) => (el.textContent || '').includes('sub'));
          if (!sub) return 'NO_SUB_ITEM';
          sub.click();
          await sleep(800);
          if (panel.classList.contains('hidden')) return 'PANEL_CLOSED_AFTER_NAV';
          const crumbs = document.getElementById('ws-crumbs').textContent || '';
          if (!crumbs.includes('sub')) return 'CRUMBS_BAD ' + crumbs.slice(0, 40);
          if (document.getElementById('ws-use').disabled) return 'USE_DISABLED_AFTER_NAV';
          // 面板开着时不得盖住输入区（面板悬在整个输入框上方；曾盖住输入框导致
          // "点输入框没反应/打不了字"——第一下只关面板且焦点落到 BODY）
          const inpOpen = document.getElementById('input');
          const irOpen = inpOpen.getBoundingClientRect();
          const atOpen = document.elementFromPoint(irOpen.left + irOpen.width / 2,
                                                   irOpen.top + irOpen.height / 2);
          if (atOpen !== inpOpen) {
            return 'INPUT_COVERED_BY_PANEL ' + (atOpen && (atOpen.id || atOpen.className || atOpen.tagName));
          }
          // 设为当前目录（cwd=root/sub，面板关闭）→ 后端确认 + 回根按钮应可用
          document.getElementById('ws-use').click();
          let cwd = '';
          for (let i = 0; i < 20; i += 1) {
            await sleep(250);
            const w = await api('/workspace');
            cwd = w.cwd || '';
            if (cwd.toLowerCase().endsWith('sub')) break;
          }
          if (!cwd.toLowerCase().endsWith('sub')) return 'CWD_NOT_SUB ' + cwd;
          // 回归：切换 cwd 后输入框仍可聚焦、未被遮挡
          const inp = document.getElementById('input');
          inp.focus();
          if (document.activeElement !== inp) return 'INPUT_NOT_FOCUSABLE';
          const ir = inp.getBoundingClientRect();
          const at = document.elementFromPoint(ir.left + ir.width / 2, ir.top + ir.height / 2);
          if (at !== inp) {
            const ui = document.getElementById('panel-ui');
            const stage = document.getElementById('stage') || ui.parentElement;
            return 'INPUT_COVERED ' + (at && (at.id || at.className || at.tagName))
              + ' uiPE=' + ui.style.pointerEvents + ' uiOp=' + ui.style.opacity
              + ' stageCls=' + stage.className
              + ' vp=' + window.innerWidth + 'x' + window.innerHeight;
          }
          if (!(await openPanel())) return 'REOPEN1_FAIL';
          if (document.getElementById('ws-root').disabled) return 'ROOT_DISABLED_AT_SUB';
          // 回到根（cwd 回工作区根，面板关闭）
          document.getElementById('ws-root').click();
          for (let i = 0; i < 20; i += 1) {
            await sleep(250);
            const w = await api('/workspace');
            if ((w.cwd || '') === (w.workspace || '')) break;
          }
          const w2 = await api('/workspace');
          if ((w2.cwd || '') !== (w2.workspace || '')) return 'CWD_NOT_ROOT ' + w2.cwd;
          // 重开：浏览位置=根 →「设为当前目录」应禁用；关掉面板
          if (!(await openPanel())) return 'REOPEN2_FAIL';
          if (!document.getElementById('ws-use').disabled) return 'USE_NOT_DISABLED_AT_ROOT';
          document.getElementById('ws-close').click();
          return 'WSPANEL_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      wsPanelResult = res.result && res.result.result && res.result.result.value;
    }
    try { fs.rmSync(wsBase, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 18) 切换工作区（最近目录、工作区外）确认框：必须是**应用内弹窗**（原生 confirm
  //     在 Windows 关闭后键盘焦点可能不还给页面 → "鼠标能点、打不了字"）；
  //     确认并切换后，输入框**第一次点击**即可聚焦并打字
  let switchResult = 'SKIP';
  if (BACKEND_PORT) {
    const wsA = path.join(os.tmpdir(), `planner_e2e_wsa_${process.pid}_${Date.now()}`);
    const wsB = path.join(os.tmpdir(), `planner_e2e_wsb_${process.pid}_${Date.now()}`);
    fs.mkdirSync(wsA, { recursive: true });
    fs.mkdirSync(wsB, { recursive: true });
    const nameA = path.basename(wsA);
    const nameB = path.basename(wsB);
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsA });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsB });  // 进 recent
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsA });  // 当前 = A
    } catch (e) {
      switchResult = 'SETUP_FAIL ' + e.message;
    }
    if (switchResult === 'SKIP') {
      const raceEval = async (expr, ms = 15000) => {
        const res = await Promise.race([
          p.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }),
          sleep(ms).then(() => ({ __timeout: true })),
        ]);
        if (res && res.__timeout) return '__TIMEOUT__';
        if (res.result && res.result.exceptionDetails) {
          return 'EXC ' + JSON.stringify(res.result.exceptionDetails.text);
        }
        return res.result && res.result.result ? res.result.result.value : undefined;
      };
      const r18 = await raceEval(`(async () => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        const api = async (p, o) => JSON.parse((await window.planner.apiFetch(p, o)).text);
        const chip = document.getElementById('cb-dir');
        for (let i = 0; i < 40; i += 1) {
          if ((chip.title || '').includes(${JSON.stringify(nameA)})) break;
          await sleep(250);
        }
        chip.click();
        await sleep(700);
        const item = [...document.querySelectorAll('#ws-recent .ws-item')]
          .find((el) => (el.textContent || '').includes(${JSON.stringify(nameB)}));
        if (!item) return 'NO_RECENT_ITEM';
        item.click();
        await sleep(500);
        const modal = document.getElementById('ui-confirm');
        if (!modal || modal.classList.contains('hidden')) return 'NO_INAPP_CONFIRM';
        if (!modal.textContent.includes(${JSON.stringify(nameB)})) return 'CONFIRM_TEXT_BAD';
        document.getElementById('ui-confirm-ok').click();
        for (let i = 0; i < 24; i += 1) {
          await sleep(250);
          const w = await api('/workspace');
          if ((w.workspace || '').includes(${JSON.stringify(nameB)})) return 'SWITCHED';
        }
        const w = await api('/workspace');
        return 'SWITCH_FAILED ' + (w.workspace || '');
      })()`);
      if (r18 === '__TIMEOUT__') {
        switchResult = 'NATIVE_CONFIRM_HANG';   // 原生 confirm 回归（阻塞页面）
      } else if (r18 !== 'SWITCHED') {
        switchResult = String(r18);
      } else {
        // 真·受信输入：点输入框第一下就要能聚焦 + 打字
        const ir = JSON.parse(await (async () => {
          const res = await p.send('Runtime.evaluate', {
            expression: `(() => { const el = document.getElementById('input');
              const b = el.getBoundingClientRect();
              return JSON.stringify({ x: Math.round(b.left + b.width / 2),
                                      y: Math.round(b.top + b.height / 2) }); })()`,
            returnByValue: true,
          });
          return res.result.result.value;
        })());
        await p.send('Input.dispatchMouseEvent',
          { type: 'mousePressed', x: ir.x, y: ir.y, button: 'left', clickCount: 1 });
        await p.send('Input.dispatchMouseEvent',
          { type: 'mouseReleased', x: ir.x, y: ir.y, button: 'left', clickCount: 1 });
        await sleep(250);
        const active = await raceEval(
          "document.activeElement && document.activeElement.id", 5000);
        await p.send('Input.dispatchKeyEvent',
          { type: 'char', key: 'k', code: 'KeyK', text: 'k' });
        await sleep(200);
        const val = await raceEval("document.getElementById('input').value", 5000);
        await raceEval("document.getElementById('input').value = ''; 'ok'", 5000);
        if (active !== 'input' || !String(val).includes('k')) {
          switchResult = 'INPUT_DEAD active=' + active + ' val=' + JSON.stringify(val);
        } else {
          switchResult = 'SWITCH_OK';
        }
      }
    }
    try { fs.rmSync(wsA, { recursive: true, force: true }); } catch { /* 忽略 */ }
    try { fs.rmSync(wsB, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 19) 滚动不打断阅读：用户翻到上面后，思考/正文/工具追加**不得把滚动条拽回底部**；
  //     "回到最新"按钮出现，点击后回到底部
  let scrollResult = 'SKIP';
  if (BACKEND_PORT) {
    const r19a = await p.send('Runtime.evaluate', {
      expression: `(async () => {
        const box = document.getElementById('messages');
        if (box.scrollHeight <= box.clientHeight + 120) return 'NOT_SCROLLABLE';
        box.scrollTop = 0;
        await new Promise((r) => setTimeout(r, 250));
        return String(box.scrollTop) + '|' + String(box.scrollHeight);
      })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    const before = String((r19a.result && r19a.result.result && r19a.result.result.value) || '');
    if (before === 'NOT_SCROLLABLE') {
      scrollResult = 'NOT_SCROLLABLE';
    } else {
      const [top0, h0] = before.split('|').map(Number);
      try {
        await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '多想一下' });
      } catch (e) {
        scrollResult = 'CHAT_FAILED ' + e.message;
      }
      if (scrollResult === 'SKIP') {
        await sleep(2600);   // 等这一轮生成（思考流+正文+工具行）全部追加完
        const r19b = await p.send('Runtime.evaluate', {
          expression: `(() => {
            const box = document.getElementById('messages');
            const jump = document.getElementById('jump-bottom');
            return JSON.stringify({ top: box.scrollTop, h: box.scrollHeight,
                                    jumpVisible: !jump.classList.contains('hidden') });
          })()`,
          returnByValue: true,
        });
        const after = JSON.parse((r19b.result && r19b.result.result && r19b.result.result.value) || '{}');
        if (!(after.h > h0)) {
          scrollResult = 'NO_APPEND h0=' + h0 + ' h=' + after.h;
        } else if (!(after.top < 60)) {
          scrollResult = 'YANKED top0=' + top0 + ' top=' + after.top;
        } else if (after.jumpVisible !== true) {
          scrollResult = 'NO_JUMP_BTN';
        } else {
          // 点「回到最新」→ 应回到底部
          await p.send('Runtime.evaluate', {
            expression: "document.getElementById('jump-bottom').click(); 'ok'",
            returnByValue: true,
          });
          let atBottom = false;
          for (let i = 0; i < 20 && !atBottom; i += 1) {
            await sleep(250);
            const r = await p.send('Runtime.evaluate', {
              expression: `(() => { const b = document.getElementById('messages');
                return b.scrollHeight - b.scrollTop - b.clientHeight < 30; })()`,
              returnByValue: true,
            });
            atBottom = r.result && r.result.result && r.result.result.value === true;
          }
          scrollResult = atBottom ? 'SCROLL_OK' : 'JUMP_FAILED';
        }
      }
    }
  }

  // 20) 多文件补丁（apply_patch）：mock「打补丁」建两个文件 → 工具行展开的多文件 diff
  //     里两个文件名都在；后端变更库各有留底
  let patchResult = 'SKIP';
  if (BACKEND_PORT) {
    const wsBase4 = path.join(os.tmpdir(), `planner_e2e_patch_${process.pid}_${Date.now()}`);
    fs.mkdirSync(wsBase4, { recursive: true });
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { permission_mode: 'auto' } });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsBase4 });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '打补丁' });
    } catch (e) {
      patchResult = 'SETUP_FAIL ' + e.message;
    }
    if (patchResult === 'SKIP') {
      const pr = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          let line = null;
          for (let i = 0; i < 60 && !line; i += 1) {
            line = document.querySelector('.tool-line[data-tool="apply_patch"]');
            if (!line) await new Promise((r) => setTimeout(r, 250));
          }
          if (!line) return 'NO_PATCH_LINE';
          for (let i = 0; i < 40 && !line.classList.contains('done'); i += 1) {
            await new Promise((r) => setTimeout(r, 250));
          }
          if (!line.classList.contains('done')) return 'PATCH_NOT_DONE';
          line.querySelector('.tl-line').click();
          await new Promise((r) => setTimeout(r, 250));
          const txt = (line.querySelector('.tool-diff') || {}).textContent || '';
          if (!txt.includes('e2e_patch_a.txt')) return 'DIFF_A_MISSING';
          if (!txt.includes('e2e_patch_b.txt')) return 'DIFF_B_MISSING';
          const prev = (line.querySelector('.tl-preview') || {}).textContent || '';
          if (!prev.startsWith('+')) return 'PREVIEW_BAD ' + prev.slice(0, 30);
          return 'PATCH_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      patchResult = pr.result && pr.result.result && pr.result.result.value;
    }
    try { fs.rmSync(wsBase4, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 21) 生成中收/展面板：隐藏期间"在途"的思考流 + 提问卡必须留在对话区
  //     回归：展开完成时 loadHistory(true) 会重建对话框，而在途的思考/工具调用/
  //     流式正文还没写进 buffer（/history 拿不到）——曾经被整个抹掉（用户实测：
  //     "生成中缩小又放大，用了哪些工具、中间的 thinking 都没了"）。
  //     现在隐藏期间事件也实时进面板 DOM，且生成中不重建。
  let hideGenResult = 'SKIP';
  if (BACKEND_PORT) {
    // 等上一轮生成（第 20 步补丁）真正结束，避免新消息进队列搞乱事件时序
    for (let i = 0; i < 40; i += 1) {
      try {
        const st = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/state`);
        if (!(st.state && st.state.thinking)) break;
      } catch { /* 忽略 */ }
      await sleep(250);
    }
    const hg = await p.send('Runtime.evaluate', {
      expression: `(async () => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        // 1) 收成悬浮球，等状态真正落到 hidden
        window.planner.hidePanel();
        for (let i = 0; i < 40; i += 1) {
          await sleep(150);
          if ((await window.planner.getPanelState()) === 'hidden') return 'HIDDEN';
        }
        return 'HIDE_FAIL';
      })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    hideGenResult = hg.result && hg.result.result && hg.result.result.value;
    if (hideGenResult === 'HIDDEN') {
      // 2) 隐藏期间发起生成（mock「出题」= ask_user 专属词，带「多想」附带思考流；
      //    提问卡在 ask_user 等回答 → 思考+提问卡全程在途）
      try {
        await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '出题，多想一下' });
      } catch (e) {
        hideGenResult = 'CHAT_FAILED ' + e.message;
      }
    }
    if (hideGenResult === 'HIDDEN') {
      const rg = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          // 3) 提问请求会自动展开面板；等到 shown 后再断言——旧代码正是在展开重建时抹掉内容
          for (let i = 0; i < 60; i += 1) {
            await sleep(150);
            if ((await window.planner.getPanelState()) === 'shown') break;
          }
          if ((await window.planner.getPanelState()) !== 'shown') return 'RESHOW_FAIL';
          const card = document.querySelector('.question-inline:not(.resolved)');
          if (!card) return 'CARD_LOST';
          const think = [...document.querySelectorAll('.thinking-block')]
            .map((el) => el.textContent || '').join(' ');
          if (!think.includes('mock')) return 'THINKING_LOST';
          const intro = [...document.querySelectorAll('#messages .msg')]
            .map((el) => el.textContent || '').join(' ');
          if (!intro.includes('先考你一道')) return 'INTRO_LOST';
          // 4) 回答提问 → 生成收尾（工具结果 + 收尾回复都要到）
          card.querySelector('.question-options button').click();
          for (let i = 0; i < 40; i += 1) {
            await sleep(250);
            const all = [...document.querySelectorAll('#messages .msg, #messages .tool-line')]
              .map((el) => el.textContent || '').join(' ');
            if (all.includes('用户选择了') && all.includes('好，收到')) return 'HIDEGEN_OK';
          }
          return 'FINISH_MISSING';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      hideGenResult = rg.result && rg.result.result && rg.result.result.value;
    }
  }

  // 22) 喇叭朗读按钮三态：点击 → 合成中 → 播放中（.playing）→ 再点一下 = 停止播报
  //     （用户反馈：播放时再点喇叭要能取消）。播放依赖本机音频设备：走到播放前
  //     失败（无设备/合成慢）时软跳过 SKIP_PLAY，不判红——只对"能播起来却停不掉"判红。
  let speakStopResult = 'SKIP';
  if (BACKEND_PORT) {
    const ss = await p.send('Runtime.evaluate', {
      expression: `(async () => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        const btn = [...document.querySelectorAll('#messages .speak-btn')].pop();
        if (!btn) return 'NO_BTN';
        btn.click();
        let playing = false;
        for (let i = 0; i < 80; i += 1) {          // ≤20s（首次合成可能要几秒）
          await sleep(250);
          if (btn.classList.contains('playing')) { playing = true; break; }
          if (!btn.classList.contains('loading') && i > 4) return 'SKIP_PLAY';   // 播放失败已复位
        }
        if (!playing) return 'SKIP_PLAY';
        if (btn.title !== '停止播报') return 'TITLE_BAD ' + btn.title;
        const audio = document.getElementById('tts-audio');
        if (audio.paused || !audio.src) return 'AUDIO_NOT_PLAYING';
        btn.click();                                // 再点一下 → 停止播报
        await sleep(600);
        if (btn.classList.contains('playing') || btn.classList.contains('loading')) return 'STILL_ACTIVE';
        if (btn.title !== '朗读这条消息') return 'RESET_TITLE_BAD ' + btn.title;
        if (!audio.paused && audio.src) return 'AUDIO_STILL_PLAYING';
        return 'SPEAKSTOP_OK';
      })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    speakStopResult = ss.result && ss.result.result && ss.result.result.value;
  }

  // 23) 语音播报开关「运行中实时生效」：在**设置页**取消勾选并保存 →
  //     settingsSaved 广播 → 面板 voiceMode.ttsEnabled() 立即变 false。
  //     回归：设置页保存曾漏调 window.planner.settingsSaved()，运行中关掉播报后
  //     回复继续被流式朗读，直到重启（attach 时拉 /settings）才停。
  let ttsSyncResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      // 基线：播报打开（e2e 隔离数据默认即 true；显式保证 + 广播一次）
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { tts_enabled: true } });
      await p.send('Runtime.evaluate', { expression: 'window.planner.settingsSaved()' });
      let on = false;
      for (let i = 0; i < 25; i += 1) {
        await sleep(200);
        const r = await p.send('Runtime.evaluate', {
          expression: 'window.voiceMode.ttsEnabled()', returnByValue: true });
        if (r.result && r.result.result && r.result.result.value === true) { on = true; break; }
      }
      if (!on) ttsSyncResult = 'BASELINE_FAIL';
    } catch (e) {
      ttsSyncResult = 'SETUP_FAIL ' + e.message;
    }
    if (ttsSyncResult === 'SKIP') {
      // 打开设置页 → 取消勾选「语音播报」→ 保存
      await p.send('Runtime.evaluate', { expression: 'window.planner.openSettings()' });
      const stPage = await waitPage('settings.html', 15000);
      const s = await connect(stPage.webSocketDebuggerUrl);
      await s.send('Runtime.enable');
      const sv = await s.send('Runtime.evaluate', {
        expression: `(async () => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          // 等设置页就绪（脚本执行 + 设置加载完成）。页面刚出现就操作会点空
          // （脚本未跑完/字段未加载），曾导致 SAVE_TIMEOUT 假失败。
          for (let i = 0; i < 75; i += 1) {
            if (document.body && document.body.dataset.settingsLoaded === '1') break;
            await sleep(200);
          }
          if (!document.body || document.body.dataset.settingsLoaded !== '1') return 'PAGE_NOT_READY';
          const cb = () => document.getElementById('tts_enabled');
          if (!cb() || !cb().checked) return 'CHECKBOX_NOT_ON';
          cb().checked = false;
          document.getElementById('workspace_root').value = '';   // 避免不存在目录导致报错
          document.getElementById('save').click();
          for (let i = 0; i < 40; i += 1) {
            await sleep(250);
            const msg = document.getElementById('msg').textContent || '';
            if (msg.includes('已保存')) return 'SAVED';
          }
          return 'SAVE_TIMEOUT ' + (document.getElementById('msg').textContent || '');
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      const svRes = sv.result && sv.result.result && sv.result.result.value;
      s.close();
      if (svRes !== 'SAVED') {
        ttsSyncResult = 'SETTINGS_UI_FAIL ' + svRes;
      } else {
        let off = false;
        for (let i = 0; i < 25; i += 1) {
          await sleep(200);
          const r = await p.send('Runtime.evaluate', {
            expression: 'window.voiceMode.ttsEnabled()', returnByValue: true });
          if (r.result && r.result.result && r.result.result.value === false) { off = true; break; }
        }
        ttsSyncResult = off ? 'TTSSYNC_OK' : 'NOT_SYNCED';
      }
    }
  }

  console.log('  面板异常数:', errs.length, '| 日历 tab 点击:', clickResult,
              '| 目录徽标:', badgeResult, '| 尺寸/拖拽:', sizeResult,
              '| 工具行:', groupResult, '| 状态条:', chipsResult,
              '| 变更 tab:', changesResult, '| 思考流:', thinkResult,
              '| ask_user:', askResult, '| 用量统计:', statsResult,
              '| 工具diff:', diffResult, '| 图片:', imgResult,
              '| 看图:', viewResult, '| HTML预览:', htmlResult,
              '| 工作目录面板:', wsPanelResult, '| 切换工作区:', switchResult,
              '| 滚动:', scrollResult, '| 多文件补丁:', patchResult,
              '| 隐藏期生成:', hideGenResult, '| 朗读停止:', speakStopResult,
              '| tts同步:', ttsSyncResult);
  p.close();

  if (errs.length > 0) { console.error('面板加载有 JS 异常'); process.exit(1); }
  if (clickResult !== 'CLICK_OK') { console.error('面板交互未生效（事件未绑定）'); process.exit(1); }
  if (badgeResult !== 'BADGE_OK') { console.error('标题栏目录徽标缺失'); process.exit(1); }
  if (sizeResult !== 'SIZE_OK') { console.error('面板尺寸/拖拽把手检查失败: ' + sizeResult); process.exit(1); }
  if (groupResult !== 'TOOLS_OK' && groupResult !== 'NO_TOOLS'
      && groupResult !== 'GROUP_OK') {
    console.error('工具组/工具行检查失败: ' + groupResult);
    process.exit(1);
  }
  if (chipsResult !== 'CHIPS_OK') {
    console.error('输入框状态条检查失败: ' + chipsResult);
    process.exit(1);
  }
  if (changesResult !== 'CHANGES_OK') {
    console.error('变更 tab 检查失败: ' + changesResult);
    process.exit(1);
  }
  if (askResult !== 'SKIP' && askResult !== 'ASK_OK') {
    console.error('ask_user 弹窗链路检查失败: ' + askResult);
    process.exit(1);
  }
  if (thinkResult !== 'SKIP' && thinkResult !== 'THINK_OK'
      && thinkResult !== 'THINK_OK+HIST') {
    console.error('思考流（thinking_stream）检查失败: ' + thinkResult);
    process.exit(1);
  }
  if (statsResult !== 'SKIP' && statsResult !== 'STATS_OK') {
    console.error('用量统计（缓存命中/TPS）检查失败: ' + statsResult);
    process.exit(1);
  }
  if (diffResult !== 'SKIP' && diffResult !== 'DIFF_OK') {
    console.error('工具修改前后 diff 检查失败: ' + diffResult);
    process.exit(1);
  }
  if (imgResult !== 'SKIP' && imgResult !== 'IMG_OK+HIST') {
    console.error('图片展示（show_image）检查失败: ' + imgResult);
    process.exit(1);
  }
  if (viewResult !== 'SKIP' && viewResult !== 'VIEW_OK') {
    console.error('看图（view_image）检查失败: ' + viewResult);
    process.exit(1);
  }
  if (htmlResult !== 'SKIP' && htmlResult !== 'HTML_OK') {
    console.error('HTML 预览卡（沙箱可交互）检查失败: ' + htmlResult);
    process.exit(1);
  }
  if (wsPanelResult !== 'SKIP' && wsPanelResult !== 'WSPANEL_OK') {
    console.error('工作目录面板检查失败: ' + wsPanelResult);
    process.exit(1);
  }
  if (switchResult !== 'SKIP' && switchResult !== 'SWITCH_OK') {
    console.error('切换工作区（应用内确认 + 输入框可用）检查失败: ' + switchResult);
    process.exit(1);
  }
  if (scrollResult !== 'SKIP' && scrollResult !== 'SCROLL_OK') {
    console.error('滚动不打断阅读检查失败: ' + scrollResult);
    process.exit(1);
  }
  if (patchResult !== 'SKIP' && patchResult !== 'PATCH_OK') {
    console.error('多文件补丁（apply_patch）检查失败: ' + patchResult);
    process.exit(1);
  }
  if (hideGenResult !== 'SKIP' && hideGenResult !== 'HIDEGEN_OK') {
    console.error('生成中收/展面板（在途思考/提问卡不得被重建抹掉）检查失败: ' + hideGenResult);
    process.exit(1);
  }
  if (speakStopResult !== 'SKIP' && speakStopResult !== 'SKIP_PLAY'
      && speakStopResult !== 'SPEAKSTOP_OK') {
    console.error('朗读按钮停止（播放中再点=取消）检查失败: ' + speakStopResult);
    process.exit(1);
  }
  if (ttsSyncResult !== 'SKIP' && ttsSyncResult !== 'TTSSYNC_OK') {
    console.error('语音播报开关运行中实时同步检查失败: ' + ttsSyncResult);
    process.exit(1);
  }
  process.exit(0);
})().catch((e) => { console.error('FATAL:', e.message); process.exit(1); });
