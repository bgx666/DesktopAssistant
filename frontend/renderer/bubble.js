// bubble.js —— 悬浮球：纯 JS 拖拽 + 单击说话/双击弹面板 + 右键菜单（状态由主进程推送）
(() => {
  'use strict';

  const core = document.getElementById('core');

  // ── 手动拖拽（不用 -webkit-app-region，透明窗口上它吞点击事件）──
  // mousedown 记录起点 → mousemove 移动窗口 → mouseup 时位移 < 6px 视为点击
  let dragging = false;
  let startX = 0, startY = 0;
  let winX = 0, winY = 0;
  let lastMoveX = 0, lastMoveY = 0;
  let clickTimer = null;   // 单击延迟 300ms 等双击：双击时不触发"说话"

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
    if (Math.abs(dx) >= 6 || Math.abs(dy) >= 6) return;
    // 原地点击：先等 300ms 看是否构成双击
    // （双击的第一击也走这里：timer 已存在 → 清掉等待第二击，不触发说话）
    if (clickTimer) {
      clearTimeout(clickTimer);
      clickTimer = null;
      return;
    }
    clickTimer = setTimeout(() => {
      clickTimer = null;
      window.planner.bubbleNudge(); // 单击 → 让 AI 主动说一句（气泡显示）
    }, 300);
  });

  // 双击 → 放大窗口（变形展开面板）
  core.addEventListener('dblclick', (e) => {
    if (clickTimer) {
      clearTimeout(clickTimer);
      clickTimer = null;
    }
    window.planner.togglePanel();
  });

  // 右键 → 菜单（打开小助 / 切换免打扰 / 退出）
  core.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    window.planner.bubbleMenu();
  });

  // 状态点（思考中脉冲 / 离线红点 / 逾期徽标）由主进程统一推送
  // （主进程 /dequeue 轮询带 state 广播），本窗口不再各自轮询 /state。
  function applyState(s) {
    const dot = document.getElementById('status-dot');
    const badge = document.getElementById('badge');
    if (!s) return;
    if (s.offline) {
      dot.classList.remove('thinking');
      dot.classList.add('offline');
      return;
    }
    dot.classList.toggle('thinking', !!s.thinking);
    dot.classList.remove('offline');
    const overdue = (s.plan && s.plan.overdue_count) || 0;
    badge.classList.toggle('hidden', !overdue);
    badge.textContent = overdue > 9 ? '9+' : overdue;
  }
  window.planner.onState(applyState);
})();
