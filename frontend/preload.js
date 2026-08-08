// preload.js —— 渲染进程桥（悬浮球 + 变形面板共用）
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('planner', {
  platform: process.platform,
  // 后端地址（release 版经 PLANNER_URL 指向独立端口）
  apiBase: process.env.PLANNER_URL || 'http://127.0.0.1:18771',
  // 拖拽文件：取本地绝对路径（Electron 37：File.path 已移除，用 webUtils）
  getPathForFile: (file) => {
    try { return webUtils.getPathForFile(file); } catch { return ''; }
  },
  // 全局挂载文件（主进程持有，两窗口共享）
  mountFiles: (files) => ipcRenderer.send('mount-files', files),
  clearMounted: () => ipcRenderer.send('clear-mounted'),
  removeMounted: (index) => ipcRenderer.send('remove-mounted', index),
  getMounted: () => ipcRenderer.invoke('get-mounted'),
  onMountedChanged: (cb) => ipcRenderer.on('mounted-files', (e, list) => cb(list)),
  // 前端 UI 设置（长按时间等，主进程本地文件，无需后端）
  getUiSettings: () => ipcRenderer.invoke('get-ui-settings'),
  saveUiSettings: (updates) => ipcRenderer.send('save-ui-settings', updates),
  onUiSettings: (cb) => ipcRenderer.on('ui-settings', (e, s) => cb(s)),
  onSettings: (cb) => ipcRenderer.on('settings', (e, s) => cb(s)),
  settingsSaved: () => ipcRenderer.send('settings-saved'),
  // 悬浮球
  togglePanel: () => ipcRenderer.send('toggle-panel'),
  bubbleMenu: () => ipcRenderer.send('bubble-menu'),
  setTyping: (typing) => ipcRenderer.send('typing-state', typing),
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
