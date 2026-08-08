// bubble.js —— 悬浮球：单击显示最近回复 / 长按说话 / 拖拽 / 右键菜单
// 交互约定：
// - 左键按住 <200ms 松开（单击）→ 显示模型最近回答的一条消息（气泡）
// - 左键按住 ≥200ms → 进入语音输入（变红即开录，麦克风流常驻零延迟），松开发送
// - 移动 ≥6px → 拖动球（取消录音）；右键 → 菜单（不变）
// 说明：getUserMedia 实测首次 1.3s / 之后 0.4~0.6s，MediaRecorder 0ms——
// mic.js 保持常驻流，录音几乎零延迟；变红与录音同步（流未就绪不提前变红）。
(() => {
  'use strict';

  const core = document.getElementById('core');

  // ── 设置：长按判定时间（主进程本地配置，启动即用，无需等后端）──
  let pressMs = 200;
  function applySettings(s) {
    if (s && s.press_ms) {
      pressMs = Math.max(50, Math.min(5000, parseInt(s.press_ms, 10) || 200));
    }
  }
  window.planner.onUiSettings(applySettings);
  window.planner.getUiSettings().then(applySettings).catch(() => {});

  // ── 手动拖拽（不用 -webkit-app-region，透明窗口上它吞点击事件）──
  let dragging = false;
  let startX = 0, startY = 0;
  let winX = 0, winY = 0;
  let lastMoveX = 0, lastMoveY = 0;
  let pressTimer = null;     // 长按判定定时器（时长来自设置，默认 200ms）
  let longPress = false;     // 已进入长按（录音）状态
  let micStop = null;        // 录音停止函数（麦克风就绪后赋值）
  let micPromise = null;     // begin() 的 Promise（预启动，可能未就绪）

  // ── 录音雷达环：12 顶点 12 边形（雷达图效果）——
  //    每顶点沿圆周、径向随音量起伏，边直线连接（直来直去），
  //    赛博朋克三色循环描边（青/品红/紫）+ 顶点小圆点同色 ──
  const ring = document.getElementById('sound-ring');
  const RING_N = 12;                // 顶点数
  const RING_CX = 50, RING_CY = 50; // 视图中心（100×100 视口，与球心重合）
  const RING_R = 21;                // 顶部半径（球边缘）
  const RING_MIN = 8;               // 底部半径（环内缘，不缩到圆心）
  const CYBER_COLORS = ['#6fd8cf', '#e59cc0', '#a99ae8'];  // 低饱和赛博：柔青/柔粉/柔紫
  let ringRaf = null;
  let ringEdges = [];
  let ringPts = [];

  function initRingSpokes() {
    const g = document.getElementById('ring-spokes');
    g.textContent = '';
    for (let i = 0; i < 8; i++) {
      const ang = (i / 8) * Math.PI * 2;
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', RING_CX);
      line.setAttribute('y1', RING_CY);
      line.setAttribute('x2', (RING_CX + RING_R * Math.cos(ang)).toFixed(1));
      line.setAttribute('y2', (RING_CY + RING_R * Math.sin(ang)).toFixed(1));
      g.appendChild(line);
    }
  }
  initRingSpokes();

  function initRingEdges() {
    const g = document.getElementById('ring-edges');
    g.textContent = '';
    ringEdges = [];
    for (let i = 0; i < RING_N; i++) {
      const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      l.setAttribute('stroke', CYBER_COLORS[i % CYBER_COLORS.length]);
      l.setAttribute('stroke-width', '2');
      l.setAttribute('stroke-linecap', 'round');
      g.appendChild(l);
      ringEdges.push(l);
    }
  }
  initRingEdges();

  function initRingPoints() {
    const g = document.getElementById('ring-points');
    g.textContent = '';
    ringPts = [];
    for (let i = 0; i < RING_N; i++) {
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('r', '1.8');
      c.setAttribute('fill', CYBER_COLORS[i % CYBER_COLORS.length]);
      g.appendChild(c);
      ringPts.push(c);
    }
  }
  initRingPoints();

  function startSoundRing() {
    if (ringRaf) return;
    window.mic.getAnalyser().then((analyser) => {
      if (!analyser || !longPress) return;
      const data = new Uint8Array(analyser.frequencyBinCount);
      ring.classList.add('active');
      const step = () => {
        if (!longPress) { stopSoundRing(); return; }
        analyser.getByteFrequencyData(data);
        let sum = 0;
        for (let i = 0; i < 48; i++) sum += data[i];          // 低频段平均音量
        const vol = sum / (48 * 255);
        const t = Date.now() / 200;
        // 12 顶点：半径在环内缘（底部）与球边缘（顶部）之间随音量跳动
        const coords = [];
        for (let i = 0; i < RING_N; i++) {
          const ang = (i / RING_N) * Math.PI * 2;
          const wave = vol * (0.5 + 0.5 * Math.sin(t + i * 0.9));
          const r = RING_MIN + wave * (RING_R - RING_MIN);   // 8 ~ 21
          coords.push({
            x: RING_CX + r * Math.cos(ang),
            y: RING_CY + r * Math.sin(ang),
          });
        }
        for (let i = 0; i < RING_N; i++) {
          const a = coords[i];
          const b = coords[(i + 1) % RING_N];
          const e = ringEdges[i];
          e.setAttribute('x1', a.x.toFixed(1));
          e.setAttribute('y1', a.y.toFixed(1));
          e.setAttribute('x2', b.x.toFixed(1));
          e.setAttribute('y2', b.y.toFixed(1));
          const p = ringPts[i];
          p.setAttribute('cx', a.x.toFixed(1));
          p.setAttribute('cy', a.y.toFixed(1));
        }
        ringRaf = requestAnimationFrame(step);
      };
      ringRaf = requestAnimationFrame(step);
    });
  }

  function stopSoundRing() {
    if (ringRaf) { cancelAnimationFrame(ringRaf); ringRaf = null; }
    ring.classList.remove('active');
    ringEdges.forEach((e) => {
      e.removeAttribute('x1'); e.removeAttribute('y1');
      e.removeAttribute('x2'); e.removeAttribute('y2');
    });
    ringPts.forEach((c) => { c.removeAttribute('cx'); c.removeAttribute('cy'); });
  }

  core.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    dragging = true;
    longPress = false;
    startX = e.screenX;
    startY = e.screenY;
    lastMoveX = 0;
    lastMoveY = 0;
    window.planner.getBubblePos().then((pos) => {
      winX = pos.x;
      winY = pos.y;
    });
    // 预启动麦克风（异步初始化）。就绪后总是挂载录音停止函数——
    // 即使判定后已松开（长按但麦克风就绪较慢）也不能丢弃：
    // 丢弃只由单击/拖动路径的 cancelMic() 负责，保证长按语音不丢。
    micPromise = window.mic.begin().then((stop) => {
      micStop = stop;
      return stop;          // 交给 mouseup 长按路径取用（就绪晚于松开时）
    }).catch(() => {
      micStop = null;
      return null;
    });
    // 长按未松开 → 判定为长按：点变红 + 启动音量环（与录音同步，不丢语音）
    pressTimer = setTimeout(() => {
      pressTimer = null;
      longPress = true;
      core.classList.add('recording');
      startSoundRing();
    }, pressMs);
  });

  function cancelMic() {
    core.classList.remove('recording');
    stopSoundRing();
    if (micStop) {
      const s = micStop;
      micStop = null;
      s(true);                               // 取消（丢弃录音）
    }
    const p = micPromise;
    micPromise = null;
    if (p) {
      p.then((s2) => { if (s2) s2(true); }).catch(() => {});   // 未就绪：就绪后立即丢弃
    }
  }

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.screenX - startX;
    const dy = e.screenY - startY;
    if (Math.abs(dx - lastMoveX) < 2 && Math.abs(dy - lastMoveY) < 2) return;
    lastMoveX = dx;
    lastMoveY = dy;
    // 位移超过 6px：放弃单击/长按说话，改为拖动（无论麦克风是否已就绪，
    // 都立即退出录音态——红点/波形环/录音一并取消，拖动中不残留特效）
    if (Math.abs(dx) >= 6 || Math.abs(dy) >= 6) {
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      if (longPress || micStop) {
        longPress = false;
        cancelMic();
      }
    }
    window.planner.moveBubble(winX + dx, winY + dy);
  });

  document.addEventListener('mouseup', async (e) => {
    if (!dragging) return;
    dragging = false;
    const dx = e.screenX - startX;
    const dy = e.screenY - startY;
    const isClick = Math.abs(dx) < 6 && Math.abs(dy) < 6;
    core.classList.remove('recording');
    stopSoundRing();
    if (!isClick) {
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      cancelMic();
      return;
    }
    if (!longPress) {
      // 单击：取消麦克风，显示模型最近回答的一条消息
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      cancelMic();
      showLastReply();
      return;
    }
    // 长按结束：等麦克风就绪 → 结束录音 → 识别 → 直接发送
    const p = micPromise;
    micPromise = null;
    const s = micStop;
    micStop = null;
    let wav = null;
    if (s) {
      wav = await s(false);
    } else if (p) {
      try {
        const s2 = await p;                  // 就绪前松开：拿到即结束（可能无录音）
        wav = s2 ? await s2(false) : null;
      } catch {
        wav = null;
      }
    }
    if (!wav) return;
    try {
      const base = window.planner.apiBase;
      const r = await fetch(base + '/asr', {
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav' },
        body: wav,
        signal: AbortSignal.timeout(60000),
      });
      const d = await r.json();
      if (d.ok && d.text) {
        // 语音文本 + 全局挂载文件一起发送；发送后清空挂载
        const mounted = await window.planner.getMounted();
        await fetch(base + '/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: d.text, files: mounted }),
        });
        if (mounted && mounted.length) window.planner.clearMounted();
      }
    } catch { /* 静默 */ }
  });

  // 单击 → 依次显示历史回复（第 1 次最新、第 2 次倒数第二…，循环）
  // 历史气泡从顶部开始向下堆叠（先点的在顶，更久远依次落在下面）
  // 若上一批气泡已过期消失（>气泡生命周期），重新从最新一条开始翻
  let replyOffset = 0;       // 距最新的偏移：0=最新
  let lastReplyAt = 0;       // 上次单击时间戳
  const REPLY_LIFE_MS = 30000;   // 与主进程 TOAST_MS 一致（气泡 30 秒生存）
  // 气泡被清空（右键"清除气泡"等）→ 重置偏移，下次单击从最近一条开始
  window.planner.onToastsCleared(() => { replyOffset = 0; });
  async function showLastReply() {
    const now = Date.now();
    if (now - lastReplyAt > REPLY_LIFE_MS) replyOffset = 0;   // 旧气泡已过期 → 重新从最新
    lastReplyAt = now;
    try {
      const r = await fetch(window.planner.apiBase + '/history', { signal: AbortSignal.timeout(5000) });
      const d = await r.json();
      const replies = (d.messages || []).filter((m) => m.role === 'assistant' && m.content);
      if (!replies.length) { replyOffset = 0; return; }
      if (replyOffset >= replies.length) replyOffset = 0;   // 到头循环回最新
      const off = replyOffset;
      const m = replies[replies.length - 1 - off];
      replyOffset = off + 1;
      window.planner.showToast(m.content, off, true);
    } catch { /* 后端不可用：静默 */ }
  }

  // ── 拖拽文件挂载：拖到球上松手 → 主进程挂载（不立即发送，语音时合并发送）──
  // dragenter/dragleave 计数（而非仅 dragleave）：避免在子元素间移动时高亮闪烁
  let dragDepth = 0;
  document.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragDepth++;
    core.classList.add('drop-target');
  });
  document.addEventListener('dragover', (e) => {
    e.preventDefault();                       // 必须，否则 Chromium 拒绝 drop
  });
  document.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) core.classList.remove('drop-target');
  });
  document.addEventListener('drop', async (e) => {
    e.preventDefault();
    dragDepth = 0;
    core.classList.remove('drop-target');
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    const files = await window.fileDrop.handleFiles(e.dataTransfer.files);
    if (files.length) window.planner.mountFiles(files);
  });

  // 挂载文件数徽标（右上角蓝色小方块）
  const fileBadge = document.getElementById('file-badge');
  window.planner.onMountedChanged((list) => {
    const n = (list || []).length;
    fileBadge.classList.toggle('hidden', !n);
    fileBadge.textContent = n > 9 ? '9+' : n;
  });

  // 右键 → 菜单（放大 / 免打扰 / 清除挂载文件 / 退出）
  core.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    window.planner.bubbleMenu();
  });

  // 状态点（思考中脉冲 / 离线红点 / 逾期徽标）由主进程统一推送
  // （主进程 /dequeue 轮询带 state 广播），本窗口不再各自轮询 /state。
  function applyState(s) {
    const dot = document.getElementById('status-dot');
    const badge = document.getElementById('badge');
    if (!s) return;
    if (s.offline) {
      dot.classList.remove('thinking');
      dot.classList.add('offline');
      return;
    }
    dot.classList.toggle('thinking', !!s.thinking);
    dot.classList.remove('offline');
    const overdue = (s.plan && s.plan.overdue_count) || 0;
    badge.classList.toggle('hidden', !overdue);
    badge.textContent = overdue > 9 ? '9+' : overdue;
  }
  window.planner.onState(applyState);

  // ── 语音播报（气泡朗读）：收到 audio 事件 → 播放（新消息打断旧播放）──
  const ttsAudio = document.getElementById('tts-audio');
  window.planner.onAudio((url) => {
    if (!url) return;
    try {
      ttsAudio.src = window.planner.apiBase + url;
      ttsAudio.play().catch(() => { /* 播放失败静默 */ });
    } catch { /* 忽略 */ }
  });
})();
