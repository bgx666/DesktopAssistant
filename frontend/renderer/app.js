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

  // ── 回到最新消息按钮 ──────────────────────────────────
  // 向上翻历史时显示，点击平滑滚回底部（类主流 AI 对话界面）。
  const jumpBtn = $('#jump-bottom');
  messages.addEventListener('scroll', () => {
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    jumpBtn.classList.toggle('hidden', nearBottom);
  });
  jumpBtn.addEventListener('click', () => {
    messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' });
  });

  function addMessage(text, cls = 'assistant', msgId = null) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    if (cls === 'assistant') {
      // 小助头像：与悬浮球同款渐变（CSS 绘制，无需图片资源）
      const avatar = document.createElement('div');
      avatar.className = 'avatar';
      el.appendChild(avatar);
    }
    const bubble = document.createElement('div');
    bubble.className = 'bubble ' + cls;   // bubble user / bubble assistant（颜色区分）
    if (cls === 'assistant' || cls === 'memory') {
      // LLM 生成内容渲染轻量 markdown（加粗/代码）；用户消息保持原文
      bubble.innerHTML = renderMarkdown(text);
    } else {
      bubble.textContent = text;
    }
    el.appendChild(bubble);
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

  // 工具卡片（实时转圈 / 历史已完成共用）：addToolCard 创建，
  // setToolResult 填充结果。
  function addToolCard(name, args, id, done) {
    const card = document.createElement('div');
    card.className = done ? 'tool-card done' : 'tool-card running';
    const argsText = args ? JSON.stringify(args, null, 1) : '';
    card.innerHTML = `
      <div class="tool-head">${done ? '<span class="tool-check">✓</span>' : '<span class="tool-spinner"></span>正在执行 '}${escapeHtml(name)}</div>
      <div class="tool-detail">
        ${argsText ? `<div class="tool-sec">参数</div><pre class="tool-pre">${escapeHtml(argsText)}</pre>` : ''}
        <div class="tool-sec">结果</div><pre class="tool-pre tool-result">…</pre>
      </div>`;
    card.addEventListener('click', () => card.classList.toggle('expanded'));
    if (id) toolCards[id] = card;
    messages.appendChild(card);
    messages.scrollTop = messages.scrollHeight;
    return card;
  }

  function setToolResult(id, content) {
    const card = toolCards[id];
    if (!card) return;
    card.classList.remove('running');
    card.classList.add('done');
    const head = card.querySelector('.tool-head');
    head.innerHTML =
      `<span class="tool-check">✓</span>${escapeHtml(head.textContent.replace(/正在执行/, '').trim())}`;
    const pre = card.querySelector('.tool-result');
    pre.textContent = content;
    card.setAttribute('title', '点击展开');
  }

  // ── 轻量 Markdown 渲染（加粗 + 行内代码 + 代码块）────────
  // 先 escapeHtml 再替换标记（XSS 安全）；代码块内容先占位避免被加粗规则误伤。
  function renderMarkdown(text) {
    let html = escapeHtml(String(text == null ? '' : text));
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

  function handleEvent(ev) {
    if (ev.type === 'text_stream') {
      let el = streamMsgs[ev.msg_id];
      if (!el) {
        el = addMessage('', 'assistant');
        streamMsgs[ev.msg_id] = el;
      }
      const bubble = el.querySelector('.bubble');
      if (bubble) bubble.textContent += ev.content;
      else el.textContent += ev.content;
      messages.scrollTop = messages.scrollHeight;
    } else if (ev.type === 'text') {
      // 流式收束（完整文本事件）：对最新一条 assistant 消息整段渲染 markdown
      const last = [...messages.querySelectorAll('.msg.assistant')].pop();
      if (last) {
        const b = last.querySelector('.bubble');
        if (b && !b.querySelector('strong, code, pre')) b.innerHTML = renderMarkdown(b.textContent);
      }
    } else if (ev.type === 'tool_call') {
      addToolCard(ev.name, ev.args, ev.id, false);
    } else if (ev.type === 'tool_result') {
      setToolResult(ev.id, ev.content);
    } else if (ev.type === 'log') {
      if (ev.content) addMessage(ev.content, 'log');
    } else if (ev.type === 'thinking') {
      $('#thinking').classList.toggle('hidden', !ev.value);
      $('.dot').classList.toggle('thinking', !!ev.value);
    } else if (ev.type === 'dnd' || ev.type === 'plan_update') {
      refreshPlan();
      refreshTasks();
    } else if (ev.type === 'memory_update') {
      pendingMemoryReload = true;   // 记忆树有变化，生成结束后重载历史
    }
  }

  // ── 事件与状态 ─────────────────────────────────────────
  // /dequeue 与 /state 都由主进程独占轮询后经 IPC 推送（events / state），
  // 面板不再各自轮询，减少 HTTP 请求。
  // 压缩发生（memory_update）时标记待重载：生成结束（thinking false）后
  // 自动重载历史，对话框与 buffer 同步（不打断流式）。
  let pendingMemoryReload = false;
  window.planner.onEvents((events) => {
    events.forEach(handleEvent);
  });

  window.planner.onState(applyState);

  function applyState(s) {
    if (!s) return;
    if (s.offline) {
      setOffline(true);
      return;
    }
    setOffline(false);
    state.heartbeat = s.heartbeat;
    state.dnd = s.dnd;
    $('#btn-dnd').classList.toggle('on', !!(s.dnd && s.dnd.enabled && s.dnd.in_dnd));
    // 思考状态：以推送的 state 为准兜底纠正（thinking 事件可能在面板隐藏时被悬浮球消费）
    const thinking = !!s.thinking;
    $('#thinking').classList.toggle('hidden', !thinking);
    $('.dot').classList.toggle('thinking', thinking);
    // 停止按钮：生成中显示
    $('#btn-stop').classList.toggle('hidden', !thinking);
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
    // 计划日期变化 → 刷新待办队列
    const plan = s.plan;
    if (plan && plan.today && plan.today !== state.lastPlanDate) {
      state.lastPlanDate = plan.today;
      refreshPlan();
    }
    // 生成结束且压缩过 → 重载历史（对话框与 buffer 同步，新节点出现）
    if (!s.thinking && pendingMemoryReload) {
      pendingMemoryReload = false;
      loadHistory(true);
    }
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
  // 面板隐藏期间小助说的话只冒了气泡，展开面板时要补进对话框；
  // 历史工具调用渲染为"已完成"卡片（静态，不转圈）。
  async function loadHistory(clear = true) {
    try {
      const data = await api('/history');
      if (clear) {
        messages.innerHTML = '';
        Object.keys(toolCards).forEach((k) => delete toolCards[k]);
        Object.keys(streamMsgs).forEach((k) => delete streamMsgs[k]);
      }
      (data.messages || []).forEach((m) => {
        if (m.role === 'tool_call') {
          addToolCard(m.name, m.args, m.id, true);
        } else if (m.role === 'tool_result') {
          setToolResult(m.id, m.content);
        } else {
          const cls = m.role === 'assistant' ? 'assistant' : m.role === 'memory' ? 'memory' : 'user';
          addMessage(m.content, cls, m.id || null);
        }
      });
    } catch { /* 后端未连接则跳过 */ }
  }

  // 主进程通知：面板变形展开完成 → 重新加载历史（含隐藏期间的气泡消息）
  window.planner.onPanelShown(() => {
    loadHistory(true);
    // 展开完成：聚焦输入框，可直接打字
    setTimeout(() => { try { $('#input').focus(); } catch { /* 忽略 */ } }, 80);
  });

  // ── 停止按钮：生成中点一下打断小助 ─────────────────────
  $('#btn-stop').addEventListener('click', async () => {
    $('#btn-stop').disabled = true;
    try {
      await post('/stop');
    } catch { /* 忽略 */ }
    setTimeout(() => { $('#btn-stop').disabled = false; }, 300);
  });

  // ── 右键复制选中文字（气泡内容已可选中）──────────────
  function showCopiedTip() {
    const tip = document.createElement('div');
    tip.className = 'copied-tip';
    tip.textContent = '已复制';
    document.body.appendChild(tip);
    setTimeout(() => tip.remove(), 1200);
  }

  document.addEventListener('contextmenu', (e) => {
    const sel = window.getSelection();
    const text = sel ? sel.toString().trim() : '';
    if (!text) return;                       // 无选中文本 → 不拦截（输入框等默认行为）
    e.preventDefault();
    // 先试 execCommand（同步可靠），失败降级 clipboard API
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch { /* 降级 */ }
    if (!copied) {
      navigator.clipboard.writeText(text).catch(() => { /* 忽略 */ });
    }
    showCopiedTip();
  });

  document.addEventListener('keydown', (e) => {
    // 全局 Ctrl+C 兜底（选中文本时原生已处理，此处确保无焦点时也可用）
    if (e.ctrlKey && (e.key === 'c' || e.key === 'C')) {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : '';
      if (text && document.activeElement && document.activeElement.tagName !== 'INPUT') {
        e.preventDefault();
        try {
          document.execCommand('copy');
          showCopiedTip();
        } catch { /* 忽略 */ }
      }
    }
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
    loadHistory();
  })();
})();
