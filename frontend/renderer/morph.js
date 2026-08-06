// morph.js —— 球 → 矩形窗口的变形动画（rAF 60fps 驱动）
// 每帧：IPC setBounds 驱动窗口尺寸/位置，CSS 插值圆角与背景，让圆形球体
// 逐渐"撑开"成矩形面板；到达阈值后球形态淡出、面板 UI 淡入。
(() => {
  'use strict';

  // 动画参数
  const IN_MS = 420;        // 展开时长（球 → 面板）
  const OUT_MS = 340;       // 收回时长（面板 → 球）
  const SWITCH_W = 170;     // 球形态 → 面板形态的窗口宽度阈值
  const FULL_W = 240;       // 球完全淡出的窗口宽度

  const ball = document.getElementById('morph-ball');
  const ballInner = ball.querySelector('.morph-inner');
  const panelUI = document.getElementById('panel-ui');

  let running = false;

  function easeOutExpo(t) {
    return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t);
  }
  function easeInCubic(t) { return t * t * t; }

  // 每帧应用：窗口矩形 + 球形态插值
  function applyFrame(b, w, h) {
    window.planner.setPanelBounds({ x: b.x, y: b.y, w: Math.max(28, w), h: Math.max(28, h) });

    // 圆角：窗口小时 50%（圆形），宽高不等时呈椭圆，接近面板时收敛为 14px
    const r = w < SWITCH_W
      ? '50%'
      : `${Math.max(14, Math.round(50 - (w - SWITCH_W) / (FULL_W - SWITCH_W) * 36))}px`;
    ball.style.borderRadius = r;

    // 球形态透明度：窗口 < SWITCH_W 完全显示；SWITCH_W→FULL_W 渐隐
    const ballOpacity = w >= FULL_W ? 0 : Math.max(0, Math.min(1, 1 - (w - SWITCH_W) / (FULL_W - SWITCH_W)));
    ball.style.opacity = ballOpacity;

    // 面板 UI：SWITCH_W→FULL_W 渐入
    const uiOpacity = w <= SWITCH_W ? 0 : Math.max(0, Math.min(1, (w - SWITCH_W) / (FULL_W - SWITCH_W)));
    panelUI.style.opacity = uiOpacity;
    panelUI.style.pointerEvents = uiOpacity > 0.5 ? 'auto' : 'none';
  }

  function runMorph({ from, to, duration, easing, done }) {
    if (running) cancelAnimationFrame(running);
    const t0 = performance.now();
    let finished = false;
    // rAF 被暂停（窗口最小化/隐藏）时的兜底：超时直接跳到终点
    const failSafe = setTimeout(() => {
      if (finished) return;
      finished = true;
      running = 0;
      applyFrame(to, to.w, to.h);
      done(to);
    }, duration + 600);
    const finish = (b) => {
      if (finished) return;
      finished = true;
      clearTimeout(failSafe);
      running = 0;
      done(b);
    };
    const step = () => {
      const t = Math.min(1, (performance.now() - t0) / duration);
      const e = easing(t);
      const b = {
        x: from.x + (to.x - from.x) * e,
        y: from.y + (to.y - from.y) * e,
        w: from.w + (to.w - from.w) * e,
        h: from.h + (to.h - from.h) * e,
      };
      applyFrame(b, b.w, b.h);
      if (t >= 1) finish(b);
      else running = requestAnimationFrame(step);
    };
    running = requestAnimationFrame(step);
  }

  // 主进程强制完成（动画卡死/超时）→ 同步 UI 到终点形态
  window.planner.onMorphForceFinish((kind) => {
    if (running) cancelAnimationFrame(running);
    running = 0;
    if (kind === 'in') {
      ball.style.display = 'none';
      panelUI.style.opacity = 1;
      panelUI.classList.add('ready');
      panelUI.style.pointerEvents = 'auto';
    } else {
      panelUI.style.opacity = 0;
      panelUI.classList.remove('ready');
      ball.style.display = '';
      ball.style.opacity = 1;
      ball.style.borderRadius = '50%';
    }
  });

  // 展开：球 → 面板
  window.planner.onMorphIn(({ from, to }) => {
    panelUI.classList.remove('ready');
    ball.style.display = '';
    panelUI.style.opacity = 0;
    runMorph({
      from, to,
      duration: IN_MS, easing: easeOutExpo,
      done: () => {
        ball.style.display = 'none';
        panelUI.style.opacity = 1;
        panelUI.classList.add('ready');
        panelUI.style.pointerEvents = 'auto';
        window.planner.morphDone('in');
      },
    });
  });

  // 收回：面板 → 球
  window.planner.onMorphOut(({ from, to }) => {
    panelUI.classList.remove('ready');
    panelUI.style.pointerEvents = 'none';
    ball.style.display = '';
    ball.style.opacity = 1;
    runMorph({
      from, to: { x: to.x, y: to.y, w: 56, h: 56 },
      duration: OUT_MS, easing: easeInCubic,
      done: () => {
        panelUI.style.opacity = 0;
        window.planner.morphDone('out');
      },
    });
  });

  // ── 球形态状态点（思考中 / 离线）────────────────────────
  async function pollBallState() {
    const dot = document.getElementById('morph-status-dot');
    try {
      const API = (window.planner && window.planner.apiBase) || 'http://127.0.0.1:18771';
      const data = await (await fetch(API + '/state')).json();
      dot.classList.toggle('thinking', !!(data.state && data.state.thinking));
      dot.classList.remove('offline');
    } catch {
      dot.classList.remove('thinking');
      dot.classList.add('offline');
    }
  }
  pollBallState();
  setInterval(pollBallState, 3000);
})();
