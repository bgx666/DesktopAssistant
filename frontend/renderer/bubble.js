// bubble.js —— 悬浮球：单击显示最近回复 / 长按说话 / 拖拽 / 右键菜单
// 交互约定：
// - 左键按住 <150ms 松开（单击）→ 显示模型最近回答的一条消息（气泡）
// - 左键按住 ≥150ms → 进入语音输入（变红即开录，麦克风流常驻零延迟），松开发送
// - 移动 ≥6px → 拖动球（取消录音）；右键 → 菜单（不变）
// 说明：getUserMedia 实测首次 1.3s / 之后 0.4~0.6s，MediaRecorder 0ms——
// mic.js 保持常驻流，录音几乎零延迟；变红与录音同步（流未就绪不提前变红）。
(() => {
  'use strict';

  const core = document.getElementById('core');

  // ── 手动拖拽（不用 -webkit-app-region，透明窗口上它吞点击事件）──
  let dragging = false;
  let startX = 0, startY = 0;
  let winX = 0, winY = 0;
  let lastMoveX = 0, lastMoveY = 0;
  let pressTimer = null;     // 150ms 长按判定定时器
  let longPress = false;     // 已进入长按（录音）状态
  let micStop = null;        // 录音停止函数（麦克风就绪后赋值）
  let micPromise = null;     // begin() 的 Promise（预启动，可能未就绪）

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
    // 150ms 未松开 → 判定为长按：变红提示（与录音同步，不丢语音）
    pressTimer = setTimeout(() => {
      pressTimer = null;
      longPress = true;
      if (window.mic.isReady()) {
        core.classList.add('recording');   // 常驻流就绪：判定即变红，录音已在录
      } else {
        // 流未就绪（首次冷启动）：等就绪后再变红（此时才真正开录）
        micPromise.then(() => {
          if (longPress) core.classList.add('recording');
        });
      }
    }, 150);
  });

  function cancelMic() {
    core.classList.remove('recording');
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
    // 位移超过 6px：放弃单击/说话，改为拖动
    if (pressTimer && (Math.abs(dx) >= 6 || Math.abs(dy) >= 6)) {
      clearTimeout(pressTimer);
      pressTimer = null;
    }
    if (micStop && (Math.abs(dx) >= 6 || Math.abs(dy) >= 6)) cancelMic();
    window.planner.moveBubble(winX + dx, winY + dy);
  });

  document.addEventListener('mouseup', async (e) => {
    if (!dragging) return;
    dragging = false;
    const dx = e.screenX - startX;
    const dy = e.screenY - startY;
    const isClick = Math.abs(dx) < 6 && Math.abs(dy) < 6;
    core.classList.remove('recording');
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
        await fetch(base + '/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: d.text }),
        });
      }
    } catch { /* 静默 */ }
  });

  // 单击 → 依次显示历史回复（第 1 次最新、第 2 次倒数第二…，循环）
  // 历史气泡从顶部开始向下堆叠（先点的在顶，更久远依次落在下面）
  // 若上一批气泡已过期消失（>气泡生命周期），重新从最新一条开始翻
  let replyOffset = 0;       // 距最新的偏移：0=最新
  let lastReplyAt = 0;       // 上次单击时间戳
  const REPLY_LIFE_MS = 30000;   // 与主进程 TOAST_MS 一致（气泡 30 秒生存）
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

  // 右键 → 菜单（放大 / 切换免打扰 / 退出）
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
