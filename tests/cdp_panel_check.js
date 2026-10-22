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

  // 2.5) Agent 切换：首屏就要在标题栏里看到「主助手 / White」，不能等 /agents 或单独占一行
  const initialAgents = await p.send('Runtime.evaluate', {
    expression: `(() => {
      const bar = document.getElementById('agent-bar');
      if (!bar) return 'NO_BAR';
      if (bar.classList.contains('hidden')) return 'BAR_HIDDEN';
      if (!bar.closest('#titlebar')) return 'NOT_IN_TITLEBAR';
      const tabs = [...bar.querySelectorAll('.agent-tab')];
      const labels = tabs.map((t) => t.textContent.trim());
      if (tabs.length < 2) return 'TABS ' + labels.join(',');
      if (!labels.some((x) => x.includes('主助手'))
          || !labels.some((x) => x.includes('White'))) {
        return 'LABELS ' + labels.join(',');
      }
      return 'INITIAL_AGENTS_OK';
    })()`,
    returnByValue: true,
  });
  const initialAgentResult = initialAgents.result && initialAgents.result.result
    && initialAgents.result.result.value;

  // 2.55) 输入框随内容增高（不是固定一行；长内容到动态上限后再内部滚动）
  const inputGrow = await p.send('Runtime.evaluate', {
    expression: `(() => {
      const i = document.getElementById('input');
      const bar = document.getElementById('input-bar');
      const h0 = i.getBoundingClientRect().height;
      const bar0 = bar.getBoundingClientRect().height;
      const set = (text) => {
        i.value = text;
        i.dispatchEvent(new Event('input', { bubbles: true }));
        return i.getBoundingClientRect().height;
      };
      const h1 = set('a');
      const h5 = set(Array(5).fill('a').join('\\n'));
      const h10 = set(Array(10).fill('a').join('\\n'));
      i.value = '';
      i.dispatchEvent(new Event('input', { bubbles: true }));
      const hClear = i.getBoundingClientRect().height;
      if (h0 > 42) return 'INITIAL_TALL ' + h0;
      if (bar0 > 150) return 'BAR_TALL ' + bar0;
      if (!(h5 > h1 + 8)) return 'NO_GROW_5 ' + h1 + '/' + h5;
      if (!(h10 > h5 + 8)) return 'NO_GROW_10 ' + h5 + '/' + h10;
      if (hClear > 42) return 'CLEAR_TALL ' + hClear;
      return 'INPUT_GROW_OK ' + Math.round(h0) + '/' + Math.round(h5)
        + '/' + Math.round(h10) + '/' + Math.round(hClear);
    })()`,
    returnByValue: true,
  });
  const inputGrowResult = inputGrow.result && inputGrow.result.result
    && inputGrow.result.result.value;

  // 2.6) 用户消息附件：图片缩略图 + 文件 chip 在气泡上方，点图片可放大
  let attachmentsResult = 'SKIP';
  if (BACKEND_PORT) {
    const attachDir = fs.mkdtempSync(path.join(os.tmpdir(), 'planner-attach-'));
    const txtPath = path.join(attachDir, 'note.txt');
    const imgPath = path.join(attachDir, 'pic.png');
    try {
      fs.writeFileSync(txtPath, 'attachment test', 'utf8');
      fs.writeFileSync(imgPath, PNG_1PX);
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, {
        message: '附件展示测试',
        files: [
          { name: 'note.txt', path: txtPath, kind: 'text', content: 'attachment test' },
          { name: 'pic.png', path: imgPath, kind: 'image' },
        ],
      });
      const ac = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          let msg = null;
          for (let i = 0; i < 60; i += 1) {
            const list = [...document.querySelectorAll('.msg.user')];
            msg = list.reverse().find((el) => el.textContent.includes('附件展示测试'));
            if (msg) break;
            await sleep(100);
          }
          if (!msg) return 'NO_MSG';
          let att = null;
          for (let i = 0; i < 40; i += 1) {
            att = msg.querySelector('.user-attachments');
            if (att) break;
            await sleep(100);
          }
          if (!att) return 'NO_ATTACH';
          const body = msg.querySelector('.msg-body');
          if (!body) return 'NO_BODY';
          if (att.nextElementSibling !== body) return 'ORDER_BAD';
          if (!att.querySelector('.user-file-chip')) return 'NO_FILE_CHIP';
          const thumb = att.querySelector('.user-image-thumb');
          if (!thumb) return 'NO_THUMB';
          thumb.click();
          await sleep(350);
          const lb = document.querySelector('.img-lightbox');
          if (!lb) return 'NO_LIGHTBOX';
          const close = lb.querySelector('.lb-close');
          if (!close) return 'NO_LB_CLOSE';
          close.click();
          await sleep(80);
          if (document.querySelector('.img-lightbox')) return 'LB_NOT_CLOSED';
          return 'ATTACH_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      attachmentsResult = (ac.result && ac.result.result && ac.result.result.value) || 'NO_RESULT';
      // 等附件消息触发的这轮生成结束，避免影响后面的步骤
      for (let i = 0; i < 60; i += 1) {
        const st = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/state`);
        if (!(st.state && st.state.thinking)) break;
        await sleep(100);
      }
    } catch (e) {
      attachmentsResult = 'SETUP_FAIL ' + e.message;
    } finally {
      try { fs.rmSync(attachDir, { recursive: true, force: true }); } catch { /* 忽略 */ }
    }
  }

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
      // 收起按钮要铺满标题栏高度（回归：上下 padding 曾把可点区压成一条小横条）
      const bar = document.getElementById('titlebar').getBoundingClientRect();
      const fold = document.getElementById('btn-fold').getBoundingClientRect();
      if (!(fold.height >= bar.height - 4)) return 'FOLD_H_BAD ' + fold.height + '/' + bar.height;
      if (!(fold.width >= 40)) return 'FOLD_W_BAD ' + fold.width;
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
          // 3) 新行为（2026-10-19）：审批/提问请求不再自动展开面板——悬浮球旁
          //    冒提醒浮层 + 黄灯。这里先等提问卡出现在（隐藏的）对话区，
          //    再手动展开（等同用户点球/点浮层「展开面板」）。
          let hasCard = false;
          for (let i = 0; i < 60; i += 1) {
            await sleep(150);
            if (document.querySelector('.question-inline:not(.resolved)')) { hasCard = true; break; }
          }
          if (!hasCard) return 'CARD_LOST';
          window.planner.togglePanel();
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

  // 24) 截屏（悬浮球右键菜单触发）：覆盖层选区 → ✓ 挂载为待发送文件（不直接发送）；
  //     ✗ 取消不留痕。无显示环境拿不到屏幕时软跳过（SKIP_CAPTURE）。
  let shotResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      const getMounted = async () => {
        const m = await p.send('Runtime.evaluate', {
          expression: 'window.planner.getMounted()', returnByValue: true, awaitPromise: true });
        return (m.result && m.result.result && m.result.result.value) || [];
      };
      const n0 = (await getMounted()).length;
      await p.send('Runtime.evaluate', { expression: 'window.planner.startScreenshot()' });
      const shotPage = await waitPage('screenshot.html', 8000);
      if (!shotPage) {
        shotResult = 'SKIP_CAPTURE';   // 拿不到屏幕画面（无显示/权限）→ 软跳过
      } else {
        const s = await connect(shotPage.webSocketDebuggerUrl);
        await s.send('Runtime.enable');
        await sleep(600);
        const rr = await s.send('Runtime.evaluate', {
          expression: `(async () => {
            const fire = (t, x, y) => window.dispatchEvent(
              new MouseEvent(t, { clientX: x, clientY: y, button: 0, bubbles: true }));
            fire('mousedown', 120, 120);
            fire('mousemove', 320, 240);
            fire('mouseup', 320, 240);
            await new Promise((r) => setTimeout(r, 250));
            if (document.getElementById('tools').classList.contains('hidden')) return 'NO_TOOLS';
            document.getElementById('ok').click();
            return 'OK';
          })()`,
          returnByValue: true, awaitPromise: true,
        });
        s.close();
        const clicked = rr.result && rr.result.result && rr.result.result.value;
        if (clicked !== 'OK') {
          shotResult = 'OVERLAY_BAD ' + clicked;
        } else {
          let list = [];
          for (let i = 0; i < 20; i += 1) {
            await sleep(250);
            list = await getMounted();
            if (list.length === n0 + 1) break;
          }
          if (list.length !== n0 + 1) {
            shotResult = 'MOUNT_FAIL ' + list.length;
          } else {
            const item = list[list.length - 1];
            if (item.kind !== 'image' || !fs.existsSync(item.path)) {
              shotResult = 'FILE_BAD ' + (item.path || '');
            } else {
              // 取消路径：再截一次 → ✗ → 挂载数不变
              await p.send('Runtime.evaluate', { expression: 'window.planner.startScreenshot()' });
              const shotPage2 = await waitPage('screenshot.html', 8000);
              if (!shotPage2) {
                shotResult = 'SKIP_CAPTURE';
              } else {
                const s2 = await connect(shotPage2.webSocketDebuggerUrl);
                await s2.send('Runtime.enable');
                await sleep(400);
                await s2.send('Runtime.evaluate', {
                  expression: "document.getElementById('cancel').click()" });
                s2.close();
                await sleep(700);
                const n2 = (await getMounted()).length;
                shotResult = (n2 === n0 + 1) ? 'SHOT_OK' : ('CANCEL_LEAK ' + n2);
              }
            }
          }
        }
      }
      // 清掉挂载，避免影响复跑
      await p.send('Runtime.evaluate', { expression: 'window.planner.clearMounted()' });
    } catch (e) {
      shotResult = 'SETUP_FAIL ' + e.message;
    }
  }

  // 25) 输入框粘贴（Ctrl+V）：剪贴板位图（无磁盘路径）→ 主进程落盘 userData/pasted/
  //     → 挂载条出现。纯文本粘贴不拦截（原生行为）。
  let pasteResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      const getMounted2 = async () => {
        const m = await p.send('Runtime.evaluate', {
          expression: 'window.planner.getMounted()', returnByValue: true, awaitPromise: true });
        return (m.result && m.result.result && m.result.result.value) || [];
      };
      const n0 = (await getMounted2()).length;
      const pr = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const input = document.getElementById('input');
          const b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
          const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
          const file = new File([bytes], 'paste-test.png', { type: 'image/png' });
          const dt = new DataTransfer();
          dt.items.add(file);
          input.dispatchEvent(new ClipboardEvent('paste',
            { clipboardData: dt, bubbles: true, cancelable: true }));
          return 'DISPATCHED';
        })()`,
        returnByValue: true, awaitPromise: true,
      });
      const dispatched = pr.result && pr.result.result && pr.result.result.value;
      if (dispatched !== 'DISPATCHED') {
        pasteResult = 'DISPATCH_FAIL ' + dispatched;
      } else {
        let list = [];
        for (let i = 0; i < 20; i += 1) {
          await sleep(250);
          list = await getMounted2();
          if (list.length === n0 + 1) break;
        }
        if (list.length !== n0 + 1) {
          pasteResult = 'PASTE_MOUNT_FAIL ' + list.length;
        } else {
          const item = list[list.length - 1];
          if (item.kind !== 'image' || !fs.existsSync(item.path)
              || !String(item.path).includes('pasted')) {
            pasteResult = 'PASTE_FILE_BAD ' + (item.path || '');
          } else {
            pasteResult = 'PASTE_OK';
          }
        }
      }
      await p.send('Runtime.evaluate', { expression: 'window.planner.clearMounted()' });
    } catch (e) {
      pasteResult = 'SETUP_FAIL ' + e.message;
    }
  }

  // 26) 改动文件快捷块：mock「打补丁」改两个文件 → 回复下方出现快捷块
  //     （文件名 + 「打开」「目录」按钮）；/history 重建同样带 changed_files 条目
  let cfResult = 'SKIP';
  if (BACKEND_PORT) {
    const wsBase5 = path.join(os.tmpdir(), `planner_e2e_cf_${process.pid}_${Date.now()}`);
    fs.mkdirSync(wsBase5, { recursive: true });
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { permission_mode: 'auto' } });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/workspace`, { path: wsBase5 });
      // 等上一轮生成真正结束再发（防御：上一轮可能还在等审批/提问超时）
      for (let i = 0; i < 300; i += 1) {
        try {
          const st = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/state`);
          if (!(st.state && st.state.thinking)) break;
        } catch { /* 忽略 */ }
        await sleep(250);
      }
      const n0r = await p.send('Runtime.evaluate', {
        expression: "document.querySelectorAll('.changed-files').length",
        returnByValue: true,
      });
      const n0 = Number((n0r.result && n0r.result.result && n0r.result.result.value) || 0);
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '补丁块' });
      const cf = await p.send('Runtime.evaluate', {
        expression: `(async () => {
          const n0 = ${n0};
          let blocks = document.querySelectorAll('.changed-files');
          for (let i = 0; i < 80 && blocks.length <= n0; i += 1) {
            await new Promise((r) => setTimeout(r, 250));
            blocks = document.querySelectorAll('.changed-files');
          }
          if (blocks.length <= n0) return 'NO_BLOCK';
          const block = blocks[blocks.length - 1];
          const line = block.querySelector('.cf-line');
          if (!line) return 'NO_LINE';
          const text = line.textContent || '';
          if (!text.includes('e2e_patch_a.txt') || !text.includes('e2e_patch_b.txt')) {
            return 'NAMES_MISSING ' + text.slice(0, 60);
          }
          // 默认收起：点一行才展开出按钮
          const body = block.querySelector('.cf-body');
          if (!body || getComputedStyle(body).display !== 'none') return 'NOT_COLLAPSED';
          line.click();
          await new Promise((r) => setTimeout(r, 150));
          if (getComputedStyle(body).display === 'none') return 'EXPAND_FAIL';
          const btns = [...block.querySelectorAll('.cf-btn')]
            .filter((b) => b.offsetParent !== null);
          if (btns.length < 4) return 'BTNS_MISSING ' + btns.length;
          // 按钮 IPC 链路可用（用不存在的路径验证返回形状：不真开窗口、也不弹
          // Windows 报错框——open-path 处理器先查存在性）
          const bad1 = await window.planner.showInFolder('C:/definitely-nonexistent-xyz/nope.txt');
          if (!bad1 || bad1.ok !== false) return 'IPC_SHOW_BAD ' + JSON.stringify(bad1);
          const bad2 = await window.planner.openInExplorer('C:/definitely-nonexistent-xyz/nope.txt');
          if (!bad2 || bad2.ok !== false) return 'IPC_OPEN_BAD ' + JSON.stringify(bad2);
          return 'BLOCK_OK';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      cfResult = cf.result && cf.result.result && cf.result.result.value;
      if (cfResult === 'BLOCK_OK') {
        // /history 重建路径：changed_files 条目在（面板重开时能重建快捷块）
        try {
          const h = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/history`);
          const has = (h.messages || []).some((m) => m.role === 'changed_files'
            && (m.files || []).some((f) => String(f.rel || '').includes('e2e_patch_a.txt')));
          cfResult = has ? 'CF_OK' : 'HISTORY_MISSING';
        } catch (e) {
          cfResult = 'HISTORY_FAIL ' + e.message;
        }
      }
    } catch (e) {
      cfResult = 'SETUP_FAIL ' + e.message;
    }
    try { fs.rmSync(wsBase5, { recursive: true, force: true }); } catch { /* 忽略 */ }
  }

  // 27) 快速输入浮层（悬浮球模式全局快捷键）：窗口启动即创建（隐藏）——
  //     验证「待发送文件显示 → 输入 → Enter 发送 → 挂载清空 → /history 落库」
  //     与 UI 设置项（快捷键/气泡时长）可读。热键本身由 OS 注册，CDP 无法模拟。
  let quickResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      const quickFile = path.join(os.tmpdir(), `planner_e2e_quick_${process.pid}.txt`);
      fs.writeFileSync(quickFile, 'hello');
      console.log('  [27] quick input: page found');
      const qp = await waitPage('quick-input.html', 15000);
      const q = await connect(qp.webSocketDebuggerUrl);
      console.log('  [27] quick input: connected');
      await q.send('Runtime.enable');
      const qr = await q.send('Runtime.evaluate', {
        expression: `(async () => {
          window.planner.clearMounted();
          await new Promise((r) => setTimeout(r, 150));
          window.planner.mountFiles([{ name: 'quick_e2e.txt', path: ${JSON.stringify(quickFile)}, kind: 'text', content: 'hello' }]);
          for (let i = 0; i < 30; i += 1) {
            await new Promise((r) => setTimeout(r, 100));
            if (document.querySelectorAll('.qf-chip').length === 1) break;
          }
          if (document.querySelectorAll('.qf-chip').length !== 1) return 'FILES_MISSING';
          const el = document.getElementById('quick-input');
          el.value = '快速输入测试';
          el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
          for (let i = 0; i < 40; i += 1) {
            await new Promise((r) => setTimeout(r, 100));
            const m = await window.planner.getMounted();
            if (!m.length) break;
          }
          const ui = await window.planner.getUiSettings();
          if (!(ui.quick_input_hotkey && ui.toast_seconds > 0)) return 'UI_SETTINGS_MISSING';
          return 'QUICK_SENT';
        })()`,
        returnByValue: true,
        awaitPromise: true,
      });
      quickResult = (qr.result && qr.result.result && qr.result.result.value) || 'NO_RESULT';
      if (quickResult === 'QUICK_SENT') {
        let found = false;
        for (let i = 0; i < 40; i += 1) {
          try {
            const h = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/history`);
            found = (h.messages || []).some((m) => m.role === 'user'
              && String(m.content || '').includes('快速输入测试'));
          } catch { /* 忽略 */ }
          if (found) break;
          await sleep(150);
        }
        // 面板应立即出现这条用户消息（user_message 事件；旧代码要收起/展开才看到）
        let shown = false;
        for (let i = 0; i < 40; i += 1) {
          const r2 = await p.send('Runtime.evaluate', {
            expression: "Array.from(document.querySelectorAll('.msg.user .bubble'))"
              + ".some((b) => (b.textContent || '').includes('快速输入测试'))",
            returnByValue: true,
          });
          if (r2.result && r2.result.result && r2.result.result.value) { shown = true; break; }
          await sleep(150);
        }
        quickResult = !found ? 'HISTORY_MISSING'
          : (shown ? 'QUICK_OK' : 'PANEL_NOT_RENDERED');
      }
      q.close();
      try { fs.rmSync(quickFile, { force: true }); } catch { /* 忽略 */ }
    } catch (e) {
      quickResult = 'SETUP_FAIL ' + e.message;
    }
  }

  // 28) 悬浮球待处理提醒：收起面板 → mock 触发 ask_user → 球黄灯呼吸 +
  //     球旁提醒卡片；直接在卡片里点选项回答（不展开面板）→ 黄灯恢复
  let pendingResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      const bp = await waitPage('bubble.html', 10000);
      const bb = await connect(bp.webSocketDebuggerUrl);
      await bb.send('Runtime.enable');
      // 先等上一轮生成收尾（步骤 27 的消息可能还在回）
      for (let i = 0; i < 100; i += 1) {
        try {
          const st = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/state`);
          if (!(st.state && st.state.thinking)) break;
        } catch { /* 忽略 */ }
        await sleep(150);
      }
      console.log('  [28] collapsing panel...');
      // 收起面板 → 悬浮球形态（togglePanel 在 shown 时是"置前"，收起要用 hidePanel）
      await bb.send('Runtime.evaluate', {
        expression: 'window.planner.hidePanel()',
        returnByValue: true,
      });
      let hidden = false;
      for (let i = 0; i < 40; i += 1) {
        await sleep(150);
        const st = await bb.send('Runtime.evaluate', {
          expression: 'window.planner.getPanelState()', returnByValue: true, awaitPromise: true,
        });
        if ((st.result && st.result.result && st.result.result.value) === 'hidden') { hidden = true; break; }
      }
      if (!hidden) {
        pendingResult = 'PANEL_NOT_HIDDEN';
      } else {
        console.log('  [28] panel hidden, sending 气泡提问');
        await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '气泡提问' });
        console.log('  [28] chat posted');
        // 球体黄灯（pending 广播）
        let lit = false;
        for (let i = 0; i < 80; i += 1) {
          const r = await bb.send('Runtime.evaluate', {
            expression: "document.getElementById('core').classList.contains('pending')",
            returnByValue: true,
          });
          if (r.result && r.result.result && r.result.result.value) { lit = true; break; }
          await sleep(150);
        }
        if (!lit) {
          pendingResult = 'BALL_NOT_PENDING';
        } else {
          console.log('  [28] ball pending lit');
          const pp = await waitPage('pending.html', 15000);
          const pw = await connect(pp.webSocketDebuggerUrl);
          await pw.send('Runtime.enable');
          let ready = false;
          for (let i = 0; i < 60; i += 1) {
            const r = await pw.send('Runtime.evaluate', {
              expression: "document.querySelectorAll('#pn-options .pn-opt').length",
              returnByValue: true,
            });
            const n = Number((r.result && r.result.result && r.result.result.value) || 0);
            if (n >= 2) { ready = true; break; }
            await sleep(150);
          }
          if (!ready) {
            pendingResult = 'CARD_NOT_READY';
          } else {
            console.log('  [28] card ready, clicking option');
            await pw.send('Runtime.evaluate', {
              expression: "document.querySelector('#pn-options .pn-opt').click()",
              returnByValue: true,
            });
            // 黄灯恢复（question_resolved 经 /dequeue 回来）
            let cleared = false;
            for (let i = 0; i < 100; i += 1) {
              const r = await bb.send('Runtime.evaluate', {
                expression: "document.getElementById('core').classList.contains('pending')",
                returnByValue: true,
              });
              if (!(r.result && r.result.result && r.result.result.value)) { cleared = true; break; }
              await sleep(150);
            }
            const pr = await bb.send('Runtime.evaluate', {
              expression: 'window.planner.getPending()', returnByValue: true, awaitPromise: true,
            });
            const left = Number(((pr.result && pr.result.result && pr.result.result.value) || {}).count || 0);
            pendingResult = (cleared && left === 0) ? 'PENDING_OK' : 'NOT_CLEARED';
          }
          pw.close();
          // 回到面板展开态（后续 bubble 检查脚本自己会切换，这里保持一致）
          await bb.send('Runtime.evaluate', {
            expression: 'window.planner.getPanelState().then((s) => { if (s === "hidden") window.planner.togglePanel(); })',
            returnByValue: true, awaitPromise: true,
          });
        }
      }
      bb.close();
    } catch (e) {
      pendingResult = 'SETUP_FAIL ' + e.message;
    }
  }

  // 29) Markdown 表格 + 链接：渲染成 <table> 与可点击超链接；非法协议拒绝
  let mdResult = 'SKIP';
  try {
    const mr = await p.send('Runtime.evaluate', {
      expression: `(async () => {
        const NL = String.fromCharCode(10);
        const mdText = ['| 公司 | 要 | 代价 |', '|---|---|---|',
          '| 中船院所 | 多数要 | 开源受限 |', '| 雅江 | 不要 | 长期驻藏 |', '',
          '[示例](https://example.com/a) 和 https://example.org/b。'].join(NL);
        const html = window.md.render(mdText);
        const div = document.createElement('div');
        div.innerHTML = html;
        const table = div.querySelector('.md-table');
        if (!table) return 'NO_TABLE';
        if (table.querySelectorAll('th').length !== 3) return 'TH_BAD';
        if (table.querySelectorAll('tbody tr').length !== 2) return 'ROWS_BAD';
        if (!table.textContent.includes('中船院所')) return 'CELL_BAD';
        const links = [...div.querySelectorAll('a.md-link')].map((a) => a.getAttribute('href'));
        if (!links.includes('https://example.com/a')) return 'MD_LINK_BAD ' + links.join(',');
        if (!links.some((h) => h.startsWith('https://example.org/b'))) return 'BARE_LINK_BAD';
        const mdText2 = ['# 标题一', '', '- 甲', '- 乙', '', '1. 丙', '2. 丁', '',
          '> 引用'].join(NL);
        const div2 = document.createElement('div');
        div2.innerHTML = window.md.render(mdText2);
        if (!div2.querySelector('h1')) return 'HEADING_MISSING';
        if (div2.querySelectorAll('ul li').length !== 2) return 'UL_BAD';
        if (div2.querySelectorAll('ol li').length !== 2) return 'OL_BAD';
        if (!div2.querySelector('blockquote')) return 'QUOTE_MISSING';
        if (window.md.render('[x](javascript:alert(1))').includes('<a')) return 'UNSAFE_LINK';
        const bad = await window.planner.openExternal('javascript:alert(1)');
        if (!bad || bad.ok !== false) return 'UNSAFE_OPEN ' + JSON.stringify(bad);
        return 'MD_OK';
      })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    mdResult = (mr.result && mr.result.result && mr.result.result.value) || 'NO_RESULT';
  } catch (e) {
    mdResult = 'SETUP_FAIL ' + e.message;
  }

  // 30) 面板录音跨窗口特效：面板开始录音 → 收起面板后悬浮球也要显示
  //     紫点 + 雷达环（麦克风无法在自动化里真录，直接走 IPC 状态通路）
  let panelVoiceResult = 'SKIP';
  try {
    const bp = await waitPage('bubble.html', 10000);
    const bb = await connect(bp.webSocketDebuggerUrl);
    await bb.send('Runtime.enable');
    const nudgePanel = async (on) => {
      await p.send('Runtime.evaluate', {
        expression: 'window.planner.setPanelRecording(' + (on ? 'true' : 'false') + ')',
        returnByValue: true,
      });
      for (let i = 0; i < 40; i += 1) {
        await sleep(150);
        const r = await bb.send('Runtime.evaluate', {
          expression: "(document.getElementById('core').classList.contains('recording'))"
            + " === " + (on ? 'true' : 'false')
            + " && (document.getElementById('sound-ring').classList.contains('active'))"
            + " === " + (on ? 'true' : 'false'),
          returnByValue: true,
        });
        if (r.result && r.result.result && r.result.result.value) return true;
      }
      return false;
    };
    const onOk = await nudgePanel(true);
    const offOk = onOk ? await nudgePanel(false) : false;
    panelVoiceResult = (!onOk) ? 'ON_FAIL' : (offOk ? 'PANELVOICE_OK' : 'OFF_FAIL');
    bb.close();
  } catch (e) {
    panelVoiceResult = 'SETUP_FAIL ' + e.message;
  }

  // 31) 悬浮窗气泡：Markdown 表格/标题渲染 + 高度到上限后内部滑动（不显示滚动条）
  let toastResult = 'SKIP';
  try {
    // 先收起面板（showToast 只在悬浮球形态工作）
    await p.send('Runtime.evaluate', {
      expression: 'window.planner.hidePanel()', returnByValue: true,
    });
    for (let i = 0; i < 40; i += 1) {
      await sleep(150);
      const st = await p.send('Runtime.evaluate', {
        expression: 'window.planner.getPanelState()', returnByValue: true, awaitPromise: true,
      });
      if ((st.result && st.result.result && st.result.result.value) === 'hidden') break;
    }
    const bp2 = await waitPage('bubble.html', 10000);
    const bb2 = await connect(bp2.webSocketDebuggerUrl);
    await bb2.send('Runtime.enable');
    await bb2.send('Runtime.evaluate', {
      expression: `(() => {
        const NL = String.fromCharCode(10);
        const lines = ['## 长内容标题', '', '| 列A | 列B |', '|---|---|',
          '| 1 | 2 |', '| 3 | 4 |', ''];
        for (let i = 0; i < 30; i++) lines.push('- 第 ' + i + ' 条内容，用来把气泡撑高');
        window.planner.showToast(lines.join(NL), 0, false);
        return 'SENT';
      })()`,
      returnByValue: true,
    });
    const tp = await waitPage('toast.html', 12000);
    const tw = await connect(tp.webSocketDebuggerUrl);
    await tw.send('Runtime.enable');
    let info = null;
    for (let i = 0; i < 40; i += 1) {
      await sleep(200);
      const r = await tw.send('Runtime.evaluate', {
        expression: `(() => {
          const el = document.getElementById('toast-text');
          if (!el) return null;
          const cs = getComputedStyle(el);
          const before = el.scrollTop;
          el.scrollTop = 200;
          return {
            hasHeading: !!el.querySelector('h2'),
            hasTable: !!el.querySelector('.md-table'),
            overflow: cs.overflowY,
            scrollable: el.scrollHeight > el.clientHeight + 20,
            scrolled: el.scrollTop > before,
            sbWidth: cs.scrollbarWidth,
            winH: window.innerHeight,
            textH: el.clientHeight,
          };
        })()`,
        returnByValue: true,
      });
      info = r.result && r.result.result ? r.result.result.value : null;
      if (info && info.hasTable && info.scrollable) break;
    }
    if (!info) {
      toastResult = 'NO_TOAST';
    } else if (!info.hasHeading || !info.hasTable) {
      toastResult = 'MD_MISSING ' + JSON.stringify(info);
    } else if (info.overflow !== 'auto' || !info.scrollable || !info.scrolled) {
      toastResult = 'NOT_SCROLLABLE ' + JSON.stringify(info);
    } else if (info.winH > 500) {
      toastResult = 'TOO_TALL ' + info.winH;
    } else if (info.sbWidth && info.sbWidth !== 'none') {
      toastResult = 'SCROLLBAR_VISIBLE ' + info.sbWidth;
    } else {
      toastResult = 'TOAST_OK';
    }
    tw.close();
    bb2.close();
    // 恢复展开态（morphIn 会清掉气泡）
    await p.send('Runtime.evaluate', {
      expression: 'window.planner.getPanelState().then((s) => { if (s === "hidden") window.planner.togglePanel(); })',
      returnByValue: true, awaitPromise: true,
    });
  } catch (e) {
    toastResult = 'SETUP_FAIL ' + e.message;
  }

  // 32) 设置页服务商回显：保存的是 opencode 就必须显示 opencode
  //     （回归：选择框不回显 → 任何一次保存都悄悄把 provider 改回 deepseek）
  let settingsProviderResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { llm_provider: 'opencode' } });
      // 设置窗口可能在第 23 步就已打开（主进程单例，openSettings 只 focus 不重载）
      // → 直接刷新页面，让它重新从 /settings 回显
      let sp = null;
      try { sp = await waitPage('settings.html', 1200); } catch { /* 没有就开一个 */ }
      if (!sp) {
        await p.send('Runtime.evaluate', {
          expression: 'window.planner.openSettings()', returnByValue: true,
        });
        sp = await waitPage('settings.html', 15000);
      }
      const sw = await connect(sp.webSocketDebuggerUrl);
      await sw.send('Runtime.enable');
      await sw.send('Runtime.evaluate', { expression: 'location.reload()' });
      let loaded = false;
      for (let i = 0; i < 60; i += 1) {
        await sleep(200);
        const r = await sw.send('Runtime.evaluate', {
          expression: "document.body.dataset.settingsLoaded === '1'",
          returnByValue: true,
        });
        if (r.result && r.result.result && r.result.result.value) { loaded = true; break; }
      }
      const vr = await sw.send('Runtime.evaluate', {
        expression: "JSON.stringify({ p: document.getElementById('llm_provider').value })",
        returnByValue: true,
      });
      const vals = JSON.parse((vr.result && vr.result.result && vr.result.result.value) || '{}');
      settingsProviderResult = !loaded ? 'NOT_LOADED'
        : (vals.p === 'opencode' ? 'SETTINGS_PROVIDER_OK'
          : 'PROVIDER_RESET ' + JSON.stringify(vals));
      await sw.send('Runtime.evaluate', { expression: 'window.close()', returnByValue: true });
      sw.close();
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/settings`,
                     { updates: { llm_provider: 'deepseek' } });
    } catch (e) {
      settingsProviderResult = 'SETUP_FAIL ' + e.message;
    }
  }

  // 33) White 子代理：主助手派活 → 后台运行 → Agent 标签页 + 统一消息视图 + 结果卡
  let whiteResult = 'SKIP';
  if (BACKEND_PORT) {
    try {
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/chat`, { message: '白色任务' });
      let alive = false;
      for (let i = 0; i < 60; i += 1) {
        const a = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/agents`);
        if ((a.agents || []).some((x) => x.id === 'white')) { alive = true; break; }
        await sleep(200);
      }
      if (!alive) {
        whiteResult = 'NO_WHITE';
      } else {
        const wr = await p.send('Runtime.evaluate', {
          expression: `(async () => {
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
            let tab = null;
            for (let i = 0; i < 60; i += 1) {
              tab = document.querySelector('#agent-bar .agent-tab.white');
              if (tab) break;
              await sleep(200);
            }
            if (!tab) return 'NO_TAB';
            tab.click();
            for (let i = 0; i < 60; i += 1) {
              if (!document.getElementById('white-view').classList.contains('hidden')) break;
              await sleep(100);
            }
            if (document.getElementById('white-view').classList.contains('hidden')) return 'VIEW_HIDDEN';
            let card = null;
            for (let i = 0; i < 80; i += 1) {
              card = document.querySelector('#white-messages .white-result');
              if (card) break;
              await sleep(200);
            }
            if (!card) return 'NO_RESULT_CARD';
            const text = card.textContent || '';
            if (!text.includes('White 测试任务完成')) return 'RESULT_TEXT ' + text.slice(0, 80);
            let state = '';
            for (let i = 0; i < 60; i += 1) {
              state = (document.getElementById('white-state').textContent || '').trim();
              if (state === '空闲') break;
              await sleep(100);
            }
            if (state !== '空闲') return 'STATE_BAD ' + state;
            // White 滚动策略：用户滚上去后，切走再切回不能重置；回到底部按钮可用
            const wm = document.getElementById('white-messages');
            const jump = document.getElementById('white-jump');
            if (!jump) return 'NO_JUMP_BTN';
            for (let i = 0; i < 30; i += 1) {
              const filler = document.createElement('div');
              filler.className = 'msg log';
              filler.textContent = 'white scroll filler ' + i;
              wm.appendChild(filler);
            }
            wm.scrollTop = 0;
            wm.dispatchEvent(new Event('scroll'));
            await sleep(100);
            if (jump.classList.contains('hidden')) return 'JUMP_HIDDEN';
            const mainTab = document.querySelector('#agent-bar .agent-tab:not(.white)');
            const whiteTab = document.querySelector('#agent-bar .agent-tab.white');
            if (mainTab) mainTab.click();
            await sleep(120);
            if (whiteTab) whiteTab.click();
            await sleep(300);
            if (wm.scrollTop > 8) return 'SCROLL_RESET';
            if (jump.classList.contains('hidden')) return 'JUMP_HIDDEN_AFTER_SWITCH';
            jump.click();
            await sleep(700);
            if (wm.scrollHeight - wm.scrollTop - wm.clientHeight > 20) return 'JUMP_FAIL';
            // 回到主助手视图（后续 bubble 检查要点输入框/单击球）
            if (mainTab) mainTab.click();
            return 'WHITE_OK';
          })()`,
          returnByValue: true,
          awaitPromise: true,
        });
        whiteResult = (wr.result && wr.result.result && wr.result.result.value) || 'NO_RESULT';
      }
    } catch (e) {
      whiteResult = 'SETUP_FAIL ' + e.message;
    }
  }

  // 34) 目录读授权页：添加父子目录 → 合并显示 → 移除父目录
  let dirPermResult = 'SKIP';
  if (BACKEND_PORT) {
    const permRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'planner-perm-'));
    const permParent = path.join(permRoot, 'projects');
    const permChild = path.join(permParent, 'alpha');
    try {
      fs.mkdirSync(permChild, { recursive: true });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/dir-permissions/add`,
                     { path: permChild });
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/dir-permissions/add`,
                     { path: permParent });
      await p.send('Runtime.evaluate', {
        expression: `document.querySelector('#tabs .tab[data-tab="permissions"]').click()`,
        returnByValue: true,
      });
      await sleep(700);
      const dp = await p.send('Runtime.evaluate', {
        expression: `(() => {
          const rows = [...document.querySelectorAll('#perm-page-list .perm-item')];
          const texts = rows.map((r) => r.textContent || '');
          const row = texts.find((x) => x.includes(${JSON.stringify(permParent)}));
          if (!row) return 'NO_PARENT ' + texts.join(' | ');
          if (!row.includes('已合并 1')) return 'NO_MERGE ' + row;
          if (!document.getElementById('perm-page-note').textContent.includes('当前工作区')) {
            return 'NO_NOTE';
          }
          return 'PERM_PAGE_OK';
        })()`,
        returnByValue: true,
      });
      dirPermResult = (dp.result && dp.result.result && dp.result.result.value) || 'NO_RESULT';
      if (dirPermResult === 'PERM_PAGE_OK') {
        const list = await backendGet(`http://127.0.0.1:${BACKEND_PORT}/dir-permissions`);
        const parentGrant = (list.grants || []).find(
          (g) => (g.path || '').toLowerCase() === permParent.toLowerCase());
        if (!parentGrant) {
          dirPermResult = 'NO_PARENT_ID';
        } else {
          await postJson(`http://127.0.0.1:${BACKEND_PORT}/dir-permissions/remove`,
                         { id: parentGrant.id });
          await p.send('Runtime.evaluate', {
            expression: `document.querySelector('#tabs .tab[data-tab="permissions"]').click()`,
            returnByValue: true,
          });
          await sleep(500);
          const after = await p.send('Runtime.evaluate', {
            expression: `([...document.querySelectorAll('#perm-page-list .perm-item')]
              .map((r) => r.textContent || '').join(' | '))`,
            returnByValue: true,
          });
          const afterText = (after.result && after.result.result
            && after.result.result.value) || '';
          if (!afterText.includes(permChild)) {
            dirPermResult = 'CHILD_LOST ' + afterText;
          }
        }
      }
      await postJson(`http://127.0.0.1:${BACKEND_PORT}/dir-permissions/clear`, {});
      await p.send('Runtime.evaluate', {
        expression: `document.querySelector('#tabs .tab[data-tab="chat"]').click()`,
        returnByValue: true,
      });
    } catch (e) {
      dirPermResult = 'SETUP_FAIL ' + e.message;
    } finally {
      try { fs.rmSync(permRoot, { recursive: true, force: true }); } catch { /* 忽略 */ }
    }
  }

  console.log('  面板异常数:', errs.length, '| 日历 tab 点击:', clickResult,
              '| 首屏 Agent 标签:', initialAgentResult,
              '| 输入框增高:', inputGrowResult,
              '| 用户附件展示:', attachmentsResult,
              '| 目录徽标:', badgeResult, '| 尺寸/拖拽:', sizeResult,
              '| 工具行:', groupResult, '| 状态条:', chipsResult,
              '| 变更 tab:', changesResult, '| 思考流:', thinkResult,
              '| ask_user:', askResult, '| 用量统计:', statsResult,
              '| 工具diff:', diffResult, '| 图片:', imgResult,
              '| 看图:', viewResult, '| HTML预览:', htmlResult,
              '| 工作目录面板:', wsPanelResult, '| 切换工作区:', switchResult,
              '| 滚动:', scrollResult, '| 多文件补丁:', patchResult,
              '| 隐藏期生成:', hideGenResult, '| 朗读停止:', speakStopResult,
              '| tts同步:', ttsSyncResult, '| 截屏:', shotResult,
              '| 输入框粘贴:', pasteResult, '| 改动文件:', cfResult,
              '| 快速输入:', quickResult, '| 待处理提醒:', pendingResult,
              '| Markdown:', mdResult, '| 面板录音特效:', panelVoiceResult,
              '| 气泡长内容:', toastResult, '| 设置页服务商回显:', settingsProviderResult,
              '| White 子代理:', whiteResult,
              '| 目录读授权页:', dirPermResult);
  p.close();

  if (errs.length > 0) { console.error('面板加载有 JS 异常'); process.exit(1); }
  if (initialAgentResult !== 'INITIAL_AGENTS_OK') {
    console.error('首屏 Agent 标签（标题栏内主助手/White）检查失败: ' + initialAgentResult);
    process.exit(1);
  }
  if (!String(inputGrowResult).startsWith('INPUT_GROW_OK')) {
    console.error('输入框随内容增高检查失败: ' + inputGrowResult);
    process.exit(1);
  }
  if (attachmentsResult !== 'SKIP' && attachmentsResult !== 'ATTACH_OK') {
    console.error('用户消息附件展示（缩略图/文件 chip/放大）检查失败: ' + attachmentsResult);
    process.exit(1);
  }
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
  if (shotResult !== 'SKIP' && shotResult !== 'SKIP_CAPTURE' && shotResult !== 'SHOT_OK') {
    console.error('截屏（选区→挂载 / 取消不留痕）检查失败: ' + shotResult);
    process.exit(1);
  }
  if (pasteResult !== 'SKIP' && pasteResult !== 'PASTE_OK') {
    console.error('输入框粘贴（Ctrl+V → 落盘 → 挂载）检查失败: ' + pasteResult);
    process.exit(1);
  }
  if (cfResult !== 'SKIP' && cfResult !== 'CF_OK') {
    console.error('改动文件快捷块（回复下方 + /history 重建）检查失败: ' + cfResult);
    process.exit(1);
  }
  if (quickResult !== 'SKIP' && quickResult !== 'QUICK_OK') {
    console.error('快速输入浮层（挂载+Enter 发送+/history）检查失败: ' + quickResult);
    process.exit(1);
  }
  if (pendingResult !== 'SKIP' && pendingResult !== 'PENDING_OK') {
    console.error('悬浮球待处理提醒（黄灯+气泡卡就地回答）检查失败: ' + pendingResult);
    process.exit(1);
  }
  if (mdResult !== 'SKIP' && mdResult !== 'MD_OK') {
    console.error('Markdown 表格/链接渲染检查失败: ' + mdResult);
    process.exit(1);
  }
  if (panelVoiceResult !== 'SKIP' && panelVoiceResult !== 'PANELVOICE_OK') {
    console.error('面板录音跨窗口特效检查失败: ' + panelVoiceResult);
    process.exit(1);
  }
  if (toastResult !== 'SKIP' && toastResult !== 'TOAST_OK') {
    console.error('悬浮窗气泡长内容（表格/标题/滑动）检查失败: ' + toastResult);
    process.exit(1);
  }
  if (settingsProviderResult !== 'SKIP' && settingsProviderResult !== 'SETTINGS_PROVIDER_OK') {
    console.error('设置页服务商回显检查失败: ' + settingsProviderResult);
    process.exit(1);
  }
  if (whiteResult !== 'SKIP' && whiteResult !== 'WHITE_OK') {
    console.error('White 子代理（派活/标签页/结果卡）检查失败: ' + whiteResult);
    process.exit(1);
  }
  if (dirPermResult !== 'SKIP' && dirPermResult !== 'PERM_PAGE_OK') {
    console.error('目录读授权页检查失败: ' + dirPermResult);
    process.exit(1);
  }
  process.exit(0);
})().catch((e) => { console.error('FATAL:', e.message); process.exit(1); });
