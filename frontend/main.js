// main.js —— Electron 悬浮球主进程
// 悬浮球（常驻置顶，可拖拽） + 对话面板（点击球"变形"展开，失焦"变形"收回）

const { app, BrowserWindow, Tray, Menu, nativeImage, screen, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const BACKEND_URL = process.env.PLANNER_URL || 'http://127.0.0.1:18771';
const PROJECT_ROOT = path.join(app.getAppPath(), '..');       // frontend/ → planner/
const PYTHON = process.env.PLANNER_PYTHON || 'D:\\Miniconda3\\python.exe';
const TRAY_ICON_FILE = path.join(__dirname, 'assets', 'tray.png');

// 独立实例：release 版设 PLANNER_USER_DATA，避免与开发版共用 userData
// （单实例锁、悬浮球位置等全部随之隔离，两个版本可同时运行）
if (process.env.PLANNER_USER_DATA) {
  app.setPath('userData', process.env.PLANNER_USER_DATA);
}

const BUBBLE_SIZE = 56;      // 悬浮球尺寸
const PANEL_W = 350;         // 面板宽
const PANEL_H = 520;         // 面板高

let bubbleWin = null;
let panelWin = null;
let tray = null;
let trayImage = null;   // 保持 nativeImage 引用，防止被 GC 导致 Tray 底层对象销毁
let backendProc = null;
let quitting = false;

// ── /dequeue 唯一消费者（主进程）──────────────────────────
// 之前 bubble（面板隐藏时）和面板窗口各自轮询 /dequeue，展开瞬间两个
// 消费者重叠 → 事件被抢走（tool_result 被 bubble 吞掉 → 工具卡片永久转圈）。
// 现在由主进程独占轮询，按 panelState 分发：hidden → 气泡；展开 → 推给面板；
// pendingEvents 缓存竞态窗口的事件，面板展开完成后补发。
let dequeueTimer = null;
let pendingEvents = [];

// 面板状态机：hidden → morphing_in → shown → morphing_out → hidden
// 动画帧由 renderer 的 rAF 驱动（60fps），主进程只执行 setBounds
let panelState = 'hidden';
let panelLoaded = false;   // 面板页面是否加载完成（listener 就绪）
let pendingMorph = null;   // 页面加载完成前暂存的变形请求 {kind, from, to, showFirst}
let ignoreBlurUntil = 0;   // morph-in 完成后短暂忽略 blur（focus 延迟失败的兜底）
let panelDragged = false;  // 面板是否被拖离过展开位置（决定缩小是否跟随面板）

const BUBBLE_STATE_FILE = () => path.join(app.getPath('userData'), 'bubble-pos.json');

// ── 全局异常兜底：不弹错误框打扰用户，能恢复就恢复 ────────────
process.on('uncaughtException', (err) => {
  console.error('[planner] uncaughtException:', err);
  try {
    if (tray && tray.isDestroyed()) {
      tray = null;
      createTray();
    }
  } catch { /* 忽略 */ }
});

function loadBubbleState() {
  try {
    const s = JSON.parse(fs.readFileSync(BUBBLE_STATE_FILE(), 'utf-8'));
    return { x: s.x, y: s.y };
  } catch {
    return {};
  }
}

function saveBubbleState(bounds) {
  try {
    fs.writeFileSync(BUBBLE_STATE_FILE(), JSON.stringify({ x: bounds.x, y: bounds.y }));
  } catch { /* 忽略 */ }
}

async function backendAlive() {
  try {
    const res = await fetch(BACKEND_URL + '/init', { signal: AbortSignal.timeout(2500) });
    return res.ok;
  } catch {
    return false;
  }
}

async function ensureBackend() {
  if (await backendAlive()) return;
  try {
    backendProc = spawn(PYTHON, ['-m', 'planner'], {
      cwd: PROJECT_ROOT, detached: true, stdio: 'ignore', windowsHide: true,
    });
    backendProc.unref();
    console.log('[planner] 已拉起后端进程');
  } catch (e) {
    console.error('[planner] 拉起后端失败:', e);
  }
}

function trayIcon() {
  if (trayImage) return trayImage;
  try {
    if (fs.existsSync(TRAY_ICON_FILE)) {
      trayImage = nativeImage.createFromPath(TRAY_ICON_FILE);
      if (!trayImage.isEmpty()) return trayImage;
    }
  } catch { /* 回退到 dataURL */ }
  trayImage = nativeImage.createFromDataURL(
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAIElEQVR42mP8z8Dwn4ECwESJ5lEDRg0YNWDUgFEDhs4AAQYAAf8CGvs3oCsAAAAASUVORK5CYII='
  );
  return trayImage;
}

// ── 悬浮球窗口 ──────────────────────────────────────────────
function createBubble() {
  if (bubbleWin && !bubbleWin.isDestroyed()) return;
  const pos = loadBubbleState();
  bubbleWin = new BrowserWindow({
    width: BUBBLE_SIZE,
    height: BUBBLE_SIZE,
    x: pos.x,
    y: pos.y,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  bubbleWin.loadFile(path.join(__dirname, 'renderer', 'bubble.html'));
  bubbleWin.once('ready-to-show', () => bubbleWin.show());
  bubbleWin.on('moved', () => {
    if (bubbleWin && !bubbleWin.isDestroyed()) saveBubbleState(bubbleWin.getBounds());
  });
  // 悬浮球不允许最小化（最小化后无处可寻）
  bubbleWin.on('minimize', () => {
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.restore();
  });
  bubbleWin.on('closed', () => { bubbleWin = null; });
}

// ── 变形面板窗口（球 → 矩形窗口的形态由 renderer 动画驱动）────
function createPanel() {
  if (panelWin && !panelWin.isDestroyed()) return;
  panelWin = new BrowserWindow({
    width: BUBBLE_SIZE,
    height: BUBBLE_SIZE,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  panelWin.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  // 页面加载完成（listener 就绪）后，补发暂存的变形请求
  panelWin.webContents.once('did-finish-load', () => {
    panelLoaded = true;
    if (pendingMorph) {
      const m = pendingMorph;
      pendingMorph = null;
      if (m.showFirst) panelWin.show();
      panelWin.webContents.send(m.kind === 'in' ? 'morph-in' : 'morph-out', { from: m.from, to: m.to });
    }
  });
  // 收起面板只通过界面上的「—」按钮（hide-panel IPC）→ 变形收回。
  // 不监听 blur/点击外部（用户明确：点击其他位置不缩小）。
  panelWin.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      morphOut(); // 收起 = 变形回悬浮球
    }
  });
  // 最小化会暂停 renderer 的 rAF → 变形动画卡死（实测：托盘再也打不开）。
  // 直接隐藏并重置状态（不 restore+hide 组合——残留状态会导致下次 show 无效；
  // 不 destroy——实测 destroy 会连带导致整个应用退出）。
  panelWin.on('minimize', () => {
    clearMorphTimeout();
    panelState = 'hidden';
    if (panelWin && !panelWin.isDestroyed()) panelWin.hide();
    showBubble();
  });
  // 窗口被意外销毁（系统关闭/崩溃路径）→ 完整重置状态，绝不让状态残留卡死
  panelWin.on('closed', () => {
    panelWin = null;
    resetPanelState();
    showBubble();
  });
  panelWin.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  // 渲染进程崩溃：重置状态并重建窗口，避免"再也打不开"
  panelWin.webContents.on('render-process-gone', () => {
    console.error('[planner] 面板渲染进程崩溃，重置状态');
    if (panelWin) panelWin.destroy();
    panelWin = null;
    resetPanelState();
    showBubble();
  });
}

// 面板窗口不复存在/状态残留时：完整重置状态机（防"托盘无反应"卡死）
function resetPanelState() {
  clearMorphTimeout();
  pendingMorph = null;
  panelLoaded = false;
  panelState = 'hidden';
  ignoreBlurUntil = 0;
}

// ── 面板目标位置：贴悬浮球旁边，夹在屏幕工作区内 ──────────────
function panelTargetPos() {
  const b = bubbleWin.getBounds();
  const disp = screen.getDisplayNearestPoint({ x: b.x, y: b.y }).workArea;
  let x = b.x + b.width + 6;
  let y = b.y - Math.round(PANEL_H / 2) + Math.round(BUBBLE_SIZE / 2);
  if (x + PANEL_W > disp.x + disp.width) x = b.x - PANEL_W - 6;
  x = Math.max(disp.x + 4, Math.min(x, disp.x + disp.width - PANEL_W - 4));
  y = Math.max(disp.y + 4, Math.min(y, disp.y + disp.height - PANEL_H - 4));
  return { x: Math.round(x), y: Math.round(y) };
}

// ── 变形动画（renderer rAF 驱动，主进程只做 setBounds）────────
// 统一矩形字段为 {x, y, w, h}（getBounds 返回 width/height，兼容两种输入）
function rectToRb(r) {
  return { x: r.x, y: r.y, w: r.w ?? r.width, h: r.h ?? r.height };
}

// ── 动画超时兜底：renderer 的 rAF 被暂停（最小化/隐藏）时状态机不会卡死 ──
let morphTimer = null;
function armMorphTimeout(kind) {
  clearMorphTimeout();
  morphTimer = setTimeout(() => {
    morphTimer = null;
    console.error('[planner] morph ' + kind + ' 超时，强制完成');
    if (kind === 'in') forceFinishIn();
    else forceFinishOut();
  }, 4000);
}
function clearMorphTimeout() {
  if (morphTimer) {
    clearTimeout(morphTimer);
    morphTimer = null;
  }
}

// 强制完成展开（动画中断/卡死时直接跳到终点）
function forceFinishIn() {
  clearMorphTimeout();
  if (panelWin && !panelWin.isDestroyed()) {
    const target = panelTargetPos();
    panelWin.setBounds({ x: target.x, y: target.y, width: PANEL_W, height: PANEL_H });
    if (panelWin.isMinimized()) panelWin.restore();
    panelWin.show();      // 若从未显示（加载卡住），强制显示
  }
  panelState = 'shown';
  ignoreBlurUntil = Date.now() + 500;
  if (panelWin && !panelWin.isDestroyed()) panelWin.focus();
  // 通知 renderer 同步 UI 状态（防止内容层停在透明/动画中）
  if (panelLoaded && panelWin && !panelWin.isDestroyed()) {
    panelWin.webContents.send('morph-force-finish', 'in');
  }
}

// 强制完成收起（动画中断/卡死时直接隐藏，变回悬浮球）
function forceFinishOut() {
  clearMorphTimeout();
  if (panelWin && !panelWin.isDestroyed()) {
    try { panelWin.restore(); } catch { /* 忽略 */ }
    // restore() 异步：延迟到恢复生效后再 hide，避免「最小化+隐藏」残留状态
    // （该状态会导致下次 show() 不生效——实测"最小化后打不开"）
    setTimeout(() => {
      if (panelWin && !panelWin.isDestroyed() && panelState === 'hidden') {
        panelWin.hide();
      }
    }, 60);
  }
  panelState = 'hidden';
  showBubble();
}

function sendMorph(kind, from, to) {
  const payload = { from: rectToRb(from), to: rectToRb(to) };
  try {
    if (!panelLoaded) {
      pendingMorph = { kind, from: payload.from, to: payload.to, showFirst: kind === 'in' };
      armMorphTimeout(kind);   // 加载也可能卡住
    } else {
      if (kind === 'in') {
        // 先确保脱离最小化状态（restore 异步，show 前调用避免"最小化+隐藏"残留）
        if (panelWin.isMinimized()) panelWin.restore();
        panelWin.show();
      }
      panelWin.webContents.send(kind === 'in' ? 'morph-in' : 'morph-out', payload);
      armMorphTimeout(kind);
    }
  } catch (e) {
    // webContents 已销毁/异常：直接走强制完成路径，避免状态卡死
    console.error('[planner] sendMorph 失败:', e);
    if (kind === 'in') forceFinishIn();
    else forceFinishOut();
  }
}

function morphIn() {
  if (panelState !== 'hidden') return;
  if (!panelWin || panelWin.isDestroyed()) createPanel();
  panelDragged = false;                       // 每次展开重置拖动标记
  const from = bubbleWin.getBounds();            // 球当前矩形
  const to = { ...panelTargetPos(), w: PANEL_W, h: PANEL_H };
  panelWin.setBounds({
    x: from.x, y: from.y, width: BUBBLE_SIZE, height: BUBBLE_SIZE,
  });
  panelState = 'morphing_in';
  if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.hide();  // 球由变形窗口接管
  sendMorph('in', from, to);
}

function morphOut() {
  if (panelState === 'hidden' || panelState === 'morphing_out') return;
  // 面板窗口已不存在（意外销毁）→ 直接重置，绝不能抛异常卡死
  if (!panelWin || panelWin.isDestroyed()) {
    resetPanelState();
    showBubble();
    return;
  }
  const from = panelWin.getBounds();
  // 缩小目标：
  // - 面板被拖离过展开位置 → 悬浮球移到面板当前位置，原地缩成球（跟随面板）
  // - 面板没动过 → 球保持原位，缩回展开前的位置（不漂移）
  if (panelDragged && bubbleWin && !bubbleWin.isDestroyed()) {
    bubbleWin.setPosition(from.x, from.y);
  }
  const to = bubbleWin.getBounds();
  panelState = 'morphing_out';
  sendMorph('out', from, to);
}

function togglePanel() {
  // 窗口没了但状态残留 → 先重置，再按用户意图执行（防"托盘无反应"）
  if ((!panelWin || panelWin.isDestroyed()) && panelState !== 'hidden') {
    resetPanelState();
    showBubble();
  }
  // 卡在动画中（如 rAF 被暂停）→ 先强制结束，再按用户意图切换
  if (panelState === 'morphing_in') {
    forceFinishIn();
  } else if (panelState === 'morphing_out') {
    forceFinishOut();
  }
  if (panelState === 'hidden') {
    morphIn();
  } else {
    morphOut(); // shown / morphing_in 都收（renderer 会打断在途动画）
  }
}

// ── 退出：最暴力可靠的方式 ─────────────────────────────────
// app.quit() 会被 panelWin 的 close 拦截（面板展开时第一次退出"闪一下又出来"）；
// app.exit(0) 实测展开时也不生效（close 事件不触发但进程不退）。
// process.exit() 直接终止进程，100% 可靠。先销毁窗口/托盘清理资源。
function doQuit() {
  quitting = true;
  try {
    killBackend();   // 后端进程不随 electron 退出（detached），残留会导致重启复用旧后端
    if (tray && !tray.isDestroyed()) tray.destroy();
    if (toastWin && !toastWin.isDestroyed()) toastWin.destroy();
    if (panelWin && !panelWin.isDestroyed()) panelWin.destroy();
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.destroy();
  } catch { /* 忽略 */ }
  setTimeout(() => process.exit(0), 50);
}

// 杀掉本实例拉起的后端（残留的旧后端会让下次启动复用旧代码）
function killBackend() {
  try {
    if (backendProc && backendProc.pid) {
      backendProc.kill();
      backendProc = null;
    }
  } catch { /* 忽略 */ }
}

// ── 气泡窗（小助未展开时主动说话的提示）────────────────────
// bubble 窗口轮询 /dequeue 拿到 text 事件 → 这里弹出独立透明气泡窗，
// 显示在悬浮球旁，几秒后自动消失；点击气泡展开面板。
const TOAST_W = 280;
const TOAST_H = 110;
const TOAST_MS = 6000;

let toastWin = null;
let toastTimer = null;
let toastQueue = [];
let toastShowing = false;
let toastLoaded = false;    // toast 页面加载完成（listener 就绪）
let pendingToast = null;    // 加载完成前暂存的 {text, above}

function createToast() {
  if (toastWin && !toastWin.isDestroyed()) return;
  toastWin = new BrowserWindow({
    width: TOAST_W,
    height: TOAST_H,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  toastWin.loadFile(path.join(__dirname, 'renderer', 'toast.html'));
  toastWin.setAlwaysOnTop(true, 'pop-up-menu');
  toastWin.webContents.once('did-finish-load', () => {
    toastLoaded = true;
    if (pendingToast) {
      const p = pendingToast;
      pendingToast = null;
      displayToast(p.text, p.above);
    }
  });
  toastWin.on('closed', () => { toastWin = null; toastLoaded = false; });
}

function showToast(text) {
  if (quitting || panelState !== 'hidden') return;   // 面板展开时不需要气泡
  if (!text || !String(text).trim()) return;
  toastQueue.push(String(text).trim());
  if (!toastShowing) pumpToast();
}

function toastPos() {
  const b = bubbleWin && !bubbleWin.isDestroyed() ? bubbleWin.getBounds() : null;
  const disp = screen.getDisplayNearestPoint({
    x: (b ? b.x + b.width / 2 : 0), y: (b ? b.y : 0),
  }).workArea;
  let x = b ? Math.round(b.x + b.width / 2 - TOAST_W / 2) : disp.x + 40;
  let y = b ? b.y - TOAST_H - 10 : disp.y + 40;
  let above = true;
  if (y < disp.y + 4) {
    y = b ? b.y + b.height + 10 : disp.y + 40;
    above = false;
  }
  x = Math.max(disp.x + 4, Math.min(x, disp.x + disp.width - TOAST_W - 4));
  return { x, y, above };
}

function displayToast(text, above) {
  const pos = toastPos();
  toastWin.setBounds({ x: pos.x, y: pos.y, width: TOAST_W, height: TOAST_H });
  toastWin.webContents.send('toast-text', { text, above });
  toastWin.showInactive();   // 不抢焦点
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (toastWin && !toastWin.isDestroyed()) toastWin.hide();
    pumpToast();
  }, TOAST_MS);
}

function pumpToast() {
  if (quitting || panelState !== 'hidden') {        // 中途展开面板 → 停止队列
    toastShowing = false;
    toastQueue = [];
    pendingToast = null;
    return;
  }
  if (!toastQueue.length) {
    toastShowing = false;
    return;
  }
  toastShowing = true;
  const text = toastQueue.shift();
  if (!toastWin || toastWin.isDestroyed()) {
    createToast();
    toastLoaded = false;
  }
  if (!toastWin || toastWin.isDestroyed()) {
    toastShowing = false;
    return;
  }
  if (!toastLoaded) {
    pendingToast = { text, above: true };   // 加载完成由 did-finish-load 补发
    return;
  }
  displayToast(text, true);
}

// ── 托盘 ────────────────────────────────────────────────────
function createTray() {
  if (tray && !tray.isDestroyed()) return;   // 幂等：避免重复创建
  try {
    tray = new Tray(trayIcon());             // 图标引用由 trayIcon() 缓存持有
  } catch (e) {
    console.error('[planner] 创建托盘失败:', e);
    tray = null;
    return;
  }
  tray.setToolTip('小助 —— 学习工作助手');
  const menu = Menu.buildFromTemplate([
    { label: '打开小助', click: () => { showBubble(); togglePanel(); } },
    { label: '切换免打扰', click: () => toggleDndFromMain() },
    { type: 'separator' },
    { label: '退出', click: () => doQuit() },
  ]);
  tray.setContextMenu(menu);
  tray.on('click', () => { showBubble(); togglePanel(); });
  tray.on('double-click', () => { showBubble(); togglePanel(); });
}

function showBubble() {
  if (panelState !== 'hidden') return;
  if (!bubbleWin || bubbleWin.isDestroyed()) createBubble();
  if (!bubbleWin.isVisible()) bubbleWin.show();
}

// ── 主进程 /dequeue 轮询（唯一消费者）────────────────────
async function pollDequeue() {
  if (quitting) return;
  try {
    const res = await fetch(BACKEND_URL + '/dequeue', { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return;
    const data = await res.json();
    const events = data.events || [];
    if (!events.length) return;
    pendingEvents.push(...events);
    if (pendingEvents.length > 60) pendingEvents.splice(0, pendingEvents.length - 60);
    if (panelState === 'hidden') {
      // 悬浮球形态：文本事件冒气泡
      for (const ev of events) {
        if (ev.type === 'text' && ev.content) showToast(ev.content);
      }
    } else if (panelLoaded && panelWin && !panelWin.isDestroyed()) {
      // 面板展开：推给面板渲染（含 morphing_in/out，事件顺序保持）
      panelWin.webContents.send('events', events);
    }
    // morphing_in 且页面未加载完 → 事件留在 pendingEvents，morph-in-done 时补发
  } catch { /* 后端未连接时静默，下轮再试 */ }
}

function startDequeuePoll() {
  if (dequeueTimer) return;
  dequeueTimer = setInterval(pollDequeue, 800);
  pollDequeue();
}

function toggleDndFromMain() {
  fetch(BACKEND_URL + '/state')
    .then((r) => r.json())
    .then((d) => {
      const enabled = !d.state.dnd.enabled;
      return fetch(BACKEND_URL + '/dnd', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
    })
    .catch(() => {});
}

// ── IPC：渲染进程 ↔ 主进程 ──────────────────────────────────
ipcMain.on('toggle-panel', () => togglePanel());
ipcMain.handle('get-panel-state', () => panelState);
ipcMain.on('toast-show', (e, text) => showToast(text));
ipcMain.on('toast-click', () => {
  // 点击气泡 → 收起气泡并展开面板
  if (toastWin && !toastWin.isDestroyed()) toastWin.hide();
  clearTimeout(toastTimer);
  toastQueue = [];
  toastShowing = false;
  togglePanel();
});
ipcMain.on('show-panel', () => {
  if (panelState === 'hidden') morphIn();
});
ipcMain.on('hide-panel', () => {
  if (panelState !== 'hidden') morphOut();
});
ipcMain.on('set-panel-bounds', (e, b) => {
  if (panelWin && !panelWin.isDestroyed() && panelState !== 'hidden') {
    panelWin.setBounds({
      x: Math.round(b.x), y: Math.round(b.y),
      width: Math.round(b.w), height: Math.round(b.h),
    });
  }
});
// 标题栏拖拽移动面板（仅完全展开时，动画中不动）
ipcMain.on('move-panel', (e, x, y) => {
  if (panelWin && !panelWin.isDestroyed() && panelState === 'shown') {
    panelWin.setPosition(Math.round(x), Math.round(y));
    panelDragged = true;   // 拖过面板：缩小跟随面板位置
  }
});
ipcMain.on('morph-in-done', () => {
  clearMorphTimeout();
  panelState = 'shown';
  ignoreBlurUntil = Date.now() + 500;   // 防 focus 延迟失败的闪回
  if (panelWin && !panelWin.isDestroyed()) {
    panelWin.focus();
    // 补发竞态窗口（展开瞬间）被主进程缓存的事件，避免工具卡片/文本丢失
    if (pendingEvents.length) {
      panelWin.webContents.send('events', pendingEvents);
      pendingEvents = [];
    }
    // 通知 renderer 补渲染隐藏期间的气泡消息
    panelWin.webContents.send('panel-shown');
  }
});
ipcMain.on('morph-out-done', () => {
  clearMorphTimeout();
  panelState = 'hidden';
  if (panelWin && !panelWin.isDestroyed()) panelWin.hide();
  showBubble();
});
ipcMain.handle('get-bubble-pos', () => {
  if (bubbleWin && !bubbleWin.isDestroyed()) {
    const b = bubbleWin.getBounds();
    return { x: b.x, y: b.y };
  }
  return { x: 0, y: 0 };
});
ipcMain.handle('get-panel-pos', () => {
  if (panelWin && !panelWin.isDestroyed()) {
    const b = panelWin.getBounds();
    return { x: b.x, y: b.y };
  }
  return { x: 0, y: 0 };
});
ipcMain.on('move-bubble', (e, x, y) => {
  if (bubbleWin && !bubbleWin.isDestroyed() && panelState === 'hidden') {
    bubbleWin.setPosition(Math.round(x), Math.round(y));
  }
});
ipcMain.on('quit-app', () => doQuit());ipcMain.on('bubble-menu', (e) => {
  const menu = Menu.buildFromTemplate([
    { label: '打开小助', click: () => togglePanel() },
    { label: '切换免打扰', click: () => toggleDndFromMain() },
    { type: 'separator' },
    { label: '退出', click: () => doQuit() },
  ]);
  menu.popup({ window: bubbleWin });
});

// ── 单实例锁：重复启动时唤醒已有实例，避免多个托盘图标互相干扰 ──
const gotSingleLock = app.requestSingleInstanceLock();
if (!gotSingleLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showBubble();
    togglePanel();
  });
  app.whenReady().then(async () => {
    createBubble();
    createTray();
    ensureBackend(); // 后端不在线则自动拉起
    startDequeuePoll(); // 主进程独占 /dequeue 消费，按面板状态分发
  });
}

app.on('window-all-closed', () => {
  // 悬浮球常驻托盘，不随窗口关闭退出
});

app.on('before-quit', () => {
  quitting = true;
  killBackend();   // 任何退出路径都清掉后端，防残留导致重启复用旧代码
});

app.on('will-quit', () => {
});





