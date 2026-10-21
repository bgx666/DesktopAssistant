// quick-input.js —— 悬浮球旁的快速输入浮层（全局快捷键弹出）
// 输入 + 语音 + 发送 + 待发送文件；Enter 发送、Shift+Enter 换行、Esc 收起。
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const input = $('#quick-input');
  const sendBtn = $('#quick-send');
  const micBtn = $('#quick-mic');
  const filesBox = $('#quick-files');
  const statusEl = $('#quick-status');

  let recState = 'idle';   // idle | starting | recording
  let recStop = null;

  // ── 窗口高度跟随内容（主进程按"贴球那侧固定"调整）──
  let lastH = 0;
  function fitWindow() {
    requestAnimationFrame(() => {
      const h = Math.ceil($('#quick').offsetHeight + 8);
      if (Math.abs(h - lastH) < 2) return;
      lastH = h;
      window.planner.quickResize(h);
    });
  }

  function setStatus(text) {
    statusEl.textContent = text || '';
    statusEl.classList.toggle('hidden', !text);
    fitWindow();
  }

  function autoResizeInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(108, Math.max(32, input.scrollHeight)) + 'px';
    fitWindow();
  }

  // ── 待发送文件（全局挂载，与面板/悬浮球共用）──
  function renderFiles(list) {
    const files = Array.isArray(list) ? list : [];
    filesBox.classList.toggle('hidden', !files.length);
    filesBox.textContent = '';
    files.forEach((f, i) => {
      const chip = document.createElement('span');
      chip.className = 'qf-chip';
      chip.textContent = f.name || '未命名';
      chip.title = f.path || '';
      const x = document.createElement('button');
      x.type = 'button';
      x.textContent = '×';
      x.title = '移除';
      x.addEventListener('click', (e) => {
        e.stopPropagation();
        window.planner.removeMounted(i);
      });
      chip.appendChild(x);
      filesBox.appendChild(chip);
    });
    fitWindow();
  }
  window.planner.onMountedChanged(renderFiles);
  window.planner.getMounted().then(renderFiles).catch(() => {});

  // 拖文件进浮层 → 挂载（与面板同一套 mounted 机制）
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', async (e) => {
    e.preventDefault();
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    const list = await window.fileDrop.handleFiles(e.dataTransfer.files);
    if (list.length) window.planner.mountFiles(list);
  });

  // ── 发送 ──
  async function send() {
    const text = input.value.trim();
    if (!text || sendBtn.disabled) { input.focus(); return; }
    sendBtn.disabled = true;
    setStatus('');
    try {
      const mounted = await window.planner.getMounted();
      const r = await window.planner.apiFetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, files: mounted }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      if (mounted && mounted.length) window.planner.clearMounted();
      input.value = '';
      autoResizeInput();
      window.planner.hideQuickInput();
    } catch {
      setStatus('连接后端失败，稍后重试');
    }
    sendBtn.disabled = false;
  }
  sendBtn.addEventListener('click', send);

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      window.planner.hideQuickInput();
    }
  });
  input.addEventListener('input', autoResizeInput);

  // ── 语音输入：点一下开始录音，再点一下结束 → /asr → 填进输入框 ──
  async function toggleMic() {
    if (recState === 'starting') return;
    if (recState === 'recording') {
      recState = 'idle';
      micBtn.classList.remove('recording');
      const stop = recStop;
      recStop = null;
      let wav = null;
      try { wav = await stop(false); } catch { wav = null; }
      if (!wav) { setStatus('（没有录到声音）'); return; }
      try {
        const r = await window.planner.apiFetch('/asr', {
          method: 'POST',
          headers: { 'Content-Type': 'audio/wav' },
          body: wav,
        });
        const d = JSON.parse(r.text);
        if (d.ok && d.text) {
          input.value = (input.value ? input.value.replace(/\s+$/, '') + ' ' : '') + d.text;
          autoResizeInput();
          input.focus();
        } else {
          setStatus('语音识别失败');
        }
      } catch {
        setStatus('语音识别失败（后端不可用）');
      }
      return;
    }
    recState = 'starting';
    micBtn.classList.add('recording');
    setStatus('');
    try {
      recStop = await window.mic.begin();
      recState = 'recording';
    } catch {
      recState = 'idle';
      recStop = null;
      micBtn.classList.remove('recording');
      setStatus('无法使用麦克风：检查系统权限/设备');
    }
  }
  micBtn.addEventListener('click', toggleMic);

  // ── 显示/收起 ──
  window.planner.onQuickShow(() => {
    setStatus('');
    input.value = '';
    autoResizeInput();
    input.focus();
    window.planner.getMounted().then(renderFiles).catch(() => {});
  });
  // 收起时若还在录音 → 丢弃，别让麦克风一直占着
  window.planner.onQuickHide(() => {
    if (recState === 'recording' && recStop) {
      try { recStop(true); } catch { /* 忽略 */ }
    }
    recState = 'idle';
    recStop = null;
    micBtn.classList.remove('recording');
  });

  // 键盘焦点兜底：点浮层任意处都重申一次焦点（Windows 焦点丢失后能打字）
  document.addEventListener('mousedown', () => window.planner.refocusWindow(), true);
})();
