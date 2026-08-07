// fileDrop.js —— 拖拽文件公共模块：File 列表 → 挂载数据 [{name, path, kind, content?}]
// kind 按扩展名分类：text（文本）/ doc（pdf,docx）/ image（图片）/ other
// 文本 ≤32KB 且可解码 → 读出内容直注；其余只给路径（后端解析/OCR/工具读取）。
(() => {
  'use strict';

  const TEXT_EXT = new Set([
    'txt', 'md', 'markdown', 'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'csv', 'log',
    'ini', 'conf', 'cfg', 'yaml', 'yml', 'toml', 'html', 'htm', 'css', 'xml', 'sql',
    'sh', 'bat', 'ps1', 'c', 'cpp', 'h', 'hpp', 'java', 'go', 'rs', 'rb', 'php',
    'kt', 'swift', 'vue', 'svelte', 'gitignore', 'env', 'properties', 'reg',
  ]);
  const DOC_EXT = new Set(['pdf', 'docx', 'doc']);   // .doc 老格式也走 doc 分支（后端提示不支持）
  const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'bmp', 'gif', 'webp', 'tiff', 'ico']);
  const MAX_TEXT_BYTES = 32 * 1024;   // 与后端约定：≤32KB 文本前端直读

  function classify(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    if (IMAGE_EXT.has(ext)) return 'image';
    if (DOC_EXT.has(ext)) return 'doc';
    if (TEXT_EXT.has(ext)) return 'text';
    return 'other';
  }

  // File → {name, path, kind, content|null}（异步：≤32KB 文本尝试读内容）
  async function handleFile(file) {
    const name = file.name || '未命名';
    const kind = classify(name);
    let path = '';
    try {
      path = window.planner.getPathForFile(file) || '';
    } catch { /* 路径获取失败则留空 */ }
    let content = null;
    if (kind === 'text' && file.size <= MAX_TEXT_BYTES) {
      try {
        const buf = new Uint8Array(await file.arrayBuffer());
        if (!buf.includes(0)) {           // 无 null 字节才按文本解码
          content = new TextDecoder('utf-8', { fatal: false }).decode(buf);
          content = content || null;
        }
      } catch { content = null; }
    }
    return { name, path, kind, content };
  }

  async function handleFiles(files) {
    const list = Array.from(files || []);
    const out = [];
    for (const f of list) {
      const item = await handleFile(f);
      if (item.path || item.content) out.push(item);
    }
    return out;
  }

  window.fileDrop = { handleFiles };
})();
