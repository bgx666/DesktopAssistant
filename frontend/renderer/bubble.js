// bubble.js —— 悬浮球：按住说话（录音→识别→直接发送）/ 拖拽 / 右键菜单
// 交互约定：左键按住 <6px = 按住说话（松开发送）；移动 ≥6px = 拖动球（取消录音）
(() => {
  'use strict';

  const core = document.getElementById('core');

  // ── 手动拖拽（不用 -webkit-app-region，透明窗口上它吞点击事件）──
  let dragging = false;
  let startX = 0, startY = 0;
  let winX = 0, winY = 0;
  let lastMoveX = 0, lastMoveY = 0;
  let micStop = null;        // 录音停止函数（录制中）
  let micPromise = null;     // begin() 的 Promise（超短按：mouseup 时还没准备好）

  core.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    dragging = true;
    startX = e.screenX;
    startY = e.screenY;
    lastMoveX = 0;
    lastMoveY = 0;
    window.planner.getBubblePos().then((pos) => {
      winX = pos.x;
      winY = pos.y;
    });
    // 按住说话：异步拿麦克风；期间已松开（超短按）→ 直接结束并交还 wav
    micPromise = window.mic.begin().then((stop) => {
      if (!dragging) return stop();          // 已松开：立即结束，拿 wav
      micStop = stop;
      core.classList.add('recording');
      return null;
    }).catch(() => {
      micStop = null;
      return null;
    });
  });

  function cancelMic() {
    if (micStop) {
      const s = micStop;
      micStop = null;
      core.classList.remove('recording');
      s(true);                               // 取消（丢弃录音）
    }
  }

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.screenX - startX;
    const dy = e.screenY - startY;
    if (Math.abs(dx - lastMoveX) < 2 && Math.abs(dy - lastMoveY) < 2) return;
    lastMoveX = dx;
    lastMoveY = dy;
    // 位移超过 6px：放弃说话，改为拖动
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
      cancelMic();
      micPromise = null;
      return;
    }
    // 按住说话结束：识别 → 直接发送
    const stop = micStop;
    micStop = null;
    let wav = null;
    if (stop) {
      wav = await stop(false);
    } else if (micPromise) {
      wav = await micPromise;                // 超短按路径
    }
    micPromise = null;
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
