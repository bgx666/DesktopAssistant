// bubble.js —— 悬浮球：单击显示最近回复 / 长按说话 / 拖拽 / 右键菜单
// 交互约定：
// - 左键按住 <300ms 松开（单击）→ 显示模型最近回答的一条消息（气泡）
// - 左键按住 ≥300ms → 进入语音输入（变红脉冲），松开发送
// - 移动 ≥6px → 拖动球（取消录音）；右键 → 菜单（不变）
(() => {
  'use strict';

  const core = document.getElementById('core');

  // ── 手动拖拽（不用 -webkit-app-region，透明窗口上它吞点击事件）──
  let dragging = false;
  let startX = 0, startY = 0;
  let winX = 0, winY = 0;
  let lastMoveX = 0, lastMoveY = 0;
  let pressTimer = null;     // 300ms 长按判定定时器
  let longPress = false;     // 已进入长按（录音）状态
  let micStop = null;        // 录音停止函数（录制中）
  let micPromise = null;     // begin() 的 Promise（松开时还没准备好）

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
    // 300ms 未松开 → 判定为长按：开始录音
    pressTimer = setTimeout(() => {
      pressTimer = null;
      longPress = true;
      try {
        micPromise = window.mic.begin().then((stop) => {
          if (!dragging) return stop();          // 已松开：立即结束，拿 wav
          micStop = stop;
          core.classList.add('recording');
          return null;
        }).catch(() => {
          micStop = null;
          return null;
        });
      } catch {
        micPromise = null;
      }
    }, 300);
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
      micPromise = null;
      return;
    }
    if (!longPress) {
      // 单击：显示模型最近回答的一条消息
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      showLastReply();
      return;
    }
    // 长按结束：识别 → 直接发送
    const stop = micStop;
    micStop = null;
    let wav = null;
    if (stop) {
      wav = await stop(false);
    } else if (micPromise) {
      wav = await micPromise;                // 松开早于 begin() 完成
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

  // 单击 → 取最近一条 assistant 回复，以气泡形式显示
  async function showLastReply() {
    try {
      const r = await fetch(window.planner.apiBase + '/history', { signal: AbortSignal.timeout(5000) });
      const d = await r.json();
      const msgs = d.messages || [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === 'assistant' && msgs[i].content) {
          window.planner.showToast(msgs[i].content);
          return;
        }
      }
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
