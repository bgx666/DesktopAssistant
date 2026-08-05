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

const BUBBLE_SIZE = 56;      // 悬浮球尺寸
const PANEL_W = 350;         // 面板宽
const PANEL_H = 520;         // 面板高

let bubbleWin = null;
let panelWin = null;
let tray = null;
let trayImage = null;   // 保持 nativeImage 引用，防止被 GC 导致 Tray 底层对象销毁
let backendProc = null;
let quitting = false;
let clickHook = null;   // 全局鼠标钩子（koffi）


// 面板状态机：hidden → morphing_in → shown → morphing_out → hidden
// 动画帧由 renderer 的 rAF 驱动（60fps），主进程只执行 setBounds
let panelState = 'hidden';
let panelLoaded = false;   // 面板页面是否加载完成（listener 就绪）
let pendingMorph = null;   // 页面加载完成前暂存的变形请求 {kind, from, to, showFirst}
let ignoreBlurUntil = 0;   // morph-in 完成后短暂忽略 blur（focus 延迟失败的兜底）
let blurTimer = null;      // blur 延迟收起（让 click toggle 有机会先执行，解决点球关不掉）

const BUBBLE_STATE_FILE = () => path.join(app.getPath('userData'), 'bubble-pos.json');

// ── 全局鼠标点击监听（koffi / WH_MOUSE_LL）────────────────
// 不依赖窗口焦点：点击面板以外的任何地方 → 收起面板（用户实测 blur 在
// 面板未获得焦点时不可靠，面板会"点外部不缩小"）。
const WH_MOUSE_LL = 14;
const WM_LBUTTONDOWN = 0x0201;

function installClickHook() {
  if (clickHook) return;
  let koffi;
  try {
    koffi = require('koffi');
  } catch {
    console.error('[planner] koffi 不可用，全局点击收起降级为 blur 方案');
    return;
  }
  try {
    const user32 = koffi.load('user32.dll');
    // koffi 3.x：func() 返回可调用函数
    const SetWindowsHookExW = user32.func('int64 SetWindowsHookExW(int32 idHook, void *lpfn, void *hMod, uint32 dwThreadId)');
    const CallNextHookEx = user32.func('int64 CallNextHookEx(int64 hhk, int32 nCode, uint64 wParam, int64 lParam)');
    const UnhookWindowsHookEx = user32.func('int32 UnhookWindowsHookEx(int64 hhk)');
    const HOOKPROC = koffi.proto('int64 LowLevelMouseProc(int32 nCode, uint64 wParam, int64 lParam)');
    const hookProc = koffi.register((nCode, wParam, lParam) => {
      try {
        if (Number(nCode) >= 0 && Number(wParam) === WM_LBUTTONDOWN) {
          setTimeout(() => handleGlobalClick(), 0);   // 转发到主线程
        }
      } catch { /* 忽略 */ }
      return CallNextHookEx(clickHook || 0, nCode, wParam, lParam);
    }, koffi.pointer(HOOKPROC));
    clickHook = SetWindowsHookExW(WH_MOUSE_LL, hookProc, null, 0);
    if (!clickHook) {
      console.error('[planner] 全局鼠标钩子安装失败');
      return;
    }
    console.log('[planner] 全局鼠标钩子已安装');
  } catch (e) {
    console.error('[planner] 全局鼠标钩子初始化失败:', e);
    clickHook = null;
  }
}

function handleGlobalClick() {
  // 用户点击面板外 = 明确收起意图，立即响应（不等待 ignoreBlurUntil——
  // 那是 blur 路径防焦点闪回的，钩子路径不受焦点抖动影响）
  if (panelState !== 'shown') return;
  if (!panelWin || panelWin.isDestroyed()) return;
  const cursor = screen.getCursorScreenPoint();
  const pb = panelWin.getBounds();
  const inPanel = cursor.x >= pb.x && cursor.x <= pb.x + pb.width &&
                  cursor.y >= pb.y && cursor.y <= pb.y + pb.height;
  // 点击悬浮球：让 click 的 toggle 决定收/展，钩子不插手
  if (!inPanel) {
    if (bubbleWin && !bubbleWin.isDestroyed() && bubbleWin.isVisible()) {
      const bb = bubbleWin.getBounds();
      if (cursor.x >= bb.x && cursor.x <= bb.x + bb.width &&
          cursor.y >= bb.y && cursor.y <= bb.y + bb.height) return;
    }
    morphOut();
  }
}

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
  // 点击面板以外的任何地方 → 面板失焦 → 变形收回。
  // 注意：blur 延迟 250ms 再收起——点击悬浮球时 blur 先于 click 到达主进程，
  // 若 blur 立即收起，随后的 click toggle 会立刻重新展开（实测关不掉）。
  // 延迟后：点球 = blur(延迟) → click(toggle 正常收起) → 延迟到点 state 已变，不重复。
  panelWin.on('blur', () => {
    if (panelState !== 'shown' || Date.now() < ignoreBlurUntil) return;
    clearTimeout(blurTimer);
    blurTimer = setTimeout(() => {
      blurTimer = null;
      if (panelState === 'shown') morphOut();
    }, 250);
  });
  panelWin.on('focus', () => {
    clearTimeout(blurTimer);
    blurTimer = null;
  });
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
  if (blurTimer) {
    clearTimeout(blurTimer);
    blurTimer = null;
  }
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
  const to = bubbleWin.getBounds();              // 缩回球的位置
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
    { label: '退出', click: () => { quitting = true; app.quit(); } },
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
ipcMain.on('morph-in-done', () => {
  clearMorphTimeout();
  panelState = 'shown';
  ignoreBlurUntil = Date.now() + 500;   // 防 focus 延迟失败的闪回
  if (panelWin && !panelWin.isDestroyed()) panelWin.focus();
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
ipcMain.on('move-bubble', (e, x, y) => {
  if (bubbleWin && !bubbleWin.isDestroyed() && panelState === 'hidden') {
    bubbleWin.setPosition(Math.round(x), Math.round(y));
  }
});
ipcMain.on('quit-app', () => { quitting = true; app.quit(); });
ipcMain.on('bubble-menu', (e) => {
  const menu = Menu.buildFromTemplate([
    { label: '打开小助', click: () => togglePanel() },
    { label: '切换免打扰', click: () => toggleDndFromMain() },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit(); } },
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
    installClickHook(); // 全局鼠标点击监听（点击面板外 → 收起）
  });
}

app.on('window-all-closed', () => {
  // 悬浮球常驻托盘，不随窗口关闭退出
});

app.on('before-quit', () => {
  quitting = true;
  try {
    if (clickHook) {
      const koffi = require('koffi');
      const user32 = koffi.load('user32.dll');
      const UnhookWindowsHookEx = user32.func('int32 UnhookWindowsHookEx(int64 hhk)');
      UnhookWindowsHookEx(clickHook);
      clickHook = null;
    }
  } catch { /* 忽略 */ }
});

