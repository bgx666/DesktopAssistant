// main.js —— Electron 悬浮球主进程
// 悬浮球（常驻置顶，可拖拽） + 对话面板（点击球"变形"展开，失焦"变形"收回）

const { app, BrowserWindow, Tray, Menu, nativeImage, screen, ipcMain, session, dialog, shell, desktopCapturer, globalShortcut } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');

const BACKEND_URL = process.env.PLANNER_URL || 'http://127.0.0.1:18771';
const PROJECT_ROOT = path.join(app.getAppPath(), '..');       // frontend/ → planner/
const PYTHON = process.env.PLANNER_PYTHON || 'python';
const TRAY_ICON_FILE = path.join(__dirname, 'assets', 'bubble_32.png');   // 悬浮窗同款托盘图标
const APP_ICON_FILE = path.join(__dirname, 'assets', 'bubble.ico');      // 应用/快捷方式图标

// Windows 任务栏分组（AppUserModelID）：不设的话开发/打包环境的任务栏都归到 "Electron"
app.setAppUserModelId('com.xiaozhu.planner');

// 独立实例：release 版设 PLANNER_USER_DATA，避免与开发版共用 userData
// （单实例锁、悬浮球位置等全部随之隔离，两个版本可同时运行）
if (process.env.PLANNER_USER_DATA) {
  app.setPath('userData', process.env.PLANNER_USER_DATA);
}

const BUBBLE_SIZE = 100;     // 悬浮球窗口尺寸（core 44px 居中，四周留 ~28px 给辉光）
const PANEL_W_DEFAULT = 680; // 面板默认宽（大窗，看代码用；可拖拽边缘调整并持久化）
const PANEL_H_DEFAULT = 760; // 面板默认高
const PANEL_MIN = 320;       // 拖拽调整下限
let panelW = PANEL_W_DEFAULT;
let panelH = PANEL_H_DEFAULT;

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
let panelDragged = false;  // 面板是否被拖离过展开位置（仅用于统计/调试，不再影响球位置）

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
    const res = await fetch(BACKEND_URL + '/init', { signal: AbortSignal.timeout(1000) });
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
      autoplayPolicy: 'no-user-gesture-required',   // 允许无手势自动播放（语音朗读）
    },
  });
  bubbleWin.loadFile(path.join(__dirname, 'renderer', 'bubble.html'));
  // 窗口形状 = 圆形可点区（球 + 少量余量）：透明区（圆外）点击穿透到下层窗口。
  // 用 setShape（Windows 原生窗口区域）而非 setIgnoreMouseEvents——
  // 后者的 forward 选项仅 macOS 支持，Windows 上会连球都点不到。
  applyBubbleShape();
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
  appLog('[win] bubble created');
}

// ── 悬浮球窗口形状：圆形可点区 ─────────────────────────────
// 视觉球 44px（半径 22）居中，可点半径取 32（球 + 10px 余量）；
// 圆外透明区通过窗口形状穿透（点击落到下层窗口）。
// setShape 用横向条带矩形近似圆形（48 条，边缘误差 <1px，肉眼无感）。
// 条带必须无缝：所有分界 y 坐标先统一取整（共享数组），相邻条带
// 用同一个值——否则各自 round 会留 1px 缝隙，窗口内容被切出横线。
function applyBubbleShape() {
  if (!bubbleWin || bubbleWin.isDestroyed() || process.platform !== 'win32') return;
  try {
    const r = 32;
    const cx = BUBBLE_SIZE / 2;
    const cy = BUBBLE_SIZE / 2;
    const strips = 48;
    const ys = [];
    for (let i = 0; i <= strips; i++) {
      ys.push(Math.round(cy - r + (i * 2 * r) / strips));
    }
    const rects = [];
    for (let i = 0; i < strips; i++) {
      const y0 = ys[i];
      const y1 = ys[i + 1];
      if (y1 <= y0) continue;
      const ym = (y0 + y1) / 2 - cy;
      const halfW = Math.sqrt(Math.max(0, r * r - ym * ym));
      rects.push({
        x: Math.round(cx - halfW),
        y: y0,
        width: Math.round(2 * halfW),
        height: y1 - y0,
      });
    }
    bubbleWin.setShape(rects);
  } catch { /* 平台不支持则整窗可点（退回原行为） */ }
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
    alwaysOnTop: false,   // 展开的面板不常驻最前（用户设定）：可被其他窗口盖住
    // 面板在任务栏有标签（点任务栏可找到/切回小助）；收起（hide）后标签自动消失。
    // 悬浮球/气泡窗口仍 skipTaskbar: true——托盘型的小部件不占任务栏。
    skipTaskbar: false,
    icon: APP_ICON_FILE,  // 任务栏图标（圆球）
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      autoplayPolicy: 'no-user-gesture-required',   // 允许无手势自动播放（语音朗读）
    },
  });
  panelWin.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  appLog('[win] panel created');
  // 面板不设 always-on-top（用户设定）；被压住时用托盘图标/重复启动把它拉回前台
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
  // 任务栏最小化（skipTaskbar:false 后用户可点任务栏图标）：**稳定展开态不干预**，
  // 走系统标准最小化/还原（点一下缩小、再点还原，与普通窗口一致）。
  // 只有"变形动画进行中"被最小化才要处理——renderer 的 rAF 会随最小化暂停、动画冻住：
  //   - 展开动画中 → 直接落到"已展开"终态（面板保持最小化，点任务栏可还原）；
  //   - 收起动画中 → 完成"变回悬浮球"：restore 再延迟 hide（避免在"最小化+隐藏"残留
  //     状态下留出任务栏幽灵按钮，历史上点它会又冒出一个面板+同时出现悬浮球而卡住）。
  panelWin.on('minimize', () => {
    if (panelState === 'shown') {
      return;
    }
    if (panelState === 'morphing_in') {
      clearMorphTimeout();
      panelState = 'shown';
      ignoreBlurUntil = Date.now() + 500;
      if (panelLoaded && panelWin && !panelWin.isDestroyed()) {
        panelWin.webContents.send('morph-force-finish', 'in');
      }
      return;
    }
    if (panelState === 'morphing_out') {
      clearMorphTimeout();
      panelState = 'hidden';
      if (panelLoaded && panelWin && !panelWin.isDestroyed()) {
        panelWin.webContents.send('morph-force-finish', 'out');
      }
      try { panelWin.restore(); } catch { /* 忽略 */ }
      setTimeout(() => {
        if (panelWin && !panelWin.isDestroyed() && panelState === 'hidden') {
          panelWin.hide();
        }
      }, 60);
      showBubble();
    }
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
function panelSize() {
  // 面板实际尺寸（宽模式在屏幕上放不下时按工作区收缩）
  const b = bubbleWin.getBounds();
  const disp = screen.getDisplayNearestPoint({ x: b.x, y: b.y }).workArea;
  return { w: Math.min(panelW, disp.width - 8), h: Math.min(panelH, disp.height - 8) };
}

function panelTargetPos() {
  const b = bubbleWin.getBounds();
  const disp = screen.getDisplayNearestPoint({ x: b.x, y: b.y }).workArea;
  const size = panelSize();
  // 球窗口比视觉球大（辉光留白）：以球视觉中心（=窗口中心）定位，面板贴球边缘
  const ballCX = b.x + b.width / 2;
  const ballCY = b.y + b.height / 2;
  const BALL_R = 22;   // 视觉球半径（44/2）
  let x = ballCX + BALL_R + 6;
  let y = ballCY - Math.round(size.h / 2);
  if (x + size.w > disp.x + disp.width) x = ballCX - BALL_R - size.w - 6;
  x = Math.max(disp.x + 4, Math.min(x, disp.x + disp.width - size.w - 4));
  y = Math.max(disp.y + 4, Math.min(y, disp.y + disp.height - size.h - 4));
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
    const size = panelSize();
    panelWin.setBounds({ x: target.x, y: target.y, width: size.w, height: size.h });
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
  hidePendingWindow();                        // 面板里有审批弹窗/提问卡，浮层让位
  const from = bubbleWin.getBounds();            // 球当前矩形（renderer 起始 transform 用）
  const size = panelSize();
  const to = { ...panelTargetPos(), w: size.w, h: size.h };
  // 窗口一次到位（面板最终位置尺寸），变形动画由 renderer 的 CSS transform
  // 完成（GPU 合成）——不再每帧 resize 窗口，帧率大幅提升
  panelWin.setBounds({
    x: to.x, y: to.y, width: size.w, height: size.h,
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
  // 缩小目标 = 悬浮球的**原位置**（2026-10-20 用户要求）：
  // 拖动展开的面板不改变球的位置——收起时球留在用户上次放它的地方，
  // 面板动画飞回球的位置。不要再按"面板中心"重定位球。
  const to = bubbleWin.getBounds();
  panelState = 'morphing_out';
  sendMorph('out', from, to);
}

// ── 面板置前（面板不常驻最前，被压住时需要能拉回来）──────────
function focusPanel() {
  if (!panelWin || panelWin.isDestroyed()) return;
  try {
    if (panelWin.isMinimized()) panelWin.restore();
    panelWin.show();
    panelWin.moveTop();
    panelWin.focus();
  } catch { /* 窗口销毁中忽略 */ }
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
  } else if (panelWin && !panelWin.isDestroyed()) {
    // 面板已展开：置前 + 聚焦。面板不再常驻最前（用户设定），托盘/重复启动需要
    // 能把被其他窗口压住的面板拉回来；收起请用界面「—」按钮或 Esc。
    focusPanel();
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
const TOAST_MS_DEFAULT = 30000;      // 默认每个气泡生存 30 秒（设置页可调）

// 气泡显示时长：设置页「交互 → 气泡显示时长」（ui-settings.json，无需后端）
function toastLifetimeMs() {
  const s = Number(uiSettings.toast_seconds);
  const sec = Number.isFinite(s) && s > 0 ? s : TOAST_MS_DEFAULT / 1000;
  return Math.max(3000, Math.min(600000, Math.round(sec * 1000)));
}

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
  const timer = setTimeout(() => destroyToastWin(win), toastLifetimeMs());
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

// ── 快速输入窗（悬浮球模式：全局快捷键 → 球旁小输入框）─────────
// 需求（2026-10-19 用户）：不展开面板也能输入——左 Alt+J 在球附近弹出
// 小输入框（输入 + 语音 + 发送 + 待发送文件），Enter 发送、Esc 收起。
// 快捷键可在设置页改（Electron 加速键写法，默认 Alt+J）。
const QUICK_W = 380;
const QUICK_MIN_H = 64;
let quickWin = null;
let quickAnchor = 'above';   // 'above' | 'below'：高度变化时固定哪条边（贴球那侧）

function createQuickInput() {
  if (quickWin && !quickWin.isDestroyed()) return;
  quickWin = new BrowserWindow({
    width: QUICK_W,
    height: QUICK_MIN_H,
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
      autoplayPolicy: 'no-user-gesture-required',   // 语音输入/播报
      backgroundThrottling: false,   // 隐藏窗口也要正常跑定时器（发送/计数不拖慢）
    },
  });
  quickWin.loadFile(path.join(__dirname, 'renderer', 'quick-input.html'));
  quickWin.setAlwaysOnTop(true, 'pop-up-menu');
  // 点到别处 → 收起（临时浮层，不常驻）
  quickWin.on('blur', () => { if (!quitting) hideQuickInput(); });
  quickWin.on('closed', () => { quickWin = null; });
  appLog('[win] quick input created');
}

function quickVisible() {
  return !!(quickWin && !quickWin.isDestroyed() && quickWin.isVisible());
}

function hideQuickInput() {
  if (!quickVisible()) return;
  try { quickWin.webContents.send('quick-hide'); } catch { /* 忽略 */ }
  quickWin.hide();
  refreshPendingWindow();   // 关闭输入浮层后，未处理的请求重新冒出来
}

function positionQuickInput() {
  if (!quickWin || quickWin.isDestroyed()) return;
  const b = (bubbleWin && !bubbleWin.isDestroyed())
    ? bubbleWin.getBounds()
    : { x: screen.getCursorScreenPoint().x, y: screen.getCursorScreenPoint().y, width: 0, height: 0 };
  const disp = screen.getDisplayNearestPoint(
    { x: b.x + b.width / 2, y: b.y + b.height / 2 }).workArea;
  const h = quickWin.getBounds().height;
  let x = Math.round(b.x + b.width / 2 - QUICK_W / 2);
  x = Math.max(disp.x + 8, Math.min(x, disp.x + disp.width - QUICK_W - 8));
  // 视觉球上下沿（窗口比视觉球大：44px 球居中）
  const ballTop = b.y + Math.round((b.height - 44) / 2);
  const ballBottom = ballTop + 44;
  // 优先放在球**下方**（气泡 toast 都堆在球上方；2026-10-20 用户要求），
  // 下方放不下（贴屏幕底）再放上方。
  let y = ballBottom + 10;
  quickAnchor = 'below';
  if (y + h > disp.y + disp.height - 8) {
    y = ballTop - h - 10;
    quickAnchor = 'above';
  }
  y = Math.max(disp.y + 8, Math.min(y, disp.y + disp.height - h - 8));
  quickWin.setBounds({ x, y, width: QUICK_W, height: h });
}

function showQuickInput() {
  if (quitting) return;
  if (panelState !== 'hidden') return;   // 面板展开时自带输入框，不弹浮层
  hidePendingWindow();                   // 两个浮层都在球旁，先让位给输入
  if (!quickWin || quickWin.isDestroyed()) createQuickInput();
  positionQuickInput();
  quickWin.show();
  quickWin.focus();
  try { quickWin.webContents.focus(); } catch { /* 忽略 */ }
  try { quickWin.webContents.send('quick-show'); } catch { /* 忽略 */ }
  appLog('[quick] shown');
}

function toggleQuickInput() {
  if (quickVisible()) { hideQuickInput(); return; }
  showQuickInput();
}

// 全局快捷键：设置页可改（Electron 加速键，默认 Alt+J）
let quickHotkey = null;
function registerQuickInputShortcut() {
  if (quickHotkey) {
    try { globalShortcut.unregister(quickHotkey); } catch { /* 忽略 */ }
    quickHotkey = null;
  }
  const wanted = String(uiSettings.quick_input_hotkey || 'Alt+J').trim() || 'Alt+J';
  try {
    if (globalShortcut.register(wanted, () => toggleQuickInput())) {
      quickHotkey = wanted;
      appLog('[quick] hotkey registered: ' + wanted);
    } else {
      appLog('[quick] hotkey 注册失败（可能被其他程序占用）: ' + wanted);
    }
  } catch (e) {
    appLog('[quick] hotkey 注册异常: ' + (e && e.message ? e.message : String(e)));
  }
}

ipcMain.on('quick-hide', () => hideQuickInput());
// 内容高度变化 → 窗口自适应（贴着球的那条边固定）
ipcMain.on('quick-resize', (e, h) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (!win || win !== quickWin || win.isDestroyed()) return;
  const nh = Math.max(QUICK_MIN_H, Math.min(420, Math.round(Number(h) || QUICK_MIN_H)));
  const b = win.getBounds();
  if (Math.abs(b.height - nh) < 2) return;
  let y = quickAnchor === 'above' ? b.y + b.height - nh : b.y;
  // 贴球那侧固定生长；再夹到屏幕内（贴底时下方生长不越界）
  const disp = screen.getDisplayNearestPoint(
    { x: b.x + b.width / 2, y: y + nh / 2 }).workArea;
  y = Math.max(disp.y + 8, Math.min(y, disp.y + disp.height - nh - 8));
  win.setBounds({ x: b.x, y, width: QUICK_W, height: nh });
});

// ── 待处理请求（审批 / ask_user 提问）：悬浮球形态的提醒与就地处理 ──
// 2026-10-19 用户需求：面板隐藏时不再强行展开面板；在球旁冒一个提醒浮层，
// 悬浮球同时黄灯呼吸；允许/拒绝/选项/自由回答都能就地完成（复杂授权选项点
// 「展开面板」）。处理完（approval_resolved / question_resolved）自动恢复。
const pendingApprovals = new Map();
const pendingQuestions = new Map();
const PENDING_W = 340;
let pendingWin = null;
let pendingAnchor = 'above';

function pendingSummary() {
  const approvals = Array.from(pendingApprovals.values());
  const questions = Array.from(pendingQuestions.values());
  return {
    approval: approvals[approvals.length - 1] || null,
    question: questions[questions.length - 1] || null,
    count: pendingApprovals.size + pendingQuestions.size,
  };
}

function broadcastPending() {
  const p = pendingSummary();
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.webContents.send('pending', p);
    if (pendingWin && !pendingWin.isDestroyed()) pendingWin.webContents.send('pending', p);
  } catch { /* 窗口销毁中忽略 */ }
}

function trackPending(ev) {
  const id = ev && ev.id;
  if (!id) return;
  if (ev.type === 'approval_request') pendingApprovals.set(id, ev);
  else if (ev.type === 'question_request') pendingQuestions.set(id, ev);
  else if (ev.type === 'approval_resolved') pendingApprovals.delete(id);
  else if (ev.type === 'question_resolved') pendingQuestions.delete(id);
}

function clearPendingAll() {
  if (!pendingApprovals.size && !pendingQuestions.size) return;
  pendingApprovals.clear();
  pendingQuestions.clear();
  broadcastPending();
  hidePendingWindow();
}

function createPendingWindow() {
  if (pendingWin && !pendingWin.isDestroyed()) return;
  pendingWin = new BrowserWindow({
    width: PENDING_W,
    height: 150,
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
      autoplayPolicy: 'no-user-gesture-required',
      backgroundThrottling: false,   // 倒计时/高度自适应在隐藏时不降频
    },
  });
  pendingWin.loadFile(path.join(__dirname, 'renderer', 'pending.html'));
  pendingWin.setAlwaysOnTop(true, 'pop-up-menu');
  // 不因失焦收起：这是"有任务需要处理"的持续提醒；× 或处理完才消失
  pendingWin.on('closed', () => { pendingWin = null; });
  appLog('[win] pending created');
}

function pendingVisible() {
  return !!(pendingWin && !pendingWin.isDestroyed() && pendingWin.isVisible());
}

function hidePendingWindow() {
  if (!pendingVisible()) return;
  pendingWin.hide();
}

function positionPendingWindow() {
  if (!pendingWin || pendingWin.isDestroyed()) return;
  const b = (bubbleWin && !bubbleWin.isDestroyed())
    ? bubbleWin.getBounds()
    : { x: screen.getCursorScreenPoint().x, y: screen.getCursorScreenPoint().y, width: 0, height: 0 };
  const disp = screen.getDisplayNearestPoint(
    { x: b.x + b.width / 2, y: b.y + b.height / 2 }).workArea;
  const h = pendingWin.getBounds().height;
  let x = Math.round(b.x + b.width / 2 - PENDING_W / 2);
  x = Math.max(disp.x + 8, Math.min(x, disp.x + disp.width - PENDING_W - 8));
  // 优先球**下方**（与快速输入一致；球上方留给 toast 气泡），下方放不下再放上方
  const ballTop = b.y + Math.round((b.height - 44) / 2);
  const ballBottom = ballTop + 44;
  let y = ballBottom + 10;
  pendingAnchor = 'below';
  if (y + h > disp.y + disp.height - 8) {
    y = ballTop - h - 10;
    pendingAnchor = 'above';
  }
  y = Math.max(disp.y + 8, Math.min(y, disp.y + disp.height - h - 8));
  pendingWin.setBounds({ x, y, width: PENDING_W, height: h });
}

function showPendingWindow() {
  if (!pendingWin || pendingWin.isDestroyed()) createPendingWindow();
  if (!pendingWin || pendingWin.isDestroyed()) return;
  positionPendingWindow();
  pendingWin.showInactive();   // 不抢当前焦点（只提醒，用户点它才交互）
  try { pendingWin.webContents.send('pending', pendingSummary()); } catch { /* 忽略 */ }
}

function refreshPendingWindow() {
  const count = pendingApprovals.size + pendingQuestions.size;
  if (!count) { hidePendingWindow(); return; }
  // 面板展开时面板里有审批弹窗/提问卡；正在用快速输入浮层时也不打扰
  if (panelState !== 'hidden' || quickVisible()) { hidePendingWindow(); return; }
  showPendingWindow();
}

ipcMain.handle('get-pending', () => pendingSummary());
ipcMain.on('pending-close', () => hidePendingWindow());
ipcMain.on('pending-expand', () => {
  hidePendingWindow();
  if (panelState === 'hidden') morphIn();
});
ipcMain.on('pending-resize', (e, h) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (!win || win !== pendingWin || win.isDestroyed()) return;
  const nh = Math.max(100, Math.min(460, Math.round(Number(h) || 150)));
  const b = win.getBounds();
  if (Math.abs(b.height - nh) < 2) return;
  let y = pendingAnchor === 'above' ? b.y + b.height - nh : b.y;
  const disp = screen.getDisplayNearestPoint(
    { x: b.x + b.width / 2, y: y + nh / 2 }).workArea;
  y = Math.max(disp.y + 8, Math.min(y, disp.y + disp.height - nh - 8));
  win.setBounds({ x: b.x, y, width: PENDING_W, height: nh });
});

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

// ── 主进程 /dequeue 长轮询（唯一消费者）────────────────────
// 事件 + 状态统一由主进程分发：事件按面板状态（气泡/面板），
// 状态广播给所有窗口（bubble/面板），各窗口不再各自轮询 /state。
// 主进程健康检查：用轻量接口判断后端是否在线，成功后立刻广播状态（变绿），
// 再开始 /dequeue 长轮询。避免首次长轮询无事件时挂 25 秒导致红点迟迟不灭。
async function checkBackend() {
  if (quitting) return;
  dequeueTimer = null;
  try {
    const res = await fetch(BACKEND_URL + '/state', { signal: AbortSignal.timeout(2000) });
    if (!res.ok) throw new Error('bad status');
    const data = await res.json();
    setBackendOnline(true);
    broadcastState(data.state);
    broadcastPanelState();
    // 后端刚就绪：补发一次设置。渲染进程可能在后端就绪**之前**启动（start.bat
    // 同时拉起，后端初始化要几秒）——voiceMode attach 时的一次性 /settings 拉取
    // 会静默失败，语音播报开关等拿不到真实值（实测：关掉播报仍在朗读的直接原因）。
    fetchSettingsAndBroadcast();
    startDequeuePoll();
  } catch {
    // 后端尚未就绪 → 广播离线状态，短暂退避后重试健康检查
    setBackendOnline(false);
    broadcastState({ offline: true });
    if (!quitting) dequeueTimer = setTimeout(checkBackend, 1000);
  }
}

// 拉取后端设置并广播给各窗口（'settings' 事件 → 渲染进程 onSettings）
async function fetchSettingsAndBroadcast() {
  try {
    const r = await fetch(BACKEND_URL + '/settings', { signal: AbortSignal.timeout(3000) });
    const d = await r.json();
    if (d.ok) broadcastSettings(d.settings);
  } catch { /* 后端不可达：下次在线转换/保存时再补 */ }
}

async function pollDequeue() {
  if (quitting) return;
  dequeueTimer = null;
  let online = false;
  try {
    // 长轮询：后端最多挂 25s（有事件立即返回），客户端 30s 超时兜底。
    // 相比固定 800ms 轮询，事件投递延迟从 ~400ms 降到接近 0（真"实时"）。
    const res = await fetch(BACKEND_URL + '/dequeue?wait=25', { signal: AbortSignal.timeout(30000) });
    if (!res.ok) return;
    online = true;
    setBackendOnline(true);
    const data = await res.json();
    const events = data.events || [];
    broadcastState(data.state);
    broadcastPanelState();
    if (!events.length) return;
    // 审批/提问的挂起状态（悬浮球黄灯 + 提醒浮层的数据源）
    const pendingRelated = events.filter((ev) => ev.type === 'approval_request'
      || ev.type === 'question_request' || ev.type === 'approval_resolved'
      || ev.type === 'question_resolved');
    if (pendingRelated.length) {
      pendingRelated.forEach(trackPending);
      broadcastPending();
    }
    // 面板窗口（**含隐藏态**）能收就实时收：隐藏期间 DOM 与对话流保持一致，展开时
    // 不必依赖 /history 重建——在途的思考/工具调用还没写进 buffer，重建会把它们抹掉
    // （用户实测：生成中收/展面板，工具行和思考流全丢了）。audio 除外（按形态单播）。
    const panelAlive = panelLoaded && panelWin && !panelWin.isDestroyed();
    if (panelAlive) {
      const panelEvents = events.filter((ev) => ev.type !== 'audio');
      if (panelEvents.length) panelWin.webContents.send('events', panelEvents);
    } else {
      // 面板页还没加载完 → 暂存，morph-in-done 时补发；流式事件不缓存（迟到重放会重复渲染）
      pendingEvents.push(...events.filter(
        (ev) => ev.type !== 'text_stream' && ev.type !== 'thinking_stream'));
      if (pendingEvents.length > 200) pendingEvents.splice(0, pendingEvents.length - 200);
    }
    // 审批/提问请求：面板隐藏时**不强行展开**（2026-10-19 用户需求）——
    // 球旁冒提醒浮层 + 悬浮球黄灯，可在浮层里直接授权/选择；复杂授权选项
    // 点「展开面板」。面板已展开 → 置前（它不常驻最前，可能被别的窗口压住）。
    const needsPopup = events.some((ev) => ev.type === 'approval_request' || ev.type === 'question_request');
    if (needsPopup) {
      if (panelState === 'hidden') {
        appLog('[popup] 审批/提问请求 → 悬浮球提醒（不自动展开）');
        refreshPendingWindow();
      } else if (panelState === 'shown') {
        appLog('[popup] 审批/提问请求 → 面板置前');
        focusPanel();
      }
    } else if (pendingRelated.some(
      (ev) => ev.type === 'approval_resolved' || ev.type === 'question_resolved')) {
      refreshPendingWindow();
    }
    if (panelState === 'hidden') {
      // 悬浮球形态：文本事件冒气泡；audio 事件 → 气泡窗口播放；
      // text_stream 也转发（语音对话模式的流式 TTS 在悬浮球形态同样可用）
      for (const ev of events) {
        if (ev.type === 'text' && ev.content) {
          showToast(ev.content);
        } else if (ev.type === 'audio' && ev.url && bubbleWin && !bubbleWin.isDestroyed()) {
          bubbleWin.webContents.send('audio', ev.url);
        } else if (ev.type === 'text_stream' && ev.content && bubbleWin && !bubbleWin.isDestroyed()) {
          bubbleWin.webContents.send('events', [ev]);
        }
      }
    } else if (panelAlive) {
      // 面板展开：audio 事件 → 面板窗口播放（展开时也朗读）
      for (const ev of events) {
        if (ev.type === 'audio' && ev.url) {
          panelWin.webContents.send('audio', ev.url);
        }
      }
    }
  } catch {
    // 后端不可达 → 广播离线状态，各窗口状态点变红
    setBackendOnline(false);
    broadcastState({ offline: true });
    clearPendingAll();   // 挂起请求随后端不可达而失效（等待会超时）
  } finally {
    // 在线 → 立即发起下一次长轮询；离线 → 回到健康检查，等后端恢复后快速变绿
    if (!quitting) {
      dequeueTimer = setTimeout(online ? pollDequeue : checkBackend, online ? 0 : 1000);
    }
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

// 后端在线状态变化落日志（排查"半死实例"时对账：什么时候断的、断了多久）
let backendOnline = null;   // null = 启动初未知
function setBackendOnline(on) {
  if (backendOnline === on) return;
  backendOnline = on;
  appLog('[backend] ' + (on ? 'online' : 'OFFLINE'));
}

// 面板形态广播：语音连续对话模式的 active 窗口判定（bubble ↔ 悬浮球形态）
function broadcastPanelState() {
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) {
      bubbleWin.webContents.send('panel-state', panelState);
    }
    if (panelWin && !panelWin.isDestroyed()) {
      panelWin.webContents.send('panel-state', panelState);
    }
  } catch { /* 窗口销毁中忽略 */ }
}

function startDequeuePoll() {
  if (dequeueTimer) return;
  pollDequeue();   // 自调度长轮询：pollDequeue 完成后再 setTimeout 下一次
}

// 启动总入口：先做健康检查，确认在线后再进入长轮询
function startBackendMonitor() {
  if (dequeueTimer) return;
  checkBackend();
}

// ── IPC：渲染进程 ↔ 主进程 ──────────────────────────────────
// 主进程诊断日志：console 同时落盘（userData/app.log，无终端窗口也能排查）
const APP_LOG_FILE = path.join(app.getPath('userData'), 'app.log');
// 超过 1MB 归档为 app.old.log：避免无限增长，且排查时最多只面对两份文件
function rotateAppLogIfNeeded() {
  try {
    if (fs.existsSync(APP_LOG_FILE) && fs.statSync(APP_LOG_FILE).size > 1024 * 1024) {
      fs.renameSync(APP_LOG_FILE, path.join(path.dirname(APP_LOG_FILE), 'app.old.log'));
    }
  } catch { /* 忽略 */ }
}
function appLog(msg) {
  try {
    rotateAppLogIfNeeded();
    fs.appendFileSync(APP_LOG_FILE, `${new Date().toISOString()} ${msg}\n`);
  } catch { /* 忽略 */ }
  console.log(msg);
}

// 渲染进程日志（诊断播放链路）
ipcMain.on('renderer-log', (e, msg) => appLog('[renderer] ' + msg));

// ── 播放结果上报：连续失败只告警不自愈 ──────────────────────
// 渲染层每次 TTS 播放结束上报成功/失败；这里落盘并统计连续失败次数。
// 曾经考虑过连续失败自动 reload 窗口，按用户决策改为只诊断（人工重启兜底）。
let playFailStreak = 0;
ipcMain.on('playback-result', (e, r) => {
  const ok = !!(r && r.ok);
  if (ok) {
    if (playFailStreak >= 5) appLog('[playback] 连续失败 ' + playFailStreak + ' 次后恢复');
    playFailStreak = 0;
    return;
  }
  playFailStreak += 1;
  appLog('[playback] FAIL(' + playFailStreak + ') ' + String((r && r.detail) || '').slice(0, 200));
  if (playFailStreak === 5) {
    appLog('[playback] ⚠ 已连续失败 5 次——播放链路疑似异常，建议彻底关闭并重启小助');
  }
});

// ── 渲染进程 HTTP 代理 ────────────────────────────────
// Electron 37 起 file:// 页面直连 127.0.0.1 被 CORS/PNA 全拦（GET/POST 均 Failed to fetch），
// 渲染进程所有后端请求统一走主进程（无此限制）。
ipcMain.handle('api-fetch', async (e, reqPath, opts = {}) => {
  const url = BACKEND_URL + (reqPath || '');
  const res = await fetch(url, {
    method: opts.method || 'GET',
    headers: opts.headers || {},
    body: opts.body,
    signal: AbortSignal.timeout(120000),
  });
  const text = await res.text();
  return { status: res.status, ok: res.ok, text };
});

// 选择代码工作区目录（设置页「选择…」按钮 / 工作目录面板「更换工作区」）；取消返回空串
ipcMain.handle('pick-directory', async (event) => {
  try {
    const senderWin = BrowserWindow.fromWebContents(event.sender);
    const parent = (senderWin && !senderWin.isDestroyed()) ? senderWin
      : (panelWin && !panelWin.isDestroyed()) ? panelWin
        : (bubbleWin && !bubbleWin.isDestroyed()) ? bubbleWin : null;
    const r = await dialog.showOpenDialog(parent, {
      title: '选择代码工作区',
      properties: ['openDirectory'],
    });
    // Windows 已知坑：原生对话框关闭后键盘焦点可能不还给原窗口
    // （表现："按钮能点、输入框打不了字"）→ 显式把焦点还回去
    if (parent && !parent.isDestroyed()) {
      try {
        parent.focus();
        if (parent.webContents && !parent.webContents.isDestroyed()) {
          parent.webContents.focus();
        }
      } catch { /* 忽略 */ }
    }
    if (r.canceled || !r.filePaths || !r.filePaths.length) return '';
    return r.filePaths[0];
  } catch {
    return '';
  }
});

// 面板置前并聚焦（托盘/审批自动展开/前端焦点兜底共用）
ipcMain.on('focus-panel', () => focusPanel());

// 键盘焦点兜底：把焦点还给发起窗口（Windows 原生对话框/边缘情况会带走键盘焦点，
// 表现为"鼠标能点、打不了字"；webContents.focus() 是把键盘输入接回来的关键一步）
ipcMain.on('refocus-window', (event) => {
  const w = BrowserWindow.fromWebContents(event.sender);
  if (w && !w.isDestroyed()) {
    try {
      w.focus();
      if (w.webContents && !w.webContents.isDestroyed()) w.webContents.focus();
    } catch { /* 忽略 */ }
  }
});

// 外部链接（Markdown 里的 http/https/mailto）→ 系统默认浏览器。
// 渲染层是 file:// 页面，直接让 <a> 导航会把面板顶掉；协议白名单防 javascript:/data:。
ipcMain.handle('open-external', async (e, url) => {
  const u = String(url || '').trim();
  if (!/^(https?:\/\/|mailto:)/i.test(u)) return { ok: false, error: 'blocked_scheme' };
  try {
    await shell.openExternal(u);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
});

// 在系统文件管理器中打开目录（工作目录面板「打开文件夹」/「改动文件」块的「打开」）
ipcMain.handle('open-path', async (e, p) => {
  const target = String(p || '');
  if (!target) return { ok: false, error: 'empty' };
  try {
    // 先查存在性：shell.openPath 对不存在的路径会触发 Windows 系统报错框
    // （"Windows 找不到文件…"，用户/测试都踩过）——直接返回失败，由前端给提示
    if (!fs.existsSync(target)) return { ok: false, error: 'not_found' };
    const err = await shell.openPath(target);   // 成功返回空串，失败返回错误描述
    return err ? { ok: false, error: err } : { ok: true };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
});

// 在资源管理器中定位文件（选中高亮 + 打开所在目录；「改动文件」快捷块的「目录」按钮）
ipcMain.handle('show-in-folder', async (e, p) => {
  const target = String(p || '');
  if (!target) return { ok: false, error: 'empty' };
  try {
    if (fs.existsSync(target)) {
      shell.showItemInFolder(target);
      return { ok: true };
    }
    const dir = path.dirname(target);   // 文件已删除/移动 → 退而打开其所在目录
    if (fs.existsSync(dir)) {
      const err = await shell.openPath(dir);
      return err ? { ok: false, error: err } : { ok: true };
    }
    return { ok: false, error: 'not_found' };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
});

// 清理前端 TTS 缓存：只保留最近 7 天、最多 200 个文件 / 200MB
function cleanupTtsCache(maxAgeMs = 7 * 24 * 3600 * 1000, maxFiles = 200, maxBytes = 200 * 1024 * 1024) {
  try {
    const dir = path.join(app.getPath('userData'), 'tts_cache');
    if (!fs.existsSync(dir)) return;
    const files = fs.readdirSync(dir).map((name) => {
      const p = path.join(dir, name);
      try {
        const st = fs.statSync(p);
        return { p, mtimeMs: st.mtimeMs, size: st.size };
      } catch { return null; }
    }).filter(Boolean);
    const now = Date.now();
    let total = files.reduce((s, f) => s + f.size, 0);
    const expired = files.filter((f) => now - f.mtimeMs > maxAgeMs);
    for (const f of expired) {
      try { fs.unlinkSync(f.p); total -= f.size; } catch { /* 忽略 */ }
    }
    let remaining = files.filter((f) => now - f.mtimeMs <= maxAgeMs && fs.existsSync(f.p));
    remaining.sort((a, b) => a.mtimeMs - b.mtimeMs);   // 最旧在前
    while (remaining.length > maxFiles || total > maxBytes) {
      const f = remaining.shift();
      if (!f) break;
      try { fs.unlinkSync(f.p); total -= f.size; } catch { /* 忽略 */ }
    }
  } catch { /* 清理失败不影响播放 */ }
}

// 语音下载：主进程拉 wav/mp3 → 写 userData/tts_cache → 返回 file:// 路径（渲染直接播放）
ipcMain.handle('api-audio', async (e, reqPath) => {
  const name = String(reqPath || '').split('/').pop();
  if (!/^[0-9a-f]{32}\.(wav|mp3)$/.test(name)) return null;
  try {
    const res = await fetch(BACKEND_URL + '/tts/' + name, { signal: AbortSignal.timeout(60000) });
    if (!res.ok) return null;
    const buf = Buffer.from(await res.arrayBuffer());
    const dir = path.join(app.getPath('userData'), 'tts_cache');
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, name);
    fs.writeFileSync(file, buf);
    cleanupTtsCache();
    const fileUrl = 'file:///' + file.replace(/\\/g, '/');
    let from = '?';
    try { from = String(e.sender.getURL() || '').split('/').pop(); } catch { /* 忽略 */ }
    appLog('[tts] audioFile: ' + name + ' ' + buf.length + ' bytes <- ' + from);
    return fileUrl;
  } catch (err) { appLog('[tts] audioFile FAIL: ' + name + ' ' + err.message); return null; }
});

// 图片下载：主进程拉 /file?path=… → 写 userData/image_cache → 返回 file:// 路径（img 直接显示）
// （与音频同理：file:// 页面直连 http 媒体受 CORS/PNA 限制，统一走主进程落地文件）
ipcMain.handle('api-image', async (e, url) => {
  const u = String(url || '');
  if (!u.startsWith('/file?path=')) return null;
  try {
    const res = await fetch(BACKEND_URL + u, { signal: AbortSignal.timeout(30000) });
    if (!res.ok) return null;
    const buf = Buffer.from(await res.arrayBuffer());
    const extByType = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
                        'image/webp': '.webp', 'image/bmp': '.bmp' };
    const ctype = String(res.headers.get('content-type') || '').split(';')[0].trim();
    const ext = extByType[ctype] || '.png';
    const digest = crypto.createHash('sha1').update(u).digest('hex').slice(0, 16);
    const dir = path.join(app.getPath('userData'), 'image_cache');
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, digest + ext);
    fs.writeFileSync(file, buf);
    cleanupImageCache(dir);
    const fileUrl = 'file:///' + file.replace(/\\/g, '/');
    return fileUrl;
  } catch (err) { appLog('[image] api-image FAIL: ' + err.message); return null; }
});

// 图片缓存清理：按修改时间保留最新 100 张
function cleanupImageCache(dir) {
  try {
    const files = fs.readdirSync(dir)
      .map((n) => ({ p: path.join(dir, n), m: fs.statSync(path.join(dir, n)).mtimeMs }))
      .sort((a, b) => b.m - a.m);
    for (const f of files.slice(100)) {
      try { fs.unlinkSync(f.p); } catch { /* 忽略 */ }
    }
  } catch { /* 清理失败不影响显示 */ }
}

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
    if (quickWin && !quickWin.isDestroyed()) {
      quickWin.webContents.send('mounted-files', mountedFiles);
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

// ── 加号按钮：选文件挂载（与拖拽/截图同一套 mounted 机制）──────────
// 扩展名分类与 renderer/fileDrop.js 的 classify 保持一致
const _PICK_TEXT_EXT = new Set([
  'txt', 'md', 'markdown', 'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'csv', 'log',
  'ini', 'conf', 'cfg', 'yaml', 'yml', 'toml', 'html', 'htm', 'css', 'xml', 'sql',
  'sh', 'bat', 'ps1', 'c', 'cpp', 'h', 'hpp', 'java', 'go', 'rs', 'rb', 'php',
  'kt', 'swift', 'vue', 'svelte', 'gitignore', 'env', 'properties', 'reg',
]);
const _PICK_DOC_EXT = new Set(['pdf', 'docx', 'doc']);
const _PICK_IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'bmp', 'gif', 'webp', 'tiff', 'ico']);

function classifyPath(name) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  if (_PICK_IMAGE_EXT.has(ext)) return 'image';
  if (_PICK_DOC_EXT.has(ext)) return 'doc';
  if (_PICK_TEXT_EXT.has(ext)) return 'text';
  return 'other';
}

ipcMain.handle('pick-files', async (event) => {
  try {
    const senderWin = BrowserWindow.fromWebContents(event.sender);
    const parent = (senderWin && !senderWin.isDestroyed()) ? senderWin
      : (panelWin && !panelWin.isDestroyed()) ? panelWin
        : (bubbleWin && !bubbleWin.isDestroyed()) ? bubbleWin : null;
    const r = await dialog.showOpenDialog(parent, {
      title: '添加文件（随下一条消息一起发送）',
      properties: ['openFile', 'multiSelections'],
    });
    // Windows 原生对话框关闭后键盘焦点兜底（同 pick-directory）
    if (parent && !parent.isDestroyed()) {
      try { parent.focus(); } catch { /* 忽略 */ }
    }
    if (r.canceled || !r.filePaths.length) return [];
    return r.filePaths.map((p) => {
      const name = path.basename(p);
      const kind = classifyPath(name);
      let content = null;
      if (kind === 'text') {
        try {
          if (fs.statSync(p).size <= 32 * 1024) {   // 与 fileDrop 同口径
            const buf = fs.readFileSync(p);
            if (!buf.includes(0)) content = buf.toString('utf-8') || null;
          }
        } catch { content = null; }
      }
      return { name, path: p, kind, content };
    });
  } catch (err) {
    appLog('[pick-files] 失败: ' + (err && err.message));
    return [];
  }
});

// ── 输入框粘贴（Ctrl+V）：剪贴板位图无磁盘路径 → 落盘后挂载 ──────────
// 场景：微信/QQ/Snipaste/Win+Shift+S 截图后直接 Ctrl+V。从资源管理器复制的
// 文件有路径（fileDrop 原路走），只有"剪贴板位图"没有路径、必须主进程落盘。
const _PASTE_EXT = {
  'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
  'image/webp': '.webp', 'image/bmp': '.bmp', 'image/tiff': '.tiff',
};

function prunePasted(dir, keep = 50) {
  try {
    const files = fs.readdirSync(dir)
      .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
      .sort((a, b) => b.t - a.t);
    for (const { f } of files.slice(keep)) {
      try { fs.unlinkSync(path.join(dir, f)); } catch { /* 忽略 */ }
    }
  } catch { /* 忽略 */ }
}

ipcMain.handle('save-pasted-file', async (event, name, mime, buf) => {
  try {
    const dir = path.join(app.getPath('userData'), 'pasted');
    fs.mkdirSync(dir, { recursive: true });
    const ext = _PASTE_EXT[String(mime || '').toLowerCase()]
      || (path.extname(String(name || '')) || '.png');
    const base = `paste-${Date.now()}${ext}`;
    const dest = path.join(dir, base);
    fs.writeFileSync(dest, Buffer.from(buf));
    prunePasted(dir);
    appLog(`[paste] 剪贴板文件已落盘: ${dest}`);
    return { path: dest, name: String(name || '') || base };
  } catch (err) {
    appLog('[paste] 落盘失败: ' + (err && err.message));
    return null;
  }
});

// ── 截屏（悬浮球右键菜单）：覆盖层选区/涂鸦 → 挂载为待发送文件 ──────
// 流程：藏气泡 → 截取光标所在屏幕 → 覆盖层页面选区（✓/✗/画笔）→
// ✓ 后**不直接发送**：挂进 mountedFiles（面板在输入框上方显示；悬浮球
// 长按语音发送时也会带上——与拖拽文件完全同一套机制）。
let screenshotWin = null;
let pendingScreenshot = null;   // { dataUrl, scale }

function pruneScreenshots(dir, keep = 50) {
  try {
    const files = fs.readdirSync(dir).filter((f) => f.endsWith('.png'))
      .map((f) => ({ f, t: fs.statSync(path.join(dir, f)).mtimeMs }))
      .sort((a, b) => b.t - a.t);
    for (const { f } of files.slice(keep)) {
      try { fs.unlinkSync(path.join(dir, f)); } catch { /* 忽略 */ }
    }
  } catch { /* 忽略 */ }
}

async function startScreenshot() {
  // 残影窗口清理：上次覆盖层没能显示出来（事件竞态/加载失败）时窗口对象还在，
  // 直接 return 会让"再点没反应"（用户实测过）——不可见的残留窗口清掉重来
  if (screenshotWin && !screenshotWin.isDestroyed()) {
    if (screenshotWin.isVisible()) return;
    try { screenshotWin.destroy(); } catch { /* 忽略 */ }
    screenshotWin = null;
  }
  const cursor = screen.getCursorScreenPoint();
  const disp = screen.getDisplayNearestPoint(cursor);
  try {
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: {
        width: Math.round(disp.bounds.width * disp.scaleFactor),
        height: Math.round(disp.bounds.height * disp.scaleFactor),
      },
    });
    const src = sources.find((s) => String(s.display_id) === String(disp.id)) || sources[0];
    if (!src || src.thumbnail.isEmpty()) throw new Error('empty thumbnail');
    pendingScreenshot = {
      dataUrl: src.thumbnail.toDataURL(),
      scale: src.thumbnail.getSize().width / disp.bounds.width,
    };
    appLog(`[screenshot] 捕获成功 ${disp.bounds.width}x${disp.bounds.height} `
      + `scale=${pendingScreenshot.scale.toFixed(2)}`);
  } catch (err) {
    appLog('[screenshot] 截屏失败: ' + (err && err.message));
    showToast('截屏失败：拿不到屏幕画面');
    return;
  }
  if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.hide();   // 别把自己截进去
  clearAllToasts();
  try {
    const win = new BrowserWindow({
      x: disp.bounds.x, y: disp.bounds.y,
      width: disp.bounds.width, height: disp.bounds.height,
      frame: false, resizable: false, movable: false,
      minimizable: false, maximizable: false, fullscreenable: false,
      skipTaskbar: true, alwaysOnTop: true, hasShadow: false,
      enableLargerThanScreen: true, show: false,
      backgroundColor: '#000000',
      webPreferences: { preload: path.join(__dirname, 'preload.js') },
    });
    screenshotWin = win;
    win.setAlwaysOnTop(true, 'screen-saver');
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    // 显示兜底：**先挂监听再 loadFile**——本地小页面加载极快时 ready-to-show
    // 可能抢在监听器挂上之前发出（用户实测：悬浮球消失但覆盖层没出来），
    // 所以 ready-to-show / did-finish-load / 定时器三重触发，任一生效即可。
    const reveal = () => {
      if (win.isDestroyed()) return;
      if (!win.isVisible()) win.show();
      try { win.focus(); } catch { /* 忽略 */ }
    };
    win.once('ready-to-show', reveal);
    win.webContents.once('did-finish-load', reveal);
    win.webContents.on('did-fail-load', (e, code, desc) => {
      appLog(`[screenshot] 覆盖层页面加载失败: ${code} ${desc}`);
      showToast('截屏页面加载失败');
      if (!win.isDestroyed()) win.close();
    });
    win.once('closed', () => {
      if (screenshotWin === win) screenshotWin = null;
      if (panelState === 'hidden') showBubble();   // 截屏期间藏了气泡：恢复
    });
    win.loadFile(path.join(__dirname, 'renderer', 'screenshot.html'));
    setTimeout(reveal, 600);   // 兜底：任何事件都没来也必须显示
  } catch (err) {
    appLog('[screenshot] 覆盖层创建失败: ' + (err && err.message));
    showToast('截屏失败：无法创建截屏窗口');
    screenshotWin = null;
    if (panelState === 'hidden') showBubble();
  }
}

ipcMain.on('start-screenshot', () => startScreenshot());
ipcMain.handle('screenshot-get', () => pendingScreenshot);
ipcMain.on('screenshot-cancel', () => {
  pendingScreenshot = null;
  if (screenshotWin && !screenshotWin.isDestroyed()) screenshotWin.close();
});
ipcMain.on('screenshot-done', (e, dataUrl) => {
  pendingScreenshot = null;
  try {
    const m = /^data:image\/png;base64,(.+)$/.exec(String(dataUrl || ''));
    if (!m) throw new Error('bad data url');
    const dir = path.join(app.getPath('userData'), 'screenshots');
    fs.mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString().replace(/\D/g, '').slice(0, 17);
    const file = path.join(dir, `screenshot-${stamp}.png`);
    fs.writeFileSync(file, Buffer.from(m[1], 'base64'));
    pruneScreenshots(dir);
    mountedFiles = mountedFiles.concat([{
      name: `截图-${stamp}.png`, path: file, kind: 'image', content: null,
    }]);
    broadcastMounted();
    showToast('截图已挂载：发消息或长按语音时一起带上');
  } catch (err) {
    appLog('[screenshot] 保存失败: ' + (err && err.message));
    showToast('截图保存失败');
  }
  if (screenshotWin && !screenshotWin.isDestroyed()) screenshotWin.close();
});

// ── 前端 UI 设置（长按时间等，纯前端参数不走后端，启动即用）──
const UI_SETTINGS_FILE = () => path.join(app.getPath('userData'), 'ui-settings.json');
let uiSettings = { press_ms: 200, toast_seconds: 30, quick_input_hotkey: 'Alt+J' };
(function initUiSettings() {
  try {
    // 优先读本地 ui-settings.json（持久化）
    if (fs.existsSync(UI_SETTINGS_FILE())) {
      const raw = JSON.parse(fs.readFileSync(UI_SETTINGS_FILE(), 'utf-8'));
      if (raw && typeof raw.press_ms === 'number') uiSettings.press_ms = raw.press_ms;
      // voice_mode 会随开关保存，但这里必须加载——否则每次重启语音模式都被重置为关
      if (raw && typeof raw.voice_mode === 'boolean') uiSettings.voice_mode = raw.voice_mode;
      // 面板尺寸（拖拽边缘调整后持久化）
      if (raw && typeof raw.panel_w === 'number') {
        uiSettings.panel_w = raw.panel_w;
        panelW = Math.max(PANEL_MIN, Math.round(raw.panel_w));
      }
      if (raw && typeof raw.panel_h === 'number') {
        uiSettings.panel_h = raw.panel_h;
        panelH = Math.max(PANEL_MIN, Math.round(raw.panel_h));
      }
      return;
    }
    // 首次：从后端 settings.json 迁移 press_ms（后端旧数据）
    const dataRoot = process.env.PLANNER_DATA_ROOT;
    if (dataRoot) {
      const p = path.join(dataRoot, 'settings.json');
      if (fs.existsSync(p)) {
        const raw = JSON.parse(fs.readFileSync(p, 'utf-8'));
      if (raw && typeof raw.press_ms === 'number') uiSettings.press_ms = raw.press_ms;
      // 气泡显示时长（秒）与快速输入快捷键（悬浮球模式全局热键）
      if (raw && typeof raw.toast_seconds === 'number') {
        uiSettings.toast_seconds = Math.max(3, Math.min(600, Math.round(raw.toast_seconds)));
      }
      if (raw && typeof raw.quick_input_hotkey === 'string' && raw.quick_input_hotkey.trim()) {
        uiSettings.quick_input_hotkey = raw.quick_input_hotkey.trim();
      }
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
  if (typeof u.toast_seconds === 'number') {
    uiSettings.toast_seconds = Math.max(3, Math.min(600, Math.round(u.toast_seconds)));
  }
  if (typeof u.quick_input_hotkey === 'string') {
    const v = u.quick_input_hotkey.trim() || 'Alt+J';
    if (v !== (uiSettings.quick_input_hotkey || '')) {
      uiSettings.quick_input_hotkey = v;
      registerQuickInputShortcut();
    }
  }
  saveUiSettingsFile();
  broadcastUiSettings();
});
// 面板尺寸：程序化设置（测试/恢复默认用）；拖拽调整走 panel-resize-start/end
ipcMain.handle('panel-size-get', () => ({ w: panelW, h: panelH }));
ipcMain.on('panel-size-set', (e, size) => {
  const w = Math.max(PANEL_MIN, Math.round(Number(size && size.w) || panelW));
  const h = Math.max(PANEL_MIN, Math.round(Number(size && size.h) || panelH));
  panelW = w;
  panelH = h;
  uiSettings.panel_w = w;
  uiSettings.panel_h = h;
  saveUiSettingsFile();
  applyPanelBoundsKeepAnchor();
});

// 拖拽边缘调整大小（透明窗口不支持原生 resize → 渲染层报边缘，主进程跟光标调尺寸）
let resizeState = null;

function clampPanelSize(w, h, disp) {
  return {
    w: Math.max(PANEL_MIN, Math.min(w, disp.width - 8)),
    h: Math.max(PANEL_MIN, Math.min(h, disp.height - 8)),
  };
}

function applyPanelBoundsKeepAnchor() {
  if (!panelWin || panelWin.isDestroyed() || panelState !== 'shown') return;
  try {
    const b = panelWin.getBounds();
    const disp = screen.getDisplayNearestPoint({ x: b.x + 10, y: b.y + 10 }).workArea;
    const size = clampPanelSize(panelW, panelH, disp);
    const x = Math.max(disp.x + 4, Math.min(b.x, disp.x + disp.width - size.w - 4));
    const y = Math.max(disp.y + 4, Math.min(b.y, disp.y + disp.height - size.h - 4));
    panelWin.setBounds({ x: Math.round(x), y: Math.round(y), width: size.w, height: size.h });
  } catch { /* 窗口销毁中忽略 */ }
}

function stopPanelResize(save = true) {
  if (!resizeState) return;
  clearInterval(resizeState.timer);
  resizeState = null;
  if (save) {
    uiSettings.panel_w = panelW;
    uiSettings.panel_h = panelH;
    saveUiSettingsFile();
    appLog('[panel] resized to ' + panelW + 'x' + panelH);
  }
}

ipcMain.on('panel-resize-start', (e, edge) => {
  if (!panelWin || panelWin.isDestroyed() || panelState !== 'shown') return;
  stopPanelResize(false);
  const b = panelWin.getBounds();
  const cur = screen.getCursorScreenPoint();
  resizeState = {
    edge: String(edge || 'se').toLowerCase(),
    sx: cur.x, sy: cur.y, b,
    startedAt: Date.now(),
    lastCur: cur, lastMoveAt: Date.now(),
    timer: null,
  };
  appLog('[panel] resize start edge=' + resizeState.edge);
  resizeState.timer = setInterval(() => {
    const st = resizeState;
    if (!st || !panelWin || panelWin.isDestroyed() || panelState !== 'shown') {
      stopPanelResize(true);
      return;
    }
    const cur2 = screen.getCursorScreenPoint();
    if (cur2.x !== st.lastCur.x || cur2.y !== st.lastCur.y) {
      st.lastCur = cur2;
      st.lastMoveAt = Date.now();
    }
    // 兜底：光标 2 秒没动（或拖了 30 秒）→ 视为松手，收尾
    if (Date.now() - st.lastMoveAt > 2000 || Date.now() - st.startedAt > 30000) {
      stopPanelResize(true);
      return;
    }
    const disp = screen.getDisplayNearestPoint({ x: st.b.x + 20, y: st.b.y + 20 }).workArea;
    const dx = cur2.x - st.sx;
    const dy = cur2.y - st.sy;
    const east = st.edge.includes('e');
    const west = st.edge.includes('w');
    const south = st.edge.includes('s');
    const north = st.edge.includes('n');
    const right = st.b.x + st.b.width;
    const bottom = st.b.y + st.b.height;
    let w = st.b.width;
    let h = st.b.height;
    if (east || west) w = Math.max(PANEL_MIN, st.b.width + (east ? dx : -dx));
    if (north || south) h = Math.max(PANEL_MIN, st.b.height + (south ? dy : -dy));
    if (east) w = Math.min(w, disp.x + disp.width - 4 - st.b.x);
    if (south) h = Math.min(h, disp.y + disp.height - 4 - st.b.y);
    if (west) w = Math.min(w, right - disp.x - 4);
    if (north) h = Math.min(h, bottom - disp.y - 4);
    w = Math.max(PANEL_MIN, w);
    h = Math.max(PANEL_MIN, h);
    const x = west ? right - w : st.b.x;
    const y = north ? bottom - h : st.b.y;
    panelW = Math.round(w);
    panelH = Math.round(h);
    try {
      panelWin.setBounds({ x: Math.round(x), y: Math.round(y), width: panelW, height: panelH });
    } catch { /* 忽略 */ }
  }, 16);
});

ipcMain.on('panel-resize-end', () => stopPanelResize(true));
// ── 语音连续对话模式：主进程统一开关（菜单/按钮 → 持久化 → 双窗口广播）──
function broadcastVoiceMode() {
  const on = !!uiSettings.voice_mode;
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.webContents.send('voice-mode', on);
    if (panelWin && !panelWin.isDestroyed()) panelWin.webContents.send('voice-mode', on);
  } catch { /* 窗口销毁中忽略 */ }
}
function toggleVoiceMode() {
  uiSettings.voice_mode = !uiSettings.voice_mode;
  appLog('[voice-mode] ' + (uiSettings.voice_mode ? 'ON' : 'off'));
  saveUiSettingsFile();
  broadcastVoiceMode();
}
ipcMain.on('voice-mode-set', (e, on) => {
  uiSettings.voice_mode = !!on;
  appLog('[voice-mode] ' + (uiSettings.voice_mode ? 'ON' : 'off'));
  saveUiSettingsFile();
  broadcastVoiceMode();
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
  await fetchSettingsAndBroadcast();
});
function broadcastSettings(settings) {
  try {
    if (bubbleWin && !bubbleWin.isDestroyed()) bubbleWin.webContents.send('settings', settings);
    if (panelWin && !panelWin.isDestroyed()) panelWin.webContents.send('settings', settings);
  } catch { /* 忽略 */ }
}
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
    panelDragged = true;   // 拖过面板（球位置不受影响，见 morphOut）
  }
});
ipcMain.on('morph-in-done', () => {
  // 陈旧事件防护：只在真正 morphing_in 时处理（曾因 renderer 兜底定时器陈旧触发，
  // "out 完成"事件把刚展开的面板又隐藏——现已在 morph.js 根治，这里再兜一层）
  if (panelState !== 'morphing_in') {
    appLog('[morph] 忽略陈旧的 morph-in-done（state=' + panelState + '）');
    return;
  }
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
  // 陈旧事件防护：只在真正 morphing_out 时处理（见 morph-in-done 同款注释）
  if (panelState !== 'morphing_out') {
    appLog('[morph] 忽略陈旧的 morph-out-done（state=' + panelState + '）');
    return;
  }
  clearMorphTimeout();
  panelState = 'hidden';
  if (panelWin && !panelWin.isDestroyed()) panelWin.hide();
  showBubble();
  refreshPendingWindow();   // 收起后仍有未处理的审批/提问 → 回到球旁提醒
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
    { label: '截屏', click: () => startScreenshot() },
    { label: '语音对话模式', type: 'checkbox', checked: !!uiSettings.voice_mode,
      click: () => toggleVoiceMode() },
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
  items.push({
    label: '显示最近的气泡',
    click: () => {
      if (bubbleWin && !bubbleWin.isDestroyed()) {
        bubbleWin.webContents.send('show-reply');
      }
    },
  });
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
    // 启动横幅：日志里区分不同实例的边界（排查"半死实例"时对账用）
    appLog(`[boot] pid=${process.pid} v=${app.getVersion() || '?'} electron=${process.versions.electron} `
      + `userData=${app.getPath('userData')} backend=${BACKEND_URL}`);
    // 麦克风权限：语音输入（getUserMedia）放行
    session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => {
      callback(permission === 'media');
    });
    createBubble();
    createQuickInput();          // 快速输入浮层：提前创建（隐藏），热键按下即弹
    createPendingWindow();       // 审批/提问提醒浮层：提前创建（隐藏）
    registerQuickInputShortcut();
    createTray();
    cleanupTtsCache(); // 启动时清理旧 TTS 缓存
    ensureBackend(); // 后端不在线则自动拉起
    startBackendMonitor(); // 先健康检查，确认在线后再进入 /dequeue 长轮询
    // 打开即展开（用户设定）：默认以大窗启动（仍可点「—」变形回悬浮球）。
    // 稍延迟：让球先就位（变形动画的起点）、面板页有时间加载。
    setTimeout(() => {
      if (!quitting && panelState === 'hidden') {
        showBubble();      // 确保球可见（动画起点）
        morphIn();
      }
    }, 500);
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
  try { globalShortcut.unregisterAll(); } catch { /* 忽略 */ }
});





