// settings.js —— 设置窗口：表单回显 + 压缩示意图（峰值折线，即时重算）+ 保存
(() => {
  'use strict';
  const API = (window.planner && window.planner.apiBase) || 'http://127.0.0.1:18771';
  const $ = (s) => document.querySelector(s);
  const canvas = $('#chart');
  const ctx = canvas.getContext('2d');

  // ── 读取当前设置并回显 ──
  let current = {};
  async function loadSettings() {
    try {
      const r = await fetch(API + '/settings', { signal: AbortSignal.timeout(4000) });
      const d = await r.json();
      current = d.settings || {};
      for (const k of ['press_ms', 'compress_trigger', 'compress_keep',
                       'compact_threshold', 'compact_factor']) {
        $(`#${k}`).value = current[k] ?? '';
      }
      $('#llm_api_key').value = current.llm_api_key || '';
      $('#llm_base_url').value = current.llm_base_url || '';
      $('#llm_model').value = current.llm_model || '';
      drawChart();
    } catch { $('#msg').textContent = '后端不可用，无法读取设置'; }
  }

  // ── 压缩示意图：只算"每次压缩触发前"的峰值点（性能优先）──
  // 模拟：每事件压缩 (trigger-keep) 条消息；节点逐层 ≥threshold 合并 factor 个。
  const MSG_TOKEN = 360, NODE_TOKEN = 500;
  const MAX_ROUNDS = 50000;
  function computePeaks(trigger, keep, threshold, factor) {
    const pts = [];
    const levels = [];
    let ev = 0;
    while (true) {
      ev++;
      const rounds = (ev * (trigger - keep) + keep) / 2;
      if (rounds > MAX_ROUNDS) break;
      levels[0] = (levels[0] || 0) + 1;
      let lv = 0;
      while ((levels[lv] || 0) >= threshold) {
        levels[lv] -= factor;
        levels[lv + 1] = (levels[lv + 1] || 0) + 1;
        lv++;
      }
      let nodes = 0;
      for (const n of levels) nodes += n || 0;
      pts.push([rounds, trigger * MSG_TOKEN + nodes * NODE_TOKEN]);
    }
    return pts;
  }

  // ── 白底黑字 + 可交互示意图：视口平移（拖动）/缩放（滚轮）/重置（双击）──
  const PAD = { l: 64, r: 16, t: 24, b: 36 };
  let pts = [];
  let viewStart = 0, viewEnd = MAX_ROUNDS;
  let dragState = null;

  function niceStep(range, target) {
    if (range <= 0) return 1;
    const rough = range / target;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    for (const m of [1, 2, 2.5, 5, 10]) {
      if (rough <= m * mag) return m * mag;
    }
    return 10 * mag;
  }

  function resetView() {
    viewStart = 0;
    viewEnd = pts.length ? pts[pts.length - 1][0] : MAX_ROUNDS;
  }

  function drawChart() {
    const trigger = clampInt($('#compress_trigger').value, 20, 500, 60);
    const keep = clampInt($('#compress_keep').value, 5, 200, 20);
    const threshold = clampInt($('#compact_threshold').value, 3, 50, 8);
    const factor = clampInt($('#compact_factor').value, 2, 10, 4);
    const W = canvas.width, H = canvas.height;
    const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);
    if (keep >= trigger || factor >= threshold) {
      ctx.fillStyle = '#d33'; ctx.font = '14px sans-serif';
      ctx.fillText('参数不合法：保留条数 < 触发条数，合并个数 < 触发阈值', 30, 60);
      return;
    }
    pts = computePeaks(trigger, keep, threshold, factor);
    if (viewStart >= viewEnd) resetView();
    // 可见区间与 y 上限（自适应：按可见数据的最大值）
    const vis = pts.filter((p) => p[0] >= viewStart && p[0] <= viewEnd);
    const yMax = Math.max(trigger * MSG_TOKEN, ...vis.map((p) => p[1])) * 1.08;
    const xMap = (r) => PAD.l + ((r - viewStart) / (viewEnd - viewStart)) * plotW;
    const yMap = (v) => H - PAD.b - (v / yMax) * plotH;

    // 网格 + 自适应刻度（白底黑字）
    ctx.font = '11px sans-serif';
    ctx.lineWidth = 1;
    const yStep = niceStep(yMax, 5);
    ctx.strokeStyle = '#e8eef4';
    ctx.fillStyle = '#666';
    for (let v = 0; v <= yMax; v += yStep) {
      const gy = yMap(v);
      ctx.beginPath(); ctx.moveTo(PAD.l, gy); ctx.lineTo(W - PAD.r, gy); ctx.stroke();
      ctx.fillText(Math.round(v).toLocaleString(), 6, gy + 4);
    }
    const xStep = niceStep(viewEnd - viewStart, 6);
    ctx.strokeStyle = '#e8eef4';
    for (let r = Math.ceil(viewStart / xStep) * xStep; r <= viewEnd; r += xStep) {
      const gx = xMap(r);
      ctx.beginPath(); ctx.moveTo(gx, PAD.t); ctx.lineTo(gx, H - PAD.b); ctx.stroke();
      ctx.fillText((r / 1000).toFixed(r >= 10000 ? 0 : 1) + 'k', gx - 8, H - 14);
    }
    ctx.fillStyle = '#333';
    ctx.fillText('轮数', W - PAD.r - 26, H - 2);

    // 折线（黑线）
    ctx.strokeStyle = '#1a1a2e'; ctx.lineWidth = 1.6;
    ctx.beginPath();
    vis.forEach(([r, v], i) => {
      const px = xMap(r), py = yMap(v);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();

    // 信息（黑字）
    ctx.fillStyle = '#111'; ctx.font = '12px sans-serif';
    ctx.fillText(`触发 ${trigger} 条 / 保留 ${keep} 条 / 每层 ${threshold} 个合并 ${factor} 个`, PAD.l, 16);
    ctx.fillStyle = '#666';
    const last = pts.length ? pts[pts.length - 1][1] : 0;
    ctx.fillText(`峰值（5 万轮）≈ ${Math.round(last / 1000)}k token · 拖动平移 · 滚轮缩放 · 双击重置`, PAD.l, 42);
  }

  // ── 交互：拖动平移 / 滚轮缩放 / 双击重置 ──
  canvas.addEventListener('mousedown', (e) => {
    dragState = { x: e.offsetX, start: viewStart, end: viewEnd };
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragState) return;
    const plotW = canvas.width - PAD.l - PAD.r;
    const dr = ((e.clientX - canvas.getBoundingClientRect().left - PAD.l) - dragState.x) / plotW * (dragState.end - dragState.start);
    viewStart = dragState.start - dr;
    viewEnd = dragState.end - dr;
    drawChart();
  });
  window.addEventListener('mouseup', () => { dragState = null; });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const plotW = canvas.width - PAD.l - PAD.r;
    const mx = (e.offsetX - PAD.l) / plotW;          // 0~1 鼠标在绘图区位置
    const range = viewEnd - viewStart;
    const factor = e.deltaY > 0 ? 1.25 : 0.8;        // 下滚缩小视野(放大)，上滚放大视野
    let newRange = Math.min(MAX_ROUNDS * 2, Math.max(200, range * factor));
    const anchor = viewStart + range * mx;           // 锚点轮数（保持不动）
    viewStart = anchor - newRange * mx;
    viewEnd = viewStart + newRange;
    if (viewStart < 0) { viewEnd -= viewStart; viewStart = 0; }
    drawChart();
  }, { passive: false });
  canvas.addEventListener('dblclick', () => { resetView(); drawChart(); });

  function clampInt(v, lo, hi, def) {
    const n = parseInt(v, 10);
    if (isNaN(n)) return def;
    return Math.max(lo, Math.min(hi, n));
  }

  // 输入即时重画（轻量：2500 点内毫秒级），参数变化重置视口
  ['compress_trigger', 'compress_keep', 'compact_threshold', 'compact_factor']
    .forEach((id) => $(`#${id}`).addEventListener('input', () => { resetView(); drawChart(); }));

  // ── 保存 ──
  $('#save').addEventListener('click', async () => {
    const updates = {};
    for (const k of ['press_ms', 'compress_trigger', 'compress_keep',
                     'compact_threshold', 'compact_factor']) {
      const v = parseInt($(`#${k}`).value, 10);
      if (!isNaN(v)) updates[k] = v;
    }
    updates.llm_api_key = $('#llm_api_key').value.trim();
    updates.llm_base_url = $('#llm_base_url').value.trim();
    updates.llm_model = $('#llm_model').value.trim();
    const btn = $('#save');
    btn.disabled = true;
    $('#msg').className = '';
    try {
      const r = await fetch(API + '/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates }),
      });
      const d = await r.json();
      if (d.ok) {
        current = d.settings;
        $('#msg').textContent = '已保存并生效 ✓';
        $('#msg').className = 'ok';
        window.planner.settingsSaved();   // 通知主进程广播（长按时间等前端项即时更新）
      } else {
        $('#msg').textContent = '保存失败：' + (d.error || '未知错误');
      }
    } catch {
      $('#msg').textContent = '保存失败：后端不可用';
    }
    btn.disabled = false;
  });

  loadSettings();
})();
