// bubble.js —— 悬浮球：纯 JS 拖拽 + 点击弹面板 + 右键菜单 + 轮询后端状态
(() => {
  'use strict';

  const API = 'http://127.0.0.1:18771';
  const core = document.getElementById('core');

  // ── 手动拖拽（不用 -webkit-app-region，透明窗口上它吞点击事件）──
  // mousedown 记录起点 → mousemove 移动窗口 → mouseup 时位移 < 6px 视为点击
  let dragging = false;
  let startX = 0, startY = 0;
  let winX = 0, winY = 0;
  let lastMoveX = 0, lastMoveY = 0;

  core.addEventListener('mousedown', async (e) => {
    if (e.button !== 0) return;
    dragging = true;
    startX = e.screenX;
    startY = e.screenY;
    lastMoveX = 0;
    lastMoveY = 0;
    const pos = await window.planner.getBubblePos();
    winX = pos.x;
    winY = pos.y;
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.screenX - startX;
    const dy = e.screenY - startY;
    if (Math.abs(dx - lastMoveX) < 2 && Math.abs(dy - lastMoveY) < 2) return;
    lastMoveX = dx;
    lastMoveY = dy;
    window.planner.moveBubble(winX + dx, winY + dy);
  });

  document.addEventListener('mouseup', (e) => {
    if (!dragging) return;
    dragging = false;
    const dx = e.screenX - startX;
    const dy = e.screenY - startY;
    if (Math.abs(dx) < 6 && Math.abs(dy) < 6) {
      window.planner.togglePanel(); // 原地点击 → 弹出/收起面板
    }
  });

  // 右键 → 菜单（打开小助 / 切换免打扰 / 退出）
  core.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    window.planner.bubbleMenu();
  });

  // 轮询后端状态：思考中脉冲 / 离线红点 / 逾期徽标
  async function pollState() {
    const dot = document.getElementById('status-dot');
    const badge = document.getElementById('badge');
    try {
      const data = await (await fetch(API + '/state')).json();
      const s = data.state || {};
      dot.classList.toggle('thinking', !!s.thinking);
      dot.classList.remove('offline');
      const overdue = (s.plan && s.plan.overdue_count) || 0;
      badge.classList.toggle('hidden', !overdue);
      badge.textContent = overdue > 9 ? '9+' : overdue;
    } catch {
      dot.classList.remove('thinking');
      dot.classList.add('offline');
    }
  }

  pollState();
  setInterval(pollState, 3000);
})();
