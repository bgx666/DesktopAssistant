// md.js —— 轻量 Markdown 渲染（加粗 + 行内代码 + 代码块[含轻量语法高亮]），XSS 安全
// 用法：window.md.render(text) → HTML；window.md.escapeHtml(text)
// 高亮是"够用就好"的极简 tokenizer（关键字/字符串/注释/数字），覆盖 py/js/ts/json/sh/yaml；
// 先按原始文本切 token，再逐个转义包裹——不存在把用户内容当 HTML 解析的路径。
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

  // ── 轻量语法高亮 ──────────────────────────────────────────
  const LANG_ALIAS = {
    python: 'py', py: 'py', javascript: 'js', js: 'js', typescript: 'ts', ts: 'ts',
    shell: 'sh', bash: 'sh', zsh: 'sh', sh: 'sh', yml: 'yaml', yaml: 'yaml', json: 'json',
  };
  const KW = {
    py: ('def class return import from as if elif else for while try except finally with '
      + 'lambda yield async await pass raise in not and or is None True False self global '
      + 'nonlocal assert del print len range').split(' '),
    js: ('function return const let var if else for while try catch finally class new '
      + 'typeof instanceof this import export from async await of in delete void throw '
      + 'switch case break continue default null undefined true false').split(' '),
    ts: ('function return const let var if else for while try catch finally class new '
      + 'typeof instanceof this import export from async await of in delete void throw '
      + 'switch case break continue default null undefined true false interface type enum '
      + 'implements extends public private readonly as').split(' '),
    sh: ('if then else fi for do done while case esac function return export local echo '
      + 'exit cd set source unset').split(' '),
    json: [],
    yaml: [],
  };
  const HASH_COMMENT = new Set(['py', 'sh', 'yaml']);

  function span(cls, text) {
    return '<span class="tok-' + cls + '">' + escapeHtml(text) + '</span>';
  }

  function findStringEnd(code, start, quote) {
    const triple = code.slice(start, start + 3) === quote.repeat(3);
    const q = triple ? quote.repeat(3) : quote;
    let i = start + q.length;
    while (i < code.length) {
      if (code[i] === '\\') { i += 2; continue; }
      if (code.startsWith(q, i)) return i + q.length;
      if (!triple && code[i] === '\n') return i;   // 单行字符串不跨行
      i += 1;
    }
    return code.length;
  }

  function highlight(code, lang) {
    const L = LANG_ALIAS[(lang || '').toLowerCase()] || '';
    const kw = new Set(KW[L] || []);
    const hash = HASH_COMMENT.has(L);
    let out = '';
    let i = 0;
    while (i < code.length) {
      const ch = code[i];
      // 块注释 /* ... */
      if ((L === 'js' || L === 'ts') && code.startsWith('/*', i)) {
        const end = code.indexOf('*/', i + 2);
        const seg = code.slice(i, end === -1 ? code.length : end + 2);
        out += span('c', seg);
        i += seg.length;
        continue;
      }
      // 行注释 // 或 #
      if ((code.startsWith('//', i) && !hash) || (hash && ch === '#')) {
        const end = code.indexOf('\n', i);
        const seg = code.slice(i, end === -1 ? code.length : end);
        out += span('c', seg);
        i += seg.length;
        continue;
      }
      // 字符串（含三引号与模板串）
      if (ch === '"' || ch === "'" || ch === '`') {
        const end = findStringEnd(code, i, ch);
        out += span('s', code.slice(i, end));
        i = end;
        continue;
      }
      // 标识符 → 关键字
      if (/[A-Za-z_$]/.test(ch)) {
        let j = i;
        while (j < code.length && /[\w$]/.test(code[j])) j += 1;
        const word = code.slice(i, j);
        out += kw.has(word) ? span('k', word) : escapeHtml(word);
        i = j;
        continue;
      }
      // 数字
      if (/[0-9]/.test(ch)) {
        let j = i;
        while (j < code.length && /[0-9a-fA-FxX._]/.test(code[j])) j += 1;
        out += span('n', code.slice(i, j));
        i = j;
        continue;
      }
      out += escapeHtml(ch);
      i += 1;
    }
    return out;
  }

  // ── Markdown 渲染 ─────────────────────────────────────────
  // 先抽出代码块（保留原文给高亮），其余按转义文本渲染。
  function render(text) {
    const blocks = [];
    let raw = String(text == null ? '' : text);
    raw = raw.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (m, info, code) => {
      blocks.push({ lang: (info || '').trim().split(/\s+/)[0], code });
      return '\u0000' + (blocks.length - 1) + '\u0000';
    });
    let html = escapeHtml(raw);
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\u0000(\d+)\u0000/g, (m, i) => {
      const b = blocks[+i];
      return '<pre class="md-code" data-lang="' + escapeHtml(b.lang || '') + '"><code>'
        + highlight(b.code, b.lang) + '</code></pre>';
    });
    return html;
  }

  window.md = { render, escapeHtml, highlight };
})();
