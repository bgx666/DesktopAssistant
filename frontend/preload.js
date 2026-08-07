// preload.js —— 渲染进程桥（悬浮球 + 变形面板共用）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('planner', {
  platform: process.platform,
  // 后端地址（release 版经 PLANNER_URL 指向独立端口）
  apiBase: process.env.PLANNER_URL || 'http://127.0.0.1:18771',
  // 悬浮球
  togglePanel: () => ipcRenderer.send('toggle-panel'),
  bubbleNudge: () => ipcRenderer.send('bubble-nudge'),
  bubbleMenu: () => ipcRenderer.send('bubble-menu'),
  setTyping: (typing) => ipcRenderer.send('typing-state', typing),
  moveBubble: (x, y) => ipcRenderer.send('move-bubble', x, y),
  getBubblePos: () => ipcRenderer.invoke('get-bubble-pos'),
  getPanelPos: () => ipcRenderer.invoke('get-panel-pos'),
  getPanelState: () => ipcRenderer.invoke('get-panel-state'),
  // 气泡窗
  showToast: (text) => ipcRenderer.send('toast-show', text),
  onToastText: (cb) => ipcRenderer.on('toast-text', (e, data) => cb(data)),
  toastClick: () => ipcRenderer.send('toast-click'),
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
  quitApp: () => ipcRenderer.send('quit-app'),
});
