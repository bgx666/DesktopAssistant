// main.js —— Electron 悬浮球主进程
// 悬浮球（常驻置顶，可拖拽） + 对话面板（点击球"变形"展开，失焦"变形"收回）

const { app, BrowserWindow, Tray, Menu, nativeImage, screen, ipcMain, session } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const BACKEND_URL = process.env.PLANNER_URL || 'http://127.0.0.1:18771';
const PROJECT_ROOT = path.join(app.getAppPath(), '..');       // frontend/ → planner/
const PYTHON = process.env.PLANNER_PYTHON || 'D:\\Miniconda3\\python.exe';
const TRAY_ICON_FILE = path.join(__dirname, 'assets', 'bubble_32.png');   // 悬浮窗同款托盘图标
const APP_ICON_FILE = path.join(__dirname, 'assets', 'bubble.ico');      // 应用/快捷方式图标

// 独立实例：release 版设 PLANNER_USER_DATA，避免与开发版共用 userData
// （单实例锁、悬浮球位置等全部随之隔离，两个版本可同时运行）
if (process.env.PLANNER_USER_DATA) {
  app.setPath('userData', process.env.PLANNER_USER_DATA);
}

const BUBBLE_SIZE = 100;     // 悬浮球窗口尺寸（core 44px 居中，四周留 ~28px 给辉光）
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
    icon: APP_ICON_FILE,   // 窗口/任务栏图标（悬浮窗同款）
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  bubbleWin.loadFile(path.join(__dirname, 'renderer', 'bubble.html'));
  // 悬浮球最高层级（pop-up-menu > floating 气泡），保证不被气泡/其他窗口遮挡
  bubbleWin.setAlwaysOnTop(true, 'pop-up-menu');
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
  // 面板与悬浮球同级最高层级
  panelWin.setAlwaysOnTop(true, 'pop-up-menu');
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
  // 面板处于焦点时按 Esc → 缩小（变形收回）
  panelWin.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'Escape' && panelState === 'shown') {
      event.preventDefault();
      morphOut();
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

// ── 面板目标位置：贴悬浮球视觉边缘旁，夹在屏幕工作区内 ──────────
function panelTargetPos() {
  const b = bubbleWin.getBounds();
  const disp = screen.getDisplayNearestPoint({ x: b.x, y: b.y }).workArea;
  // 球窗口比视觉球大（辉光留白）：以球视觉中心（=窗口中心）定位，面板贴球边缘
  const ballCX = b.x + b.width / 2;
  const ballCY = b.y + b.height / 2;
  const BALL_R = 22;   // 视觉球半径（44/2）
  let x = ballCX + BALL_R + 6;
  let y = ballCY - Math.round(PANEL_H / 2);
  if (x + PANEL_W > disp.x + disp.width) x = ballCX - BALL_R - PANEL_W - 6;
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
  clearAllToasts();                           // 展开面板时清掉所有气泡
  const from = bubbleWin.getBounds();            // 球当前矩形（renderer 起始 transform 用）
  const to = { ...panelTargetPos(), w: PANEL_W, h: PANEL_H };
  // 窗口一次到位（面板最终位置尺寸），变形动画由 renderer 的 CSS transform
  // 完成（GPU 合成）——不再每帧 resize 窗口，帧率大幅提升
  panelWin.setBounds({
    x: to.x, y: to.y, width: PANEL_W, height: PANEL_H,
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
    // 球窗口对齐面板中心（窗口比视觉球大，球视觉居中 → 视觉球落在面板中心）
    const bw = bubbleWin.getBounds();
    bubbleWin.setPosition(
      Math.round(from.x + (from.width - bw.width) / 2),
      Math.round(from.y + (from.height - bw.height) / 2),
    );
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
    clearAllToasts();
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
// 主进程 /dequeue 拿到 text 事件 → 每个消息一个独立透明气泡窗，
// 从悬浮球上方由下往上堆叠（新消息在最下面、旧的被顶上去）；
// 每个气泡生存 TOAST_MS 后自动消失；点击气泡展开面板。
const TOAST_W = 280;
const TOAST_H = 110;                 // 初始高度（内容多高窗口自适应多高）
const TOAST_MS = 30000;              // 每个气泡生存 30 秒

let toastWins = [];                     // 堆叠的气泡窗口（[0] 最靠近球=最新）

function createToastWin(text, offset = 0, history = false) {
  const win = new BrowserWindow({
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
  win._toastOffset = offset;   // 距最新偏移（0=最新）
  win._isHistory = history;    // true=单击翻出的历史消息；false=小助主动说话
  win.loadFile(path.join(__dirname, 'renderer', 'toast.html'));
  // 气泡层级低于悬浮球（floating > 普通窗口，但 < pop-up-menu）：
  // 悬浮球永远不被气泡遮挡
  win.setAlwaysOnTop(true, 'floating');
  const timer = setTimeout(() => destroyToastWin(win), TOAST_MS);
  win.on('closed', () => {
    clearTimeout(timer);
    const i = toastWins.indexOf(win);
    if (i >= 0) toastWins.splice(i, 1);
    layoutToasts();
  });
  win.webContents.once('did-finish-load', () => {
    if (win.isDestroyed()) return;
    win.webContents.send('toast-text', { text, above: true });
    win.showInactive();   // 不抢焦点
    layoutToasts();
  });
  // 堆叠位置（统一时间序：越往下越新，越往上越早）：
  // - 普通消息（模型生成）：插到最底（贴球），旧气泡被顶上去
  // - 历史消息（单击翻出，off=距最新偏移）：插到比它新的气泡之后——
  //   第 1 次点击（off0 最近回复）在最底，之后点击的更早消息依次往上
  if (!history) {
    toastWins.unshift(win);
  } else {
    let idx = -1;
    for (let i = toastWins.length - 1; i >= 0; i--) {
      const w = toastWins[i];
      if (w._isHistory && w._toastOffset < offset) { idx = i + 1; break; }
      if (!w._isHistory) { idx = i + 1; break; }   // 普通消息更底 → 插到其后
    }
    toastWins.splice(idx < 0 ? toastWins.length : idx, 0, win);
  }
  layoutToasts();
}

function destroyToastWin(win) {
  try {
    if (win && !win.isDestroyed()) win.destroy();
  } catch { /* 忽略 */ }
}

function clearAllToasts() {
  const wins = toastWins.slice();
  toastWins = [];
  for (const w of wins) destroyToastWin(w);
  // 通知悬浮球：气泡已清空，下次单击重新从最近一条开始翻历史
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) {
      bubbleWin.webContents.send('toasts-cleared');
    }
  } catch { /* 忽略 */ }
}

// 堆叠布局（统一时间序）：数组 [0] 最新贴球（最下），越往后越旧越靠上。
// 高度按各窗口实际高度动态累积（长文本气泡自适应撑高）。
function layoutToasts() {
  if (!toastWins.length) return;
  const b = bubbleWin && !bubbleWin.isDestroyed() ? bubbleWin.getBounds() : null;
  const disp = screen.getDisplayNearestPoint({
    x: (b ? b.x + b.width / 2 : 0), y: (b ? b.y : 0),
  }).workArea;
  const x = Math.max(disp.x + 4,
    Math.min(b ? Math.round(b.x + b.width / 2 - TOAST_W / 2) : disp.x + 40,
             disp.x + disp.width - TOAST_W - 4));
  const above = b ? (b.y - 20 >= disp.y + 4) : true;
  // 球视觉顶部（窗口比视觉球大：core 44px 居中，四周留白）
  const ballTop = b ? b.y + Math.round((b.height - 44) / 2) : 0;
  const ballBottom = b ? ballTop + 44 : 0;
  // 游标：above → 下一个气泡的底部位置；否则 → 下一个气泡的顶部位置
  let cursor = above
    ? (b ? ballTop - 10 : disp.y + 40 + 110)
    : (b ? ballBottom + 10 : disp.y + 40);
  for (const win of toastWins) {
    if (win.isDestroyed()) continue;
    const h = win.getBounds().height;
    const top = above ? cursor - h : cursor;
    const wy = Math.max(disp.y + 4,
      Math.min(top, disp.y + disp.height - h - 4));
    win.setBounds({ x, y: wy, width: TOAST_W, height: h });
    cursor = above ? top - 6 : cursor + h + 6;
  }
}

function showToast(text, offset = 0, history = false) {
  if (quitting || panelState !== 'hidden') return;   // 面板展开时不需要气泡
  if (!text || !String(text).trim()) return;
  createToastWin(String(text).trim(), offset, history);
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
    { label: '设置', click: () => openSettings() },
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
// 事件 + 状态统一由主进程分发：事件按面板状态（气泡/面板），
// 状态广播给所有窗口（bubble/面板），各窗口不再各自轮询 /state。
async function pollDequeue() {
  if (quitting) return;
  try {
    const res = await fetch(BACKEND_URL + '/dequeue', { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return;
    const data = await res.json();
    const events = data.events || [];
    broadcastState(data.state);
    if (!events.length) return;
    pendingEvents.push(...events);
    if (pendingEvents.length > 60) pendingEvents.splice(0, pendingEvents.length - 60);
    if (panelState === 'hidden') {
      // 悬浮球形态：文本事件冒气泡；audio 事件 → 气泡窗口播放（只读气泡）
      for (const ev of events) {
        if (ev.type === 'text' && ev.content) {
          showToast(ev.content);
        } else if (ev.type === 'audio' && ev.url && bubbleWin && !bubbleWin.isDestroyed()) {
          bubbleWin.webContents.send('audio', ev.url);
        }
      }
    } else if (panelLoaded && panelWin && !panelWin.isDestroyed()) {
      // 面板展开：推给面板渲染（含 morphing_in/out，事件顺序保持）
      panelWin.webContents.send('events', events);
    }
    // morphing_in 且页面未加载完 → 事件留在 pendingEvents，morph-in-done 时补发
  } catch {
    // 后端不可达 → 广播离线状态，各窗口状态点变红
    broadcastState({ offline: true });
  }
}

// 状态广播：bubble 窗口（状态点/逾期徽标）+ 面板窗口（token/心跳/思考状态）
function broadcastState(state) {
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) {
      bubbleWin.webContents.send('state', state);
    }
    if (panelWin && !panelWin.isDestroyed()) {
      panelWin.webContents.send('state', state);
    }
  } catch { /* 窗口销毁中忽略 */ }
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
// 全局挂载文件（拖拽上传）：主进程单点持有，两窗口（悬浮球/面板）共享，
// 变更即广播——球显示文件数徽标、面板显示对话框上方文件条。
let mountedFiles = [];

function broadcastMounted() {
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) {
      bubbleWin.webContents.send('mounted-files', mountedFiles);
    }
    if (panelWin && !panelWin.isDestroyed()) {
      panelWin.webContents.send('mounted-files', mountedFiles);
    }
  } catch { /* 窗口销毁中忽略 */ }
}

ipcMain.on('mount-files', (e, files) => {
  const list = Array.isArray(files) ? files : [];
  if (!list.length) return;
  mountedFiles = mountedFiles.concat(list);
  broadcastMounted();
});
ipcMain.on('clear-mounted', () => {
  if (!mountedFiles.length) return;
  mountedFiles = [];
  broadcastMounted();
});
ipcMain.on('remove-mounted', (e, index) => {
  if (typeof index === 'number' && index >= 0 && index < mountedFiles.length) {
    mountedFiles.splice(index, 1);
    broadcastMounted();
  }
});
ipcMain.handle('get-mounted', () => mountedFiles);
// ── 前端 UI 设置（长按时间等，纯前端参数不走后端，启动即用）──
const UI_SETTINGS_FILE = () => path.join(app.getPath('userData'), 'ui-settings.json');
let uiSettings = { press_ms: 200 };
(function initUiSettings() {
  try {
    // 优先读本地 ui-settings.json（持久化）
    if (fs.existsSync(UI_SETTINGS_FILE())) {
      const raw = JSON.parse(fs.readFileSync(UI_SETTINGS_FILE(), 'utf-8'));
      if (raw && typeof raw.press_ms === 'number') uiSettings.press_ms = raw.press_ms;
      return;
    }
    // 首次：从后端 settings.json 迁移 press_ms（后端旧数据）
    const dataRoot = process.env.PLANNER_DATA_ROOT;
    if (dataRoot) {
      const p = path.join(dataRoot, 'settings.json');
      if (fs.existsSync(p)) {
        const raw = JSON.parse(fs.readFileSync(p, 'utf-8'));
        if (raw && typeof raw.press_ms === 'number') uiSettings.press_ms = raw.press_ms;
      }
    }
  } catch { /* 用默认值 */ }
})();
function saveUiSettingsFile() {
  try {
    fs.writeFileSync(UI_SETTINGS_FILE(), JSON.stringify(uiSettings, null, 2));
  } catch { /* 忽略 */ }
}
function broadcastUiSettings() {
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.webContents.send('ui-settings', uiSettings);
    if (panelWin && !panelWin.isDestroyed()) panelWin.webContents.send('ui-settings', uiSettings);
  } catch { /* 忽略 */ }
}
ipcMain.handle('get-ui-settings', () => uiSettings);
ipcMain.on('save-ui-settings', (e, updates) => {
  const u = updates || {};
  if (typeof u.press_ms === 'number') {
    uiSettings.press_ms = Math.max(50, Math.min(5000, Math.round(u.press_ms)));
  }
  saveUiSettingsFile();
  broadcastUiSettings();
});
ipcMain.on('toggle-panel', () => togglePanel());
// ── 设置窗口 ─────────────────────────────────────────────
let settingsWin = null;
const SETTINGS_DEBUG_LOG = () => path.join(app.getPath('userData'), 'settings-debug.log');
function logSettingsDebug(msg) {
  try {
    fs.appendFileSync(SETTINGS_DEBUG_LOG(), `[${new Date().toISOString()}] ${msg}\n`);
  } catch { /* 忽略 */ }
}
function openSettings() {
  logSettingsDebug('openSettings 被调用');
  try {
    if (settingsWin && !settingsWin.isDestroyed()) {
      logSettingsDebug('已存在，聚焦');
      settingsWin.focus();
      return;
    }
    logSettingsDebug('创建 BrowserWindow');
    settingsWin = new BrowserWindow({
      width: 520, height: 660,
      frame: true, resizable: true, maximizable: false,
      alwaysOnTop: true,
      title: '小助 · 设置',
      show: false,
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    settingsWin.webContents.on('did-fail-load', (e, code, desc) => {
      logSettingsDebug('did-fail-load: ' + code + ' ' + desc);
    });
    settingsWin.webContents.on('did-finish-load', () => {
      logSettingsDebug('did-finish-load OK');
    });
    settingsWin.once('ready-to-show', () => {
      logSettingsDebug('ready-to-show，显示窗口');
      if (settingsWin && !settingsWin.isDestroyed()) {
        settingsWin.show();
        settingsWin.center();        // 强制居中当前主屏
        settingsWin.moveTop();       // 置顶
        settingsWin.focus();
        try {
          const b = settingsWin.getBounds();
          const displays = screen.getAllDisplays().length;
          logSettingsDebug(`窗口位置 ${JSON.stringify(b)} 可见=${settingsWin.isVisible()} 屏数=${displays}`);
        } catch (e2) { logSettingsDebug('位置日志失败: ' + e2); }
      }
    });
    settingsWin.loadFile(path.join(__dirname, 'renderer', 'settings.html'));
    settingsWin.on('closed', () => { settingsWin = null; });
    logSettingsDebug('窗口创建流程完成');
  } catch (err) {
    logSettingsDebug('异常: ' + (err && err.stack ? err.stack : String(err)));
    console.error('[planner] 打开设置窗口失败:', err);
  }
}
ipcMain.on('open-settings', () => openSettings());
// 设置保存后：重新拉取并广播给各窗口（长按时间等前端项即时生效）
ipcMain.on('settings-saved', async () => {
  try {
    const r = await fetch(BACKEND_URL + '/settings', { signal: AbortSignal.timeout(3000) });
    const d = await r.json();
    if (d.ok) broadcastSettings(d.settings);
  } catch { /* 后端不可达忽略 */ }
});
function broadcastSettings(settings) {
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.webContents.send('settings', settings);
    if (panelWin && !panelWin.isDestroyed()) panelWin.webContents.send('settings', settings);
  } catch { /* 忽略 */ }
}
// 输入框状态同步：非空 = 正在输入（模型生成前提示，临时注入不落库）
ipcMain.on('typing-state', (e, typing) => {
  fetch(BACKEND_URL + '/typing', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ typing: !!typing }),
    signal: AbortSignal.timeout(3000),
  }).catch(() => {});
});
// 单击悬浮球 = 按住说话：录音识别在 bubble.js 直接走后端，主进程无需中转
// （nudge 端点保留供未来使用，前端入口已移除）
ipcMain.handle('get-panel-state', () => panelState);
ipcMain.on('toast-show', (e, payload) => {
  // {text, offset, history}：history=单击翻出的历史消息（顶部向下堆叠），
  // 否则为小助主动说话（贴球向上堆叠）
  const p = (typeof payload === 'string') ? { text: payload } : (payload || {});
  showToast(p.text, p.offset || 0, !!p.history);
});
// 气泡内容高度 → 窗口自适应（完整显示长文本，不做省略号）
ipcMain.on('toast-resize', (e, h) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (!win || win.isDestroyed()) return;
  const nh = Math.max(60, Math.min(480, Math.round(h)));
  const b = win.getBounds();
  if (Math.abs(b.height - nh) < 2) return;
  win.setBounds({ x: b.x, y: b.y, width: TOAST_W, height: nh });
  layoutToasts();
});
ipcMain.on('toast-click', (e) => {
  // 点击气泡 → 销毁该气泡并展开面板（其余气泡也清掉）
  const win = BrowserWindow.fromWebContents(e.sender);
  if (win) destroyToastWin(win);
  clearAllToasts();
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
  const items = [
    { label: '放大', click: () => togglePanel() },
    { label: '切换免打扰', click: () => toggleDndFromMain() },
    { label: '设置', click: () => openSettings() },
  ];
  if (mountedFiles.length) {
    items.push({
      label: `清除已挂载文件（${mountedFiles.length}）`,
      click: () => {
        mountedFiles = [];
        broadcastMounted();
      },
    });
  }
  items.push({ type: 'separator' });
  items.push({ label: '清除气泡', click: () => clearAllToasts() });
  items.push({ type: 'separator' }, { label: '退出', click: () => doQuit() });
  const menu = Menu.buildFromTemplate(items);
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
    // 麦克风权限：语音输入（getUserMedia）放行
    session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => {
      callback(permission === 'media');
    });
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





