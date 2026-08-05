// preload.js —— 渲染进程桥（悬浮球 + 变形面板共用）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('planner', {
  platform: process.platform,
  // 悬浮球
  togglePanel: () => ipcRenderer.send('toggle-panel'),
  bubbleMenu: () => ipcRenderer.send('bubble-menu'),
  moveBubble: (x, y) => ipcRenderer.send('move-bubble', x, y),
  getBubblePos: () => ipcRenderer.invoke('get-bubble-pos'),
  // 变形面板
  setPanelBounds: (b) => ipcRenderer.send('set-panel-bounds', b),
  onMorphIn: (cb) => ipcRenderer.on('morph-in', (e, data) => cb(data)),
  onMorphOut: (cb) => ipcRenderer.on('morph-out', (e, data) => cb(data)),
  onMorphForceFinish: (cb) => ipcRenderer.on('morph-force-finish', (e, kind) => cb(kind)),
  morphDone: (kind) => ipcRenderer.send(kind === 'in' ? 'morph-in-done' : 'morph-out-done'),
  hidePanel: () => ipcRenderer.send('hide-panel'),
});
