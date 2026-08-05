// app.js —— 渲染进程：轮询 /dequeue + 聊天 / 今日计划 / 任务列表
(() => {
  'use strict';

  const API = 'http://127.0.0.1:18771';

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

  function addMessage(text, cls = 'assistant') {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = text;
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
    addMessage(text, 'user');
    $('#send').disabled = true;
    try {
      await post('/chat', { message: text });
    } catch (err) {
      addMessage('（连接后端失败，稍后重试）', 'log');
    }
    $('#send').disabled = false;
  });

  // ── 事件处理（/dequeue）──────────────────────────────
  function handleEvent(ev) {
    if (ev.type === 'text') addMessage(ev.content, 'assistant');
    else if (ev.type === 'log') addMessage(ev.content, 'log');
    else if (ev.type === 'thinking') {
      $('#thinking').classList.toggle('hidden', !ev.value);
      $('.dot').classList.toggle('thinking', !!ev.value);
    } else if (ev.type === 'dnd' || ev.type === 'plan_update') {
      refreshAll();
    }
  }

  // ── 轮询 ─────────────────────────────────────────────
  async function poll() {
    try {
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
      setTimeout(poll, 600);
    }
  }

  function applyState(s) {
    if (!s) return;
    setOffline(false);
    state.heartbeat = s.heartbeat;
    state.dnd = s.dnd;
    $('#btn-dnd').classList.toggle('on', !!(s.dnd && s.dnd.enabled && s.dnd.in_dnd));
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

  // ── 今日计划 ─────────────────────────────────────────
  async function refreshPlan() {
    const list = $('#plan-list');
    list.innerHTML = '<div class="empty-tip">加载中…</div>';
    try {
      const dateStr = localDateStr(new Date());
      const data = await api('/plan?date=' + dateStr);
      const plan = data.plan || [];
      const done = plan.filter((p) => p.status === 'done').length;
      $('#plan-date').textContent = dateStr;
      $('#plan-progress').textContent = `${done}/${plan.length} 完成`;
      if (!plan.length) {
        list.innerHTML = '<div class="empty-tip">今天还没有安排，告诉小助你想做什么</div>';
        return;
      }
      list.innerHTML = '';
      plan.forEach((p) => {
        const card = document.createElement('div');
        card.className = 'plan-card' + (p.status === 'done' ? ' done' : '');
        card.innerHTML = `
          <input type="checkbox" ${p.status === 'done' ? 'checked' : ''} data-id="${p.id}" />
          <div class="p-content">
            ${p.phase_title ? `<div class="p-task">${escapeHtml(p.task_title)} · ${escapeHtml(p.phase_title)}</div>` : ''}
            <div>${escapeHtml(p.content)}</div>
          </div>`;
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
    poll();
  })();
})();
