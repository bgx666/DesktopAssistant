// preload.js —— 渲染进程桥（悬浮球 + 变形面板共用）
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('planner', {
  platform: process.platform,
  // 后端地址（release 版经 PLANNER_URL 指向独立端口）
  apiBase: process.env.PLANNER_URL || 'http://127.0.0.1:18771',
  // HTTP 代理：file:// 页面直连被 CORS/PNA 拦，全部走主进程转发
  apiFetch: (path, opts) => ipcRenderer.invoke('api-fetch', path, opts || {}),
  // 选择目录（代码工作区）
  pickDirectory: () => ipcRenderer.invoke('pick-directory'),
  // 在系统文件管理器中打开目录
  openInExplorer: (p) => ipcRenderer.invoke('open-path', String(p || '')),
  // 在资源管理器中定位文件（选中 + 打开所在目录；「改动文件」快捷块用）
  showInFolder: (p) => ipcRenderer.invoke('show-in-folder', String(p || '')),
  // 外部链接（Markdown 里的 http/https/mailto）：交给系统默认浏览器
  openExternal: (url) => ipcRenderer.invoke('open-external', String(url || '')),
  // 面板置前 + 聚焦（Windows 键盘焦点被抢走时的兜底）
  focusPanel: () => ipcRenderer.send('focus-panel'),
  // 把键盘焦点还给本窗口（原生对话框后遗症兜底；双窗口共用）
  refocusWindow: () => ipcRenderer.send('refocus-window'),
  // 面板尺寸：get 当前值 / set 程序化调整（拖拽调整走 beginResize/endResize）
  getPanelSize: () => ipcRenderer.invoke('panel-size-get'),
  setPanelSize: (w, h) => ipcRenderer.send('panel-size-set', { w, h }),
  beginResize: (edge) => ipcRenderer.send('panel-resize-start', edge),
  endResize: () => ipcRenderer.send('panel-resize-end'),
  // 语音下载代理：/tts/x.wav → 本地 file:// 路径（audio 元素直接播放）
  audioFile: (path) => ipcRenderer.invoke('api-audio', path),
  // 图片下载代理：/file?path=… → 本地 file:// 路径（img 元素直接显示）
  imageFile: (url) => ipcRenderer.invoke('api-image', url),
  // 渲染进程日志上报（播放链路诊断，输出到 start.bat 窗口）
  reportLog: (msg) => ipcRenderer.send('renderer-log', msg),
  // TTS 播放结果上报：主进程统计连续失败（只告警不自动恢复）
  reportPlaybackResult: (ok, detail) => ipcRenderer.send('playback-result', { ok: !!ok, detail: String(detail || '') }),
  // 拖拽文件：取本地绝对路径（Electron 37：File.path 已移除，用 webUtils）
  getPathForFile: (file) => {
    try { return webUtils.getPathForFile(file); } catch { return ''; }
  },
  // 面板语音输入：把"正在录音"同步给悬浮球（跨窗口特效）
  setPanelRecording: (on) => ipcRenderer.send('panel-recording', !!on),
  onPanelRecording: (cb) => ipcRenderer.on('panel-recording', (e, on) => cb(on)),
  // 全局挂载文件（主进程持有，两窗口共享）
  mountFiles: (files) => ipcRenderer.send('mount-files', files),
  clearMounted: () => ipcRenderer.send('clear-mounted'),
  removeMounted: (index) => ipcRenderer.send('remove-mounted', index),
  getMounted: () => ipcRenderer.invoke('get-mounted'),
  onMountedChanged: (cb) => ipcRenderer.on('mounted-files', (e, list) => cb(list)),
  // 加号按钮：系统文件选择器 → 挂载（返回 [{name, path, kind, content?}]）
  pickFiles: () => ipcRenderer.invoke('pick-files'),
  // 输入框粘贴：剪贴板位图（无磁盘路径）→ 主进程落盘 userData/pasted/ → {path, name}
  savePastedFile: (name, mime, buf) => ipcRenderer.invoke('save-pasted-file', name, mime, buf),
  // 截屏（悬浮球右键菜单触发；截图覆盖层页面用 get/done/cancel 回传）
  startScreenshot: () => ipcRenderer.send('start-screenshot'),
  screenshotGet: () => ipcRenderer.invoke('screenshot-get'),
  screenshotDone: (dataUrl) => ipcRenderer.send('screenshot-done', String(dataUrl || '')),
  screenshotCancel: () => ipcRenderer.send('screenshot-cancel'),
  // 前端 UI 设置（长按时间等，主进程本地文件，无需后端）
  getUiSettings: () => ipcRenderer.invoke('get-ui-settings'),
  saveUiSettings: (updates) => ipcRenderer.send('save-ui-settings', updates),
  onUiSettings: (cb) => ipcRenderer.on('ui-settings', (e, s) => cb(s)),
  onSettings: (cb) => ipcRenderer.on('settings', (e, s) => cb(s)),
  settingsSaved: () => ipcRenderer.send('settings-saved'),
  // 语音连续对话模式（主进程统一持有开关，双窗口广播）
  setVoiceMode: (on) => ipcRenderer.send('voice-mode-set', !!on),
  onVoiceMode: (cb) => ipcRenderer.on('voice-mode', (e, on) => cb(on)),
  // 快速输入浮层（悬浮球模式全局快捷键弹出）
  onQuickShow: (cb) => ipcRenderer.on('quick-show', () => cb()),
  onQuickHide: (cb) => ipcRenderer.on('quick-hide', () => cb()),
  hideQuickInput: () => ipcRenderer.send('quick-hide'),
  quickResize: (h) => ipcRenderer.send('quick-resize', h),
  // 待处理请求（审批/提问）：球旁提醒浮层（悬浮球形态）
  onPending: (cb) => ipcRenderer.on('pending', (e, p) => cb(p)),
  getPending: () => ipcRenderer.invoke('get-pending'),
  pendingResize: (h) => ipcRenderer.send('pending-resize', h),
  closePending: () => ipcRenderer.send('pending-close'),
  expandPending: () => ipcRenderer.send('pending-expand'),
  // 面板形态广播（hidden/shown/morphing_*）：语音模式 active 窗口判定用
  onPanelState: (cb) => ipcRenderer.on('panel-state', (e, s) => cb(s)),
  // 悬浮球
  togglePanel: () => ipcRenderer.send('toggle-panel'),
  bubbleMenu: () => ipcRenderer.send('bubble-menu'),
  moveBubble: (x, y) => ipcRenderer.send('move-bubble', x, y),
  getBubblePos: () => ipcRenderer.invoke('get-bubble-pos'),
  getPanelPos: () => ipcRenderer.invoke('get-panel-pos'),
  getPanelState: () => ipcRenderer.invoke('get-panel-state'),
  // 气泡窗
  showToast: (text, offset, history) => ipcRenderer.send('toast-show', { text, offset: offset || 0, history: !!history }),
  onToastText: (cb) => ipcRenderer.on('toast-text', (e, data) => cb(data)),
  toastClick: () => ipcRenderer.send('toast-click'),
  toastResize: (h) => ipcRenderer.send('toast-resize', h),
  onToastsCleared: (cb) => ipcRenderer.on('toasts-cleared', () => cb()),
  onShowReply: (cb) => ipcRenderer.on('show-reply', () => cb()),
  onAudio: (cb) => ipcRenderer.on('audio', (e, url) => cb(url)),
  // 变形面板
  setPanelBounds: (b) => ipcRenderer.send('set-panel-bounds', b),
  movePanel: (x, y) => ipcRenderer.send('move-panel', x, y),
  onMorphIn: (cb) => ipcRenderer.on('morph-in', (e, data) => cb(data)),
  onMorphOut: (cb) => ipcRenderer.on('morph-out', (e, data) => cb(data)),
  onMorphForceFinish: (cb) => ipcRenderer.on('morph-force-finish', (e, kind) => cb(kind)),
  onPanelShown: (cb) => ipcRenderer.on('panel-shown', () => cb()),
  onEvents: (cb) => ipcRenderer.on('events', (e, list) => cb(list)),
  onState: (cb) => ipcRenderer.on('state', (e, s) => cb(s)),
  morphDone: (kind) => ipcRenderer.send(kind === 'in' ? 'morph-in-done' : 'morph-out-done'),
  hidePanel: () => ipcRenderer.send('hide-panel'),
  openSettings: () => ipcRenderer.send('open-settings'),
  quitApp: () => ipcRenderer.send('quit-app'),
});
