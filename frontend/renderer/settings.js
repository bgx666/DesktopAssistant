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

  function drawChart() {
    const trigger = clampInt($('#compress_trigger').value, 20, 500, 60);
    const keep = clampInt($('#compress_keep').value, 5, 200, 20);
    const threshold = clampInt($('#compact_threshold').value, 3, 50, 8);
    const factor = clampInt($('#compact_factor').value, 2, 10, 4);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (keep >= trigger || factor >= threshold) {
      ctx.fillStyle = '#ff6b81'; ctx.font = '14px sans-serif';
      ctx.fillText('参数不合法：保留条数 < 触发条数，合并个数 < 触发阈值', 30, 60);
      return;
    }
    const pts = computePeaks(trigger, keep, threshold, factor);
    const W = canvas.width, H = canvas.height;
    const padL = 64, padR = 16, padT = 20, padB = 34;
    const maxY = Math.max(trigger * MSG_TOKEN * 1.15, 22000);
    const x = (r) => padL + (r / MAX_ROUNDS) * (W - padL - padR);
    const y = (v) => H - padB - (v / maxY) * (H - padT - padB);
    // 网格
    ctx.strokeStyle = 'rgba(125,238,227,0.15)';
    ctx.fillStyle = '#8fa8b8'; ctx.font = '11px sans-serif';
    for (let i = 0; i <= 5; i++) {
      const gy = padT + ((H - padT - padB) / 5) * i;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
      ctx.fillText(Math.round(maxY * (1 - i / 5)) + '', 8, gy + 4);
    }
    for (let i = 0; i <= 5; i++) {
      const gx = padL + ((W - padL - padR) / 5) * i;
      ctx.beginPath(); ctx.moveTo(gx, padT); ctx.lineTo(gx, H - padB); ctx.stroke();
      ctx.fillText(Math.round((MAX_ROUNDS / 5) * i / 1000) + 'k', gx - 8, H - 14);
    }
    ctx.fillText('轮数', W - padR - 30, H - 2);
    // 折线（峰值点）
    ctx.strokeStyle = '#5fb8d6'; ctx.lineWidth = 1.6;
    ctx.beginPath();
    pts.forEach(([r, v], i) => {
      const px = x(r), py = y(v);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();
    // 信息
    ctx.fillStyle = '#7deee3'; ctx.font = '12px sans-serif';
    ctx.fillText(`触发 ${trigger} 条 / 保留 ${keep} 条 / 每层 ${threshold} 个合并 ${factor} 个`, padL, 16);
    ctx.fillStyle = '#8fa8b8';
    ctx.fillText(`5 万轮峰值 ≈ ${pts.length ? Math.round(pts[pts.length - 1][1] / 1000) : '?'}k token`, padL, 32);
  }

  function clampInt(v, lo, hi, def) {
    const n = parseInt(v, 10);
    if (isNaN(n)) return def;
    return Math.max(lo, Math.min(hi, n));
  }

  // 输入即时重画（轻量：2500 点内毫秒级）
  ['compress_trigger', 'compress_keep', 'compact_threshold', 'compact_factor']
    .forEach((id) => $(`#${id}`).addEventListener('input', drawChart));

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
