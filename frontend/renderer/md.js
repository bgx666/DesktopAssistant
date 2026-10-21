// md.js —— 轻量 Markdown 渲染（标题 + 加粗 + 行内代码 + 代码块[轻量高亮] + 表格 + 列表/引用 + 链接），XSS 安全
// 用法：window.md.render(text) → HTML；window.md.escapeHtml(text)
// 高亮是"够用就好"的极简 tokenizer（关键字/字符串/注释/数字），覆盖 py/js/ts/json/sh/yaml；
// 先按原始文本切 token，再逐个转义包裹——不存在把用户内容当 HTML 解析的路径。
// 链接只放行 http(s)/mailto；点击走 window.planner.openExternal（系统浏览器），
// 不让 file:// 页面自己导航（会顶掉面板）。
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

  // ── 行内格式：代码 / 链接 / 粗体（安全：先占位再转义）──
  // 只有 http(s)/mailto 才是合法链接（javascript:/data:/file: 一律按普通文本）
  function safeUrl(raw) {
    let u = String(raw || '').trim();
    if (u.startsWith('<') && u.endsWith('>')) u = u.slice(1, -1).trim();
    if (!/^(https?:\/\/|mailto:)/i.test(u)) return '';
    return u;
  }

  function linkHtml(href, label) {
    const text = escapeHtml(label)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    return '<a class="md-link" href="' + escapeHtml(href) + '"'
      + ' title="' + escapeHtml(href) + '">' + text + '</a>';
  }

  function inline(raw) {
    const slots = [];
    const keep = (html) => {
      slots.push(html);
      return '\u0001' + (slots.length - 1) + '\u0001';
    };
    let s = String(raw == null ? '' : raw);
    // 1) 行内代码（内部不再解析链接/粗体）
    s = s.replace(/`([^`\n]+)`/g, (m, code) => keep('<code>' + escapeHtml(code) + '</code>'));
    // 2) Markdown 链接 [label](url)
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, href) => {
      const u = safeUrl(href);
      if (!u) return m;
      return keep(linkHtml(u, label));
    });
    // 3) 裸链接（自动识别；尾部标点/右括号还给文本）
    s = s.replace(/(https?:\/\/[^\s<>"'）】]+)/g, (m, url) => {
      let trail = '';
      while (/[.,;:!?，。；：！？）)\]】]$/.test(url)) {
        trail = url.slice(-1) + trail;
        url = url.slice(0, -1);
      }
      if (url.length < 12) return m;
      return keep(linkHtml(url, url)) + escapeHtml(trail);
    });
    // 4) 剩余文本转义 + 粗体
    s = escapeHtml(s);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    // 5) 还原占位
    s = s.replace(/\u0001(\d+)\u0001/g, (m, i) => slots[+i]);
    return s;
  }

  // ── GFM 表格 ──────────────────────────────────────────────
  function splitRow(line) {
    let s = String(line || '').trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    const cells = [];
    let cur = '';
    for (let i = 0; i < s.length; i += 1) {
      const ch = s[i];
      if (ch === '\\' && s[i + 1] === '|') { cur += '|'; i += 1; continue; }
      if (ch === '|') { cells.push(cur.trim()); cur = ''; continue; }
      cur += ch;
    }
    cells.push(cur.trim());
    return cells;
  }

  function isDelimRow(line) {
    const s = String(line || '').trim();
    if (!s.startsWith('|') || !s.includes('-')) return false;
    return /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$/.test(s);
  }

  function parseTable(lines, start) {
    const header = splitRow(lines[start]);
    const delim = splitRow(lines[start + 1]);
    if (header.length !== delim.length) return null;   // 不是合法表格
    const aligns = delim.map((d) => {
      const l = d.startsWith(':');
      const r = d.endsWith(':');
      return l && r ? 'center' : (r ? 'right' : (l ? 'left' : ''));
    });
    const rows = [];
    let i = start + 2;
    while (i < lines.length && /^\s*\|/.test(lines[i])) {
      rows.push(splitRow(lines[i]));
      i += 1;
    }
    const alignAttr = (k) => (aligns[k] ? ' style="text-align:' + aligns[k] + '"' : '');
    const th = header.map((c, k) => '<th' + alignAttr(k) + '>' + inline(c) + '</th>').join('');
    const body = rows.map((r) => '<tr>' + header.map((_, k) =>
      '<td' + alignAttr(k) + '>' + inline(r[k] || '') + '</td>').join('') + '</tr>').join('');
    return {
      html: '<div class="md-table-wrap"><table class="md-table"><thead><tr>' + th + '</tr></thead>'
        + '<tbody>' + body + '</tbody></table></div>',
      next: i,
    };
  }

  // ── Markdown 渲染 ─────────────────────────────────────────
  // 先抽出代码块（保留原文给高亮），其余按行渲染（表格/标题/列表/引用整块，
  // 其余行内）。blank 行保持原样（气泡是 pre-wrap）。
  const RE_UL = /^\s*[-*+]\s+(.+)$/;
  const RE_OL = /^\s*\d+[.)]\s+(.+)$/;
  const RE_QUOTE = /^\s*>\s?(.*)$/;

  function renderList(lines, start, re, tag) {
    const items = [];
    let i = start;
    while (i < lines.length) {
      const m = lines[i].match(re);
      if (!m) break;
      items.push(inline(m[1].trim()));
      i += 1;
    }
    return { html: '<' + tag + '>' + items.map((s) => '<li>' + s + '</li>').join('')
      + '</' + tag + '>', next: i };
  }

  function renderQuote(lines, start) {
    const parts = [];
    let i = start;
    while (i < lines.length) {
      const m = lines[i].match(RE_QUOTE);
      if (!m) break;
      parts.push(inline(m[1]));
      i += 1;
    }
    return { html: '<blockquote>' + parts.join('<br>') + '</blockquote>', next: i };
  }

  function render(text) {
    const blocks = [];
    let raw = String(text == null ? '' : text);
    raw = raw.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (m, info, code) => {
      blocks.push({ lang: (info || '').trim().split(/\s+/)[0], code });
      return '\u0000' + (blocks.length - 1) + '\u0000';
    });
    const lines = raw.split('\n');
    const out = [];
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (/^\s*\|/.test(line) && i + 1 < lines.length && isDelimRow(lines[i + 1])) {
        const tbl = parseTable(lines, i);
        if (tbl) {
          out.push(tbl.html);
          i = tbl.next - 1;
          continue;
        }
      }
      const h = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (h) {
        const n = h[1].length;
        out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>');
        continue;
      }
      if (RE_UL.test(line)) {
        const list = renderList(lines, i, RE_UL, 'ul');
        out.push(list.html);
        i = list.next - 1;
        continue;
      }
      if (RE_OL.test(line)) {
        const list = renderList(lines, i, RE_OL, 'ol');
        out.push(list.html);
        i = list.next - 1;
        continue;
      }
      if (RE_QUOTE.test(line)) {
        const q = renderQuote(lines, i);
        out.push(q.html);
        i = q.next - 1;
        continue;
      }
      out.push(inline(line));
    }
    let html = out.join('\n');
    html = html.replace(/\u0000(\d+)\u0000/g, (m, i) => {
      const b = blocks[+i];
      return '<pre class="md-code" data-lang="' + escapeHtml(b.lang || '') + '"><code>'
        + highlight(b.code, b.lang) + '</code></pre>';
    });
    return html;
  }

  // ── 链接点击 → 系统浏览器（capture：先于 toast 的"点击展开面板"）──
  if (typeof document !== 'undefined') {
    document.addEventListener('click', (e) => {
      const t = e.target;
      const a = (t && typeof t.closest === 'function') ? t.closest('a') : null;
      if (!a) return;
      const href = a.getAttribute('href') || '';
      e.preventDefault();          // file:// 页面内导航会顶掉面板
      e.stopPropagation();         // 别让气泡把点击当成"展开面板"
      const u = safeUrl(href);
      if (!u) return;
      try {
        if (window.planner && window.planner.openExternal) {
          window.planner.openExternal(u);
        } else if (window.open) {
          window.open(u, '_blank', 'noopener');
        }
      } catch { /* 忽略 */ }
    }, true);
  }

  window.md = { render, escapeHtml, highlight, inline };
})();
