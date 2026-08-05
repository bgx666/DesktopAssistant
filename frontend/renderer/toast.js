// toast.js —— 气泡窗：接收文本显示；点击气泡 → 展开面板
(() => {
  'use strict';

  window.planner.onToastText(({ text, above }) => {
    document.getElementById('toast-text').textContent = text;
    document.getElementById('toast').classList.toggle('above', !!above);
  });

  document.getElementById('toast').addEventListener('click', () => {
    window.planner.toastClick();
  });
})();
