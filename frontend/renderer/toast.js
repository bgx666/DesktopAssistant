// toast.js —— 气泡窗：接收文本显示；点击气泡 → 展开面板
(() => {
  'use strict';

  window.planner.onToastText(({ text, above }) => {
    const el = document.getElementById('toast-text');
    // 轻量 markdown 渲染（加粗/行内代码/代码块），不再显示原始 ** 符号
    el.innerHTML = window.md.render(text);
    document.getElementById('toast').classList.toggle('above', !!above);
    // 文本渲染后测量实际高度 → 通知主进程调整窗口（内容多高窗口就多高；
    // offsetHeight 不受入场动画 transform 缩放影响）
    requestAnimationFrame(() => {
      const h = Math.round(document.getElementById('toast').offsetHeight + 4);
      if (h > 0) window.planner.toastResize(h);
    });
  });

  document.getElementById('toast').addEventListener('click', () => {
    window.planner.toastClick();
  });
})();
