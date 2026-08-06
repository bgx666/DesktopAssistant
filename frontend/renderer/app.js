// app.js —— 渲染进程：轮询 /dequeue + 聊天 / 今日计划 / 任务列表
(() => {
  'use strict';

  const API = (window.planner && window.planner.apiBase) || 'http://127.0.0.1:18771';

  const $ = (sel) => document.querySelector(sel);
  const state = { heartbeat: null, dnd: null, plan: null, lastPlanDate: '' };

  // ── HTTP ─────────────────────────────────────────────
  async function api(path, opts = {}) {
    const res = await fetch(API + path, opts);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // ── 聊天 ─────────────────────────────────────────────
  const messages = $('#messages');

  function addMessage(text, cls = 'assistant', msgId = null) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    const span = document.createElement('span');
    span.className = 'msg-text';
    span.textContent = text;
    el.appendChild(span);
    if (cls === 'user' && msgId) {
      const undo = document.createElement('button');
      undo.className = 'undo-btn';
      undo.textContent = '撤销';
      undo.title = '删除这条消息及其之后的对话';
      undo.addEventListener('click', async () => {
        undo.disabled = true;
        try {
          const r = await post('/undo', { msg_id: msgId });
          if (r.ok) loadHistory(true);
          else addMessage(r.error || '无法撤销', 'log');
        } catch {
          addMessage('撤销失败（后端未连接）', 'log');
        }
      });
      el.appendChild(undo);
    }
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
  }

  $('#input-bar').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = $('#input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    $('#send').disabled = true;
    try {
      const r = await post('/chat', { message: text });
      addMessage(text, 'user', r.msg_id || null);
    } catch (err) {
      addMessage('（连接后端失败，稍后重试）', 'log');
    }
    $('#send').disabled = false;
  });

  // ── 标题栏拖拽（仅 titlebar 本体，按钮区域除外）────────
  (function setupTitlebarDrag() {
    const bar = $('#titlebar');
    let dragging = false;
    let startX = 0, startY = 0, winX = 0, winY = 0;
    let moved = 0;
    bar.addEventListener('mousedown', async (e) => {
      if (e.button !== 0 || e.target.closest('button')) return;
      dragging = true;
      moved = 0;
      startX = e.screenX;
      startY = e.screenY;
      try {
        const pos = await window.planner.getPanelPos();
        winX = pos.x; winY = pos.y;
      } catch { /* 忽略 */ }
    });
    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const dx = e.screenX - startX;
      const dy = e.screenY - startY;
      moved = Math.max(moved, Math.abs(dx), Math.abs(dy));
      if (moved > 4) window.planner.movePanel(winX + dx, winY + dy);
    });
    document.addEventListener('mouseup', () => { dragging = false; });
  })();

  // ── 事件处理（/dequeue）──────────────────────────────
  // 流式：text_stream 逐字追加（msg_id 区分多轮）；text 完整文本已由
  // 流式渲染过，面板忽略（气泡 toast 用）。
  const streamMsgs = {};
  const toolCards = {};
  function handleEvent(ev) {
    if (ev.type === 'text_stream') {
      let el = streamMsgs[ev.msg_id];
      if (!el) {
        el = addMessage('', 'assistant');
        streamMsgs[ev.msg_id] = el;
      }
      el.textContent += ev.content;
      messages.scrollTop = messages.scrollHeight;
    } else if (ev.type === 'tool_call') {
      // 工具卡片：闪烁提示正在执行；点击展开参数与结果
      const card = document.createElement('div');
      card.className = 'tool-card running';
      const argsText = ev.args ? JSON.stringify(ev.args, null, 1) : '';
      card.innerHTML = `
        <div class="tool-head"><span class="tool-spinner"></span>正在执行 ${escapeHtml(ev.name)}</div>
        <div class="tool-detail">
          ${argsText ? `<div class="tool-sec">参数</div><pre class="tool-pre">${escapeHtml(argsText)}</pre>` : ''}
          <div class="tool-sec">结果</div><pre class="tool-pre tool-result">…</pre>
        </div>`;
      card.addEventListener('click', () => card.classList.toggle('expanded'));
      toolCards[ev.id] = card;
      messages.appendChild(card);
      messages.scrollTop = messages.scrollHeight;
    } else if (ev.type === 'tool_result') {
      const card = toolCards[ev.id];
      if (card) {
        card.classList.remove('running');
        card.classList.add('done');
        card.querySelector('.tool-head').innerHTML =
          `<span class="tool-check">✓</span>${escapeHtml(card.querySelector('.tool-head').textContent.replace(/正在执行/, '').trim())}`;
        const pre = card.querySelector('.tool-result');
        pre.textContent = ev.content;
        card.setAttribute('title', '点击展开');
      }
    } else if (ev.type === 'log') {
      if (ev.content) addMessage(ev.content, 'log');
    } else if (ev.type === 'thinking') {
      $('#thinking').classList.toggle('hidden', !ev.value);
      $('.dot').classList.toggle('thinking', !!ev.value);
    } else if (ev.type === 'dnd' || ev.type === 'plan_update') {
      refreshAll();
    }
  }

  // ── 轮询 ─────────────────────────────────────────────
  // 面板隐藏时暂停 /dequeue 轮询（由悬浮球窗口消费事件并弹气泡），
  // 避免两个窗口抢事件。panelState 提升到函数作用域（finally 里也要用）。
  async function poll() {
    let panelState = null;
    try {
      panelState = await window.planner.getPanelState();
      if (panelState === 'hidden') {
        setTimeout(poll, 600);
        return;
      }
      const data = await api('/dequeue');
      data.events.forEach(handleEvent);
      applyState(data.state);
      const plan = data.state && data.state.plan;
      if (plan && plan.today !== state.lastPlanDate) {
        state.lastPlanDate = plan.today;
        refreshPlan();
      }
    } catch {
      setOffline(true);
    } finally {
      if (panelState !== 'hidden') setTimeout(poll, 600);
    }
  }

  function applyState(s) {
    if (!s) return;
    setOffline(false);
    state.heartbeat = s.heartbeat;
    state.dnd = s.dnd;
    $('#btn-dnd').classList.toggle('on', !!(s.dnd && s.dnd.enabled && s.dnd.in_dnd));
    // 思考状态：以 /state 为准兜底纠正（thinking 事件可能在面板隐藏时被悬浮球消费）
    $('#thinking').classList.toggle('hidden', !s.thinking);
    $('.dot').classList.toggle('thinking', !!s.thinking);
    // 上下文 token 估算（字符数近似）
    const ctx = s.context;
    if (ctx) {
      $('#ctx-tokens').textContent = `约 ${ctx.tokens.toLocaleString()} token`;
      $('#ctx-tokens').classList.toggle('warn', ctx.tokens > 60000);
    } else {
      $('#ctx-tokens').textContent = '';
    }
    const hb = s.heartbeat;
    if (hb && hb.in_minutes > 0) {
      $('#heartbeat').textContent = hb.note
        ? `${hb.in_minutes} 分钟后：${hb.note}`
        : `${hb.in_minutes} 分钟后醒来`;
    } else {
      $('#heartbeat').textContent = '';
    }
    // 今日计划徽标（逾期数）
    const overdue = (s.plan && s.plan.overdue_count) || 0;
    const badge = $('#plan-badge');
    badge.classList.toggle('hidden', !overdue);
    badge.textContent = overdue;
  }

  function setOffline(off) {
    if (off) {
      $('#heartbeat').textContent = '后端未连接';
      $('#btn-dnd').classList.add('on');
    }
  }

  // ── Tab 切换 ─────────────────────────────────────────
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
      btn.classList.add('active');
      $('#' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'plan') refreshPlan();
      if (btn.dataset.tab === 'tasks') refreshTasks();
    });
  });

  function localDateStr(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  // ── 接下来（动态待办队列）──────────────────────────────
  async function refreshPlan() {
    const list = $('#plan-list');
    list.innerHTML = '<div class="empty-tip">加载中…</div>';
    try {
      const data = await api('/next');
      const queue = data.queue || [];
      $('#plan-date').textContent = '待办队列（按紧急度排序）';
      $('#plan-progress').textContent = `${queue.length} 项未完成`;
      if (!queue.length) {
        list.innerHTML = '<div class="empty-tip">没有待办，告诉小助你想做什么</div>';
        return;
      }
      list.innerHTML = '';
      queue.forEach((p, idx) => {
        const card = document.createElement('div');
        card.className = 'plan-card';
        const due = p.task_due ? `<span class="p-due">截止 ${escapeHtml(p.task_due)}</span>` : '';
        card.innerHTML = `
          <span class="p-rank">${idx + 1}</span>
          <div class="p-content">
            <div class="p-task">${escapeHtml(p.task_title)}${p.phase_title ? ' · ' + escapeHtml(p.phase_title) : ''}</div>
            <div>${escapeHtml(p.content)}</div>
            ${due}
          </div>
          <input type="checkbox" data-id="${p.id}" title="做完了勾选" />`;
        card.querySelector('input').addEventListener('change', async (e) => {
          try {
            await post('/plan/done', { plan_id: p.id });
            refreshPlan();
          } catch { /* 忽略 */ }
        });
        list.appendChild(card);
      });
    } catch {
      list.innerHTML = '<div class="empty-tip">后端未连接</div>';
    }
  }

  // ── 任务列表 ─────────────────────────────────────────
  async function refreshTasks() {
    const list = $('#task-list');
    list.innerHTML = '<div class="empty-tip">加载中…</div>';
    try {
      const data = await api('/tasks');
      const tasks = data.tasks || [];
      if (!tasks.length) {
        list.innerHTML = '<div class="empty-tip">还没有任务，告诉小助你的目标吧</div>';
        return;
      }
      list.innerHTML = '';
      tasks.forEach((t) => {
        const card = document.createElement('div');
        card.className = 'task-card';
        const statusCls = t.status === 'done' ? 'done' : (isOverdue(t) ? 'overdue' : '');
        const statusText = t.status === 'done' ? '完成' : (isOverdue(t) ? '已逾期' : statusTextCn(t.status));
        let planHtml = '';
        if (t.plan_items && t.plan_items.length) {
          planHtml = '<ul class="t-plan">' + t.plan_items
            .map((p) => `<li class="${p.status === 'done' ? 'done' : ''}">${p.date} ${escapeHtml(p.content)}</li>`)
            .join('') + '</ul>';
        }
        card.innerHTML = `
          <div class="t-head">
            <span class="t-title">${escapeHtml(t.title)}</span>
            <span class="t-status ${statusCls}">${statusText}</span>
          </div>
          <div class="t-meta">截止 ${t.due_date || '未定'} · 进度 ${t.plan_done}/${t.plan_total} · 优先级 ${t.priority}</div>
          ${planHtml}`;
        list.appendChild(card);
      });
    } catch {
      list.innerHTML = '<div class="empty-tip">后端未连接</div>';
    }
  }

  function isOverdue(t) {
    if (!t.due_date || t.status === 'done') return false;
    return t.due_date < localDateStr(new Date());
  }

  function statusTextCn(s) {
    return { todo: '待开始', in_progress: '进行中', abandoned: '已放弃' }[s] || s;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── 免打扰按钮 ───────────────────────────────────────
  $('#btn-dnd').addEventListener('click', async () => {
    const next = !(state.dnd && state.dnd.enabled);
    try {
      await post('/dnd', { enabled: next });
      applyState(await (await fetch(API + '/state')).json());
    } catch { /* 忽略 */ }
  });

  // ── 收起（回到悬浮球）──────────────────────────────────────
  $('#btn-fold').addEventListener('click', () => {
    window.planner.hidePanel(); // 走 IPC 变形收回（不依赖 window.close 链路）
  });

  // ── 历史消息（重启恢复 / 面板重新可见时补渲染）────────────
  // 面板隐藏期间小助说的话只冒了气泡，展开面板时要补进对话框。
  async function loadHistory(clear = true) {
    try {
      const data = await api('/history');
      if (clear) messages.innerHTML = '';
      (data.messages || []).forEach((m) => {
        addMessage(m.content, m.role === 'assistant' ? 'assistant' : 'user', m.id || null);
      });
    } catch { /* 后端未连接则跳过 */ }
  }

  // 主进程通知：面板变形展开完成 → 重新加载历史（含隐藏期间的气泡消息）
  window.planner.onPanelShown(() => loadHistory(true));

  // ── 启动 ─────────────────────────────────────────────
  (async () => {
    try {
      const data = await api('/state');
      applyState(data.state);
      state.lastPlanDate = (data.state.plan || {}).today || '';
      refreshPlan();
      refreshTasks();
    } catch {
      setOffline(true);
    }
    loadHistory();
    poll();
  })();
})();
