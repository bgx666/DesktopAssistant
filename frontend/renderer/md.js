// md.js —— 轻量 Markdown 渲染（加粗 + 行内代码 + 代码块），XSS 安全
// 用法：window.md.render(text) → HTML；window.md.escapeHtml(text)
(() => {
  'use strict';

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // 先 escapeHtml 再替换标记（XSS 安全）；代码块内容先占位避免被加粗规则误伤。
  function render(text) {
    let html = escapeHtml(text);
    const blocks = [];
    html = html.replace(/```([\s\S]*?)```/g, (m, code) => {
      blocks.push(code.trim());
      return '\u0000' + (blocks.length - 1) + '\u0000';
    });
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\u0000(\d+)\u0000/g, (m, i) => `<pre class="md-code">${blocks[+i]}</pre>`);
    return html;
  }

  window.md = { render, escapeHtml };
})();
