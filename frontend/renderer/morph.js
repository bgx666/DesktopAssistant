// morph.js —— 球 → 矩形窗口的变形动画（CSS transition 驱动）
// 窗口本身只 resize 两次（动画前后各一次，由主进程执行）；本文件只驱动
// #morph-stage 的 transform（scale + translate）与两层 opacity——
// 全部是合成器属性，由 GPU 合成器完成动画，主线程零开销、零 IPC。
// 内容固定 350×520 布局，文字全程不 reflow。

(() => {
  'use strict';

  // 动画参数（与主进程 PANEL_W/H、BUBBLE_SIZE 对应）
  const STAGE_W = 350;
  const STAGE_H = 520;
  const BUBBLE_SIZE = 56;
  const SCALE0 = BUBBLE_SIZE / STAGE_W;   // 球形态 scale ≈ 0.16

  const stage = document.getElementById('morph-stage');
  const ball = document.getElementById('morph-ball');
  const panelUI = document.getElementById('panel-ui');
  const ballInner = ball.querySelector('.morph-inner');

  let animating = false;

  function baseTransform() {
    // 舞台居中于窗口的基准 transform
    return `translate(-50%, -50%)`;
  }

  // 展开起始态：内容中心对齐球位置，尺寸 = 球尺寸
  function startTransform(from, to) {
    const cx = (to.x + to.w / 2) - (from.x + from.w / 2);   // 球中心相对面板中心偏移
    const cy = (to.y + to.h / 2) - (from.y + from.h / 2);
    stage.style.transform = `${baseTransform()} translate(${cx}px, ${cy}px) scale(${SCALE0})`;
  }

  function finishTransform() {
    stage.style.transform = `${baseTransform()} scale(1)`;
  }

  // 动画结束兜底（transitionend 可能因窗口隐藏/异常丢失）
  function armTimeout(kind, done) {
    const ms = kind === 'in' ? 420 : 340;
    setTimeout(() => {
      if (!animating) return;
      animating = false;
      finishTransform();
      ball.style.opacity = kind === 'in' ? '0' : '1';
      panelUI.style.opacity = kind === 'in' ? '1' : '0';
      done();
    }, ms + 250);
  }

  function runMorph(kind, from, to, done) {
    animating = true;
    if (kind === 'in') {
      // 起始：球形态
      ball.style.display = '';
      ball.style.opacity = '1';
      panelUI.style.opacity = '0';
      startTransform(from, to);
      // 下一帧应用目标 transform → CSS transition 自动动画
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (!animating) return;
        stage.classList.add('ready');
        stage.classList.remove('folding');
        finishTransform();
        // 动画中段：球渐隐、面板渐入（opacity transition 渐变衔接，
        // 避免"放大后瞬间刷成面板"的突兀感）
        setTimeout(() => {
          if (!animating) return;
          ball.style.opacity = '0';
          panelUI.style.opacity = '1';
        }, 190);
      }));
    } else {
      stage.classList.add('folding');
      stage.classList.remove('ready');
      ball.style.display = '';
      ball.style.opacity = '0';
      panelUI.style.opacity = '1';
      panelUI.classList.remove('ready');
      panelUI.style.pointerEvents = 'none';
      startTransform(from, to);   // from=面板, to=球：内容中心滑向球位置并缩小
      // 动画中段：面板渐隐、球渐显
      setTimeout(() => {
        if (!animating) return;
        ball.style.opacity = '1';
        panelUI.style.opacity = '0';
      }, 140);
    }

    const onEnd = (ev) => {
      if (!animating) return;
      if (ev && ev.propertyName !== 'transform') return;
      animating = false;
      stage.removeEventListener('transitionend', onEnd);
      if (kind === 'in') {
        ball.style.opacity = '0';
        ball.style.display = 'none';
        panelUI.style.opacity = '1';
        panelUI.classList.add('ready');
        panelUI.style.pointerEvents = 'auto';
      } else {
        panelUI.style.opacity = '0';
        panelUI.style.pointerEvents = 'none';
        ball.style.display = '';
        ball.style.opacity = '1';
      }
      done();
    };
    stage.addEventListener('transitionend', onEnd);
    armTimeout(kind, () => {
      stage.removeEventListener('transitionend', onEnd);
      if (kind === 'in') {
        ball.style.opacity = '0';
        ball.style.display = 'none';
        panelUI.style.opacity = '1';
        panelUI.classList.add('ready');
        panelUI.style.pointerEvents = 'auto';
      } else {
        panelUI.style.opacity = '0';
        panelUI.style.pointerEvents = 'none';
        ball.style.display = '';
        ball.style.opacity = '1';
      }
      done();
    });
  }

  // 主进程强制完成（异常/超时）→ 同步 UI 到终点形态
  window.planner.onMorphForceFinish((kind) => {
    animating = false;
    if (kind === 'in') {
      stage.classList.add('ready');
      stage.classList.remove('folding');
      finishTransform();
      ball.style.display = 'none';
      ball.style.opacity = '0';
      panelUI.style.opacity = '1';
      panelUI.classList.add('ready');
      panelUI.style.pointerEvents = 'auto';
    } else {
      stage.classList.add('folding');
      stage.classList.remove('ready');
      finishTransform();
      panelUI.style.opacity = '0';
      panelUI.classList.remove('ready');
      panelUI.style.pointerEvents = 'none';
      ball.style.display = '';
      ball.style.opacity = '1';
    }
  });

  // 展开：球 → 面板
  window.planner.onMorphIn(({ from, to }) => {
    panelUI.classList.remove('ready');
    ball.style.display = '';
    runMorph('in', from, to, () => window.planner.morphDone('in'));
  });

  // 收回：面板 → 球
  window.planner.onMorphOut(({ from, to }) => {
    runMorph('out', from, to, () => window.planner.morphDone('out'));
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
