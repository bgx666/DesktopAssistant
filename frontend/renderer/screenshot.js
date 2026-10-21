// screenshot.js —— 截屏覆盖层：框选 → 涂鸦（画笔）→ ✓ 确认 / ✗ 取消
//
// 数据来源：主进程 desktopCapturer 截取光标所在屏幕（物理像素 PNG），
// 通过 planner.screenshotGet() 取回（{dataUrl, scale}）。scale = 物理像素 /
// 页面 CSS 像素（高 DPI 屏 >1）——导出裁剪时按它换算。
// 确认后回传裁剪好的 PNG dataURL（原图 + 笔迹一起），主进程保存并挂载为
// "待发送文件"（不直接发送）。
(() => {
  'use strict';

  const shotEl = document.getElementById('shot');
  const selEl = document.getElementById('sel');
  const ink = document.getElementById('ink');
  const tools = document.getElementById('tools');
  const hint = document.getElementById('hint');
  const sizeEl = document.getElementById('size');
  const penBtn = document.getElementById('pen');
  const undoBtn = document.getElementById('undo');
  const okBtn = document.getElementById('ok');
  const cancelBtn = document.getElementById('cancel');

  let dataUrl = '';
  let scale = 1;            // 截图物理像素 / CSS 像素
  let img = null;           // Image 对象（导出时裁剪用）
  let selecting = false;
  let hasSel = false;
  let startX = 0;
  let startY = 0;
  const sel = { x: 0, y: 0, w: 0, h: 0 };
  let penMode = false;
  let strokes = [];         // [[{x,y}...], ...]（CSS 像素坐标）
  let drawing = false;
  let curStroke = null;

  // ── 选区 ──────────────────────────────────────────────
  function setSel(x, y, w, h) {
    sel.x = x; sel.y = y; sel.w = w; sel.h = h;
    selEl.style.left = x + 'px';
    selEl.style.top = y + 'px';
    selEl.style.width = w + 'px';
    selEl.style.height = h + 'px';
    selEl.style.backgroundPosition = `${-x}px ${-y}px`;
    selEl.style.backgroundSize = `${window.innerWidth}px ${window.innerHeight}px`;
    sizeEl.textContent = `${Math.round(w * scale)} × ${Math.round(h * scale)}`;
    placeTools();
  }

  function placeTools() {
    const tw = tools.offsetWidth || 190;
    const th = tools.offsetHeight || 38;
    let x = sel.x + sel.w - tw;                 // 右对齐选区（类微信）
    let y = sel.y + sel.h + 8;
    if (y + th > window.innerHeight - 8) y = sel.y - th - 8;   // 下方放不下放上方
    x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
    y = Math.max(8, y);
    tools.style.left = x + 'px';
    tools.style.top = y + 'px';
  }

  // ── 涂鸦 ──────────────────────────────────────────────
  function redrawInk() {
    const ctx = ink.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, ink.width, ink.height);
    ctx.strokeStyle = '#ff3b30';
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (const s of strokes) {
      if (s.length === 1) {                     // 单击点：画个小点
        ctx.beginPath();
        ctx.arc(s[0].x, s[0].y, 1.6, 0, Math.PI * 2);
        ctx.fillStyle = '#ff3b30';
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      ctx.moveTo(s[0].x, s[0].y);
      for (let i = 1; i < s.length; i += 1) ctx.lineTo(s[i].x, s[i].y);
      ctx.stroke();
    }
  }

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    ink.width = Math.round(window.innerWidth * dpr);
    ink.height = Math.round(window.innerHeight * dpr);
    ink.style.width = window.innerWidth + 'px';
    ink.style.height = window.innerHeight + 'px';
    redrawInk();
  }

  // ── 交互 ──────────────────────────────────────────────
  window.addEventListener('mousedown', (e) => {
    if (e.button === 2) { cancel(); return; }          // 右键取消（类微信）
    if (e.target && typeof e.target.closest === 'function'
        && e.target.closest('#tools')) return;
    if (penMode && hasSel) {                           // 画笔：涂抹
      drawing = true;
      curStroke = [{ x: e.clientX, y: e.clientY }];
      strokes.push(curStroke);
      redrawInk();
      return;
    }
    selecting = true;                                  // 重新框选
    hasSel = false;
    startX = e.clientX;
    startY = e.clientY;
    tools.classList.add('hidden');
    hint.classList.add('hidden');
    selEl.classList.remove('hidden');
    setSel(startX, startY, 0, 0);
  });

  window.addEventListener('mousemove', (e) => {
    if (drawing && curStroke) {
      curStroke.push({ x: e.clientX, y: e.clientY });
      redrawInk();
      return;
    }
    if (!selecting) return;
    const x = Math.min(startX, e.clientX);
    const y = Math.min(startY, e.clientY);
    const w = Math.abs(e.clientX - startX);
    const h = Math.abs(e.clientY - startY);
    setSel(x, y, w, h);
  });

  window.addEventListener('mouseup', () => {
    if (drawing) { drawing = false; curStroke = null; return; }
    if (!selecting) return;
    selecting = false;
    if (sel.w < 4 || sel.h < 4) {                      // 太小的拖拽不算选区
      selEl.classList.add('hidden');
      hint.classList.remove('hidden');
      return;
    }
    hasSel = true;
    tools.classList.remove('hidden');
    placeTools();
  });

  window.addEventListener('contextmenu', (e) => e.preventDefault());
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); cancel(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      undo();
      return;
    }
    if (e.key === 'Enter' && hasSel) { e.preventDefault(); confirmShot(); }
  });

  // ── 工具条 ────────────────────────────────────────────
  penBtn.addEventListener('click', () => {
    penMode = !penMode;
    penBtn.classList.toggle('active', penMode);
    document.body.style.cursor = penMode ? 'cell' : 'crosshair';
  });

  function undo() {
    if (!strokes.length) return;
    strokes.pop();
    redrawInk();
  }
  undoBtn.addEventListener('click', undo);

  function cancel() {
    window.planner.screenshotCancel();
  }
  cancelBtn.addEventListener('click', cancel);

  async function confirmShot() {
    if (!hasSel) return;
    if (img && !img.complete) {                        // 底图还没解码完：等它（曾静默丢确认）
      await new Promise((resolve) => {
        const t = setTimeout(resolve, 3000);           // 大屏截图 base64 大、解码慢，给足 3s
        img.addEventListener('load', () => { clearTimeout(t); resolve(); }, { once: true });
        img.addEventListener('error', () => { clearTimeout(t); resolve(); }, { once: true });
      });
    }
    if (!img || !img.naturalWidth) {
      window.planner.reportLog('screenshot: 底图未就绪，无法确认');
      return;
    }
    const sx = Math.round(sel.x * scale);
    const sy = Math.round(sel.y * scale);
    const sw = Math.max(1, Math.round(sel.w * scale));
    const sh = Math.max(1, Math.round(sel.h * scale));
    const out = document.createElement('canvas');
    out.width = sw;
    out.height = sh;
    const ctx = out.getContext('2d');
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    if (strokes.length) {                              // 笔迹一起裁进图里
      ctx.save();
      ctx.scale(scale, scale);                         // 逻辑坐标 → 物理像素
      ctx.translate(-sel.x, -sel.y);
      ctx.strokeStyle = '#ff3b30';
      ctx.lineWidth = 3;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      for (const s of strokes) {
        if (s.length === 1) {
          ctx.beginPath();
          ctx.arc(s[0].x, s[0].y, 1.6, 0, Math.PI * 2);
          ctx.fillStyle = '#ff3b30';
          ctx.fill();
          continue;
        }
        ctx.beginPath();
        ctx.moveTo(s[0].x, s[0].y);
        for (let i = 1; i < s.length; i += 1) ctx.lineTo(s[i].x, s[i].y);
        ctx.stroke();
      }
      ctx.restore();
    }
    window.planner.reportLog(`screenshot: 确认导出 ${sw}x${sh}（笔迹 ${strokes.length} 笔）`);
    window.planner.screenshotDone(out.toDataURL('image/png'));
  }
  okBtn.addEventListener('click', confirmShot);

  // ── 初始化（从主进程取截图数据）──────────────────────
  (async () => {
    const init = await window.planner.screenshotGet();
    if (!init || !init.dataUrl) { cancel(); return; }
    dataUrl = init.dataUrl;
    scale = init.scale || 1;
    window.planner.reportLog(
      `screenshot: 初始化 ${window.innerWidth}x${window.innerHeight} scale=${scale.toFixed(2)}`);
    shotEl.style.backgroundImage = `url(${dataUrl})`;
    selEl.style.backgroundImage = `url(${dataUrl})`;
    img = new Image();
    img.src = dataUrl;
    resizeCanvas();
  })();
  window.addEventListener('resize', resizeCanvas);
})();
