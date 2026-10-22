// pending.js —— 悬浮球旁的待处理请求卡片（审批 / ask_user 提问）
// 面板隐藏时由主进程弹出：球体黄灯呼吸 + 这张卡片；可在这里直接
// 允许/拒绝、选选项、打字回答；复杂授权（记住并允许等）走「展开面板」。
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  let cur = { approval: null, question: null };
  let timer = null;
  let deadline = 0;

  function fit() {
    requestAnimationFrame(() => {
      window.planner.pendingResize(Math.ceil($('#pending').offsetHeight + 8));
    });
  }

  function post(path, body) {
    return window.planner.apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  function renderPaths(paths) {
    const box = $('#pn-paths');
    box.innerHTML = '';
    const list = Array.isArray(paths) ? paths : [];
    list.slice(0, 3).forEach((p) => {
      const div = document.createElement('div');
      div.className = 'pn-path';
      div.textContent = p;
      div.title = p;
      box.appendChild(div);
    });
    if (list.length > 3) {
      const more = document.createElement('div');
      more.className = 'pn-path';
      more.textContent = `… 还有 ${list.length - 3} 个文件`;
      box.appendChild(more);
    }
  }

  function renderOptions(options) {
    const box = $('#pn-options');
    box.innerHTML = '';
    (Array.isArray(options) ? options : []).forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pn-opt';
      btn.textContent = opt;
      btn.addEventListener('click', () => resolveQuestion({ selected: opt }));
      box.appendChild(btn);
    });
  }

  function startCountdown(timeoutSec, isApproval) {
    if (timer) { clearInterval(timer); timer = null; }
    const note = $('#pn-note');
    deadline = timeoutSec > 0 ? Date.now() + timeoutSec * 1000 : 0;
    const tick = () => {
      if (!deadline) { note.textContent = ''; return; }
      const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
      note.textContent = left > 0
        ? `${left} 秒内不处理将自动${isApproval ? '拒绝' : '跳过'}`
        : '（等待超时…）';
      if (left <= 0 && timer) { clearInterval(timer); timer = null; }
    };
    tick();
    timer = setInterval(tick, 1000);
  }

  function render(p) {
    cur = p || { approval: null, question: null };
    const ap = cur.approval || null;
    const q = cur.question || null;
    $('#pn-approval').classList.toggle('hidden', !ap);
    $('#pn-question').classList.toggle('hidden', !q);
    $('#pn-title').textContent = ap
      ? (ap.agent === 'white' ? 'White 需要你确认' : '需要你确认')
      : (q ? '小助想问你' : '');
    if (ap) {
      $('#pn-tool').textContent = ap.tool || '';
      renderPaths(ap.paths);
    }
    if (q) {
      $('#pn-qtext').textContent = q.question || '';
      renderOptions(q.options);
      $('#pn-answer').value = '';
    }
    if (ap || q) startCountdown((ap || q).timeout || 0, !!ap);
    fit();
  }

  async function resolveApproval(decision) {
    const id = cur.approval && cur.approval.id;
    if (!id) return;
    try {
      await post('/approval', { id, decision, scope: '' });
    } catch { /* 后端不可用：等超时 */ }
    window.planner.closePending();
  }

  async function resolveQuestion(payload) {
    const id = cur.question && cur.question.id;
    if (!id) return;
    const body = { id, selected: payload.selected || '' };
    if (payload.answer) body.answer = payload.answer;
    try {
      await post('/question', body);
    } catch { /* 后端不可用：等超时 */ }
    window.planner.closePending();
  }

  $('#pn-allow').addEventListener('click', () => resolveApproval('allow_once'));
  $('#pn-deny').addEventListener('click', () => resolveApproval('deny'));
  $('#pn-expand-a').addEventListener('click', () => window.planner.expandPending());
  $('#pn-expand-q').addEventListener('click', () => window.planner.expandPending());
  $('#pn-close').addEventListener('click', () => window.planner.closePending());

  const answerEl = $('#pn-answer');
  const sendAnswer = () => {
    const v = answerEl.value.trim();
    if (v) resolveQuestion({ answer: v });
  };
  $('#pn-send').addEventListener('click', sendAnswer);
  answerEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); sendAnswer(); }
    else if (e.key === 'Escape') { e.preventDefault(); window.planner.closePending(); }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') window.planner.closePending();
  });

  // 键盘焦点兜底：点卡片任意处都重申一次焦点（Windows 焦点丢失后能打字）
  document.addEventListener('mousedown', () => window.planner.refocusWindow(), true);

  window.planner.onPending(render);
  window.planner.getPending().then(render).catch(() => {});
})();
