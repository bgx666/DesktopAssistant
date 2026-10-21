// settings.js —— 设置窗口：选项卡切换 + 表单回显 + 压缩示意图 + 保存
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const canvas = $('#chart');
  const ctx = canvas.getContext('2d');

  // ── 应用内确认框（替代 window.confirm）────────────────────
  // 原因：Windows 下原生 confirm 关闭后键盘焦点可能不还给页面（能点、打不了字）
  let uiConfirmResolve = null;
  function uiConfirm(message) {
    const modal = $('#ui-confirm');
    if (!modal) return Promise.resolve(window.confirm(message));   // DOM 缺失兜底
    $('#ui-confirm-text').textContent = String(message || '');
    modal.classList.remove('hidden');
    return new Promise((resolve) => { uiConfirmResolve = resolve; });
  }
  function settleUiConfirm(ok) {
    const modal = $('#ui-confirm');
    if (modal) modal.classList.add('hidden');
    const r = uiConfirmResolve;
    uiConfirmResolve = null;
    if (r) r(ok);
  }
  $('#ui-confirm-ok').addEventListener('click', () => settleUiConfirm(true));
  $('#ui-confirm-cancel').addEventListener('click', () => settleUiConfirm(false));
  document.addEventListener('keydown', (e) => {
    if (!uiConfirmResolve) return;
    if (e.key === 'Escape') { e.preventDefault(); settleUiConfirm(false); }
    else if (e.key === 'Enter') { e.preventDefault(); settleUiConfirm(true); }
  });

  // ── 选项卡切换 ──
  document.querySelectorAll('aside .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('aside .tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      const name = tab.dataset.tab;
      document.querySelectorAll('.pane').forEach((p) => {
        p.classList.toggle('active', p.dataset.pane === name);
      });
      if (name === 'compress') drawChart();   // 隐藏时 canvas 不可见，切回重画
    });
  });

  // ── 模型 API：分服务商 Key / 模型 / 端点（切换服务商互不覆盖）──
  let llmProviders = [];
  const llmDraft = {};    // 未保存的编辑：provider → {key, model, base}

  function activeProvider() {
    return $('#llm_provider').value || 'deepseek';
  }

  function snapshotProviderFields() {
    const p = activeProvider();
    llmDraft[p] = {
      key: $('#llm_api_key_active').value,
      model: $('#llm_model_active').value,
      base: $('#llm_base_url_active').value,
    };
  }

  function applyProviderFields() {
    const p = activeProvider();
    const preset = llmProviders.find((x) => x.id === p) || { models: [], base_url: '' };
    const draft = llmDraft[p];
    $('#llm_api_key_active').value = draft ? draft.key : (current[`llm_api_key_${p}`] || '');
    $('#llm_model_active').value = draft ? draft.model : (current[`llm_model_${p}`] || '');
    $('#llm_base_url_active').value = draft ? draft.base : (current[`llm_base_url_${p}`] || '');
    $('#llm_base_url_active').placeholder = '留空 = ' + preset.base_url;
    $('#llm_provider_hint').textContent = preset.base_url || '';
    const list = $('#llm_model_list');
    list.innerHTML = '';
    (preset.models || []).forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m;
      list.appendChild(opt);
    });
  }

  $('#llm_provider').addEventListener('change', () => {
    snapshotProviderFields();   // 暂存当前服务商的未保存编辑
    applyProviderFields();
  });

  // ── 代码工作区 + 已记住的授权规则 ────────────────────────
  async function loadWorkspace() {
    try {
      const r = await window.planner.apiFetch('/workspace');
      const d = JSON.parse(r.text);
      if (!d.ok) return;
      $('#workspace_root').value = d.workspace || '';
      renderPermRules(d.rules || []);
    } catch { /* 后端不可用：留空 */ }
  }

  function renderPermRules(rules) {
    const box = $('#perm-rules');
    if (!box) return;
    box.innerHTML = '';
    if (!rules.length) {
      const div = document.createElement('div');
      div.className = 'hint';
      div.textContent = '（还没有规则——审批时选「记住并允许」会出现这里）';
      box.appendChild(div);
      return;
    }
    rules.forEach((r, i) => {
      const row = document.createElement('div');
      row.className = 'row';
      const tool = document.createElement('label');
      tool.textContent = r.tool || '?';
      const val = document.createElement('span');
      val.className = 'hint';
      val.style.width = 'auto';
      val.style.flex = '1';
      val.textContent = r.cmd_glob ? `命令：${r.cmd_glob}` : (r.path_glob || '');
      const btn = document.createElement('button');
      btn.className = 'mini';
      btn.type = 'button';
      btn.textContent = '删除';
      btn.addEventListener('click', async () => {
        try {
          await window.planner.apiFetch('/permissions/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: i }),
          });
        } catch { /* 忽略 */ }
        await loadWorkspace();
      });
      row.appendChild(tool);
      row.appendChild(val);
      row.appendChild(btn);
      box.appendChild(row);
    });
  }

  // ── 读取当前设置并回显：长按时间走主进程（本地配置），其余走后端 ──
  let current = {};
  async function loadSettings() {
    try {
      // 长按时间 / 气泡时长 / 快速输入快捷键：主进程本地配置（无需等后端）
      const ui = await window.planner.getUiSettings();
      if (ui && ui.press_ms != null) $('#press_ms').value = ui.press_ms;
      if (ui && ui.toast_seconds != null) $('#toast_seconds').value = ui.toast_seconds;
      if (ui && ui.quick_input_hotkey) $('#quick_input_hotkey').value = ui.quick_input_hotkey;
    } catch { /* 主进程不可用 */ }
    try {
      const r = await window.planner.apiFetch('/settings');
      const d = JSON.parse(r.text);
      current = d.settings || {};
      for (const k of ['compress_keep',
                       'compact_threshold', 'compact_factor',
                       'compress_token_budget', 'compress_chunk_tokens',
                       'node_token_budget',
                       'model_call_limit']) {
        $(`#${k}`).value = current[k] ?? '';
      }
      try {
        const pr = await window.planner.apiFetch('/llm/providers');
        const pd = JSON.parse(pr.text);
        if (pd.ok) llmProviders = pd.providers || [];
      } catch { /* 后端不可用：模型建议列表留空 */ }
      applyProviderFields();
      // 思考开关与强度（默认 adaptive / high，与 settings.py 的 DEFAULT_SETTINGS 一致）
      $('#thinking_mode').value = current.thinking_mode || 'adaptive';
      $('#reasoning_effort').value = current.reasoning_effort || 'high';
      $('#tts_enabled').checked = current.tts_enabled !== false;
      $('#tts_engine').value = current.tts_engine || '';
      drawChart();
    } catch { $('#msg').textContent = '后端不可用，无法读取压缩/LLM 设置'; }
    // 代码工作区（独立端点：/workspace）+ 已记住的授权规则
    await loadWorkspace();
    await loadVoices($('#tts_engine').value);
    // 就绪标记：设置全部加载完成（e2e 等这个再操作——页面刚出现时脚本/数据
    // 可能还没就绪，此时点保存会因字段为空/无监听而静默失败）
    document.body.dataset.settingsLoaded = '1';
  }

  // 按引擎加载音色列表（保留当前已选音色；没有则清空）
  async function loadVoices(engine) {
    const sel = $('#tts_voice');
    const prev = sel.value;
    sel.innerHTML = '';
    try {
      const q = engine ? '?engine=' + encodeURIComponent(engine) : '';
      const r = await window.planner.apiFetch('/tts/voices' + q);
      const d = JSON.parse(r.text);
      (d.voices || []).forEach((v) => {
        const opt = document.createElement('option');
        opt.value = v.id;
        opt.textContent = v.label;
        sel.appendChild(opt);
      });
      if (prev && [...sel.options].some((o) => o.value === prev)) {
        sel.value = prev;
      } else if (current.tts_voice && [...sel.options].some((o) => o.value === current.tts_voice)) {
        sel.value = current.tts_voice;
      }
    } catch { /* 后端不可用：音色列表留空 */ }
  }

  $('#tts_engine').addEventListener('change', () => {
    loadVoices($('#tts_engine').value);
  });

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
    // 叶子压缩只看 token 预算：图上把预算换算成"等价条数"（每条按 MSG_TOKEN 估）画曲线
    const tokenBudget = clampInt($('#compress_token_budget').value, 10000, 500000, 100000);
    const keep = clampInt($('#compress_keep').value, 5, 200, 20);
    const threshold = clampInt($('#compact_threshold').value, 3, 50, 8);
    const factor = clampInt($('#compact_factor').value, 2, 10, 4);
    const trigger = Math.max(keep + 1, Math.round(tokenBudget / MSG_TOKEN));
    const W = canvas.width, H = canvas.height;
    const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);
    if (factor >= threshold) {
      ctx.fillStyle = '#d33'; ctx.font = '14px sans-serif';
      ctx.fillText('参数不合法：合并个数 < 触发阈值', 30, 60);
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
    ctx.fillText(`触发 ${tokenBudget.toLocaleString()} tok（≈${trigger} 条）/ 保留 ${keep} 条 / 每层 ${threshold} 个合并 ${factor} 个`, PAD.l, 16);
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
  ['compress_token_budget', 'compress_keep', 'compact_threshold', 'compact_factor']
    .forEach((id) => $(`#${id}`).addEventListener('input', () => { resetView(); drawChart(); }));

  // ── 保存：长按时间 → 主进程（本地，立即生效）；压缩/LLM → 后端 ──
  $('#save').addEventListener('click', async () => {
    const btn = $('#save');
    btn.disabled = true;
    $('#msg').className = '';
    const errs = [];
    // 1) 长按时间 / 气泡时长 / 快速输入快捷键（主进程本地配置）
    try {
      const v = parseInt($('#press_ms').value, 10);
      const ui = {};
      if (!isNaN(v)) ui.press_ms = v;
      const ts = parseInt($('#toast_seconds').value, 10);
      if (!isNaN(ts)) ui.toast_seconds = ts;
      const hot = $('#quick_input_hotkey').value.trim();
      ui.quick_input_hotkey = hot || 'Alt+J';
      if (!hot) $('#quick_input_hotkey').value = 'Alt+J';
      window.planner.saveUiSettings(ui);
    } catch { errs.push('长按时间/气泡时长/快捷键保存失败'); }
    // 2) 压缩参数 + LLM（后端）
    const updates = {};
    for (const k of ['compress_keep',
                     'compact_threshold', 'compact_factor',
                     'compress_token_budget', 'compress_chunk_tokens',
                     'node_token_budget',
                     'model_call_limit']) {
      const v = parseInt($(`#${k}`).value, 10);
      if (!isNaN(v)) updates[k] = v;
    }
    snapshotProviderFields();   // 把当前服务商的编辑并入草稿
    Object.entries(llmDraft).forEach(([p, v]) => {
      updates[`llm_api_key_${p}`] = String(v.key || '').trim();
      updates[`llm_model_${p}`] = String(v.model || '').trim();
      updates[`llm_base_url_${p}`] = String(v.base || '').trim();
    });
    updates.llm_provider = activeProvider();
    updates.thinking_mode = $('#thinking_mode').value;
    updates.reasoning_effort = $('#reasoning_effort').value;
    updates.tts_enabled = $('#tts_enabled').checked;
    updates.tts_engine = $('#tts_engine').value;
    updates.tts_voice = $('#tts_voice').value;
    try {
      const r = await window.planner.apiFetch('/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates }),
      });
      const d = JSON.parse(r.text);
      if (!d.ok) errs.push(d.error || '保存失败');
      else {
        current = d.settings;
        // 通知主进程广播新设置给悬浮球/面板（语音播报开关等**运行中实时生效**——
        // 曾经漏掉这步：运行中关掉语音播报后，面板 voiceMode 的 ttsEnabled 仍是
        // true，回复继续被流式朗读，直到重启才停）
        window.planner.settingsSaved();
      }
    } catch {
      errs.push('后端不可用，压缩/LLM 设置未保存');
    }
    // 3) 代码工作区（独立端点；路径变了后端会清空授权规则并重建 agent）
    try {
      const ws = $('#workspace_root').value.trim();
      const r = await window.planner.apiFetch('/workspace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: ws }),
      });
      const d = JSON.parse(r.text);
      if (!d.ok) errs.push(d.error === 'not_a_directory' ? '工作区目录不存在' : (d.error || '工作区保存失败'));
      else $('#workspace_root').value = d.workspace || '';
    } catch {
      errs.push('后端不可用，工作区设置未保存');
    }
    if (errs.length) {
      $('#msg').textContent = errs.join('；');
    } else {
      $('#msg').textContent = '已保存并生效 ✓';
      $('#msg').className = 'ok';
    }
    btn.disabled = false;
  });

  loadSettings();

  // ── 代码工作区：选择目录 / 清除 ────────────────────────────
  $('#btn_pick_ws').addEventListener('click', async () => {
    const p = await window.planner.pickDirectory();
    if (p) $('#workspace_root').value = p;
  });
  $('#btn_clear_ws').addEventListener('click', () => {
    $('#workspace_root').value = '';
  });
  $('#perm-clear').addEventListener('click', async () => {
    if (!(await uiConfirm('清空全部「记住并允许」规则？'))) return;
    try {
      await window.planner.apiFetch('/permissions/clear', { method: 'POST' });
    } catch { /* 忽略 */ }
    await loadWorkspace();
  });

  // ── 试听：合成当前选中音色并播放（不保存）────────────
  const ttsAudio = document.getElementById('tts-audio');
  $('#btn_tts_test').addEventListener('click', async () => {
    const btn = $('#btn_tts_test');
    const voice = $('#tts_voice').value;
    if (!voice || btn.disabled) return;
    btn.disabled = true;
    btn.textContent = '合成中…';
    try {
      const r = await window.planner.apiFetch('/tts/say?text=' +
        encodeURIComponent('你好，我是小助，这是我的声音。') + '&voice=' + encodeURIComponent(voice));
      const d = JSON.parse(r.text);
      if (d.ok && d.url) {
        const fileUrl = await window.planner.audioFile(d.url);
        if (fileUrl) {
          ttsAudio.src = fileUrl;
          ttsAudio.onended = () => ttsAudio.removeAttribute('src');
          await ttsAudio.play();
        } else {
          $('#msg').textContent = '试听下载失败';
        }
      } else {
        $('#msg').textContent = '语音合成失败（' + (d.error || '') + '）';
      }
    } catch {
      $('#msg').textContent = '试听失败（后端不可用）';
    }
    btn.disabled = false;
    btn.textContent = '试听';
  });
})();
