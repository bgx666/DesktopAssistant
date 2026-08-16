// voiceMode.js —— 豆包式语音连续对话：免按 VAD + 说话打断 + 流式 TTS
//
// 状态机：off → listening（VAD 待命）→ recording（说话中，同时触发打断）
//         → sending（ASR + 发送中）→ listening
//
// 全局单例：主进程统一持有开关（uiSettings.voice_mode）并广播。
// 悬浮球/面板两个窗口各调一次 attach()，各自注入 audio 元素与 UI 回调；
// **只有当前形态对应的窗口是 active**（bubble ↔ 悬浮球形态 / app ↔ 面板形态），
// 通过 isActive 回调判定——非 active 窗口不录音、不播放（避免双识别/双播）。
//
// 流式 TTS（仅语音模式启用时）：text_stream 逐 token 累积 → 按标点切句
// → 逐句 /tts/say 合成 → 串行播放；同时抑制后端整句 audio 事件（防双播）。
(() => {
  'use strict';

  // VAD 参数（音量阈值 RMS）
  const START_DB = 0.022;         // 说话开始阈值（≈ -33dB，安静环境）
  const END_DB = 0.006;           // 静音阈值
  const START_MS = 150;           // 音量持续 150ms 判定为说话开始
  const END_MS = 700;             // 静音持续 700ms 判定说话结束
  const MAX_UTTERANCE_MS = 30000; // 单句最长 30s 强制切（防挂起）
  const SPEAK_MAX_CHARS = 96;     // 流式 TTS 单句最长（防超 Kokoro 限制）

  // 回声防护：自己的 TTS 播放中，扬声器声音会被麦克风捕获——
  // 播放中提高说话开始阈值 + 延长判定（要打断必须明显大声），
  // 播放结束进入冷却期（防音箱尾音误触发）
  const START_DB_HIGH = 0.07;     // 播放中：说话开始阈值（≈ -23dB，明显大声）
  const START_MS_HIGH = 300;      // 播放中：持续判定时长
  const COOLDOWN_MS = 400;        // 播放停止后冷却期（不触发说话开始）

  // 切句标点：句号/感叹/问号/省略号/分号/换行/逗号（长句按逗号切，保证流畅）
  const SPLIT_RE = /[。！？…；\n，,.]/;

  let enabled = false;        // 语音模式开关（全局）
  let state = 'off';          // off | listening | recording | sending
  let onUi = null;            // 窗口 UI 回调 { setState, interruptSpeech }
  let isActive = null;        // 窗口形态判定（本窗口是否负责语音收发）
  let speechActive = false;   // VAD 判定正在说话
  let startTimer = null;
  let endTimer = null;
  let maxTimer = null;
  let rafId = null;
  let stopRec = null;         // 当前录音停止函数
  let sending = false;        // ASR/发送中（期间不再触发新录音）
  let thinking = false;       // 后端生成中（打断时 POST /stop）
  let mutedTts = false;       // 主动停嘴（打断/关闭模式）——流式队列停止播放
  let ownAudio = false;       // 自己的 TTS 正在播放（扬声器出声 → 麦克风可能捕获）
  let cooldownUntil = 0;      // 播放停止后的冷却截止时间（防回声尾音误触发）
  let ttsEnabled = true;      // 自动语音播报开关（来自后端 settings.tts_enabled）

  // 流式 TTS 状态
  let streamBuf = '';
  let playQueue = [];
  let playing = false;
  let ttsAudio = null;        // attach 注入的 audio 元素
  let audioFetch = null;      // window.planner.audioFile（主进程代理）

  function apiFetch(path, opts) {
    return window.planner.apiFetch(path, opts || {});
  }

  // ── 开关（主进程广播驱动；窗口内按钮也可直接切）──────────
  function setEnabled(on) {
    on = !!on;
    if (on === enabled) return;
    enabled = on;
    if (on) {
      if (isActive()) {
        startVAD();
      }
    } else {
      // 关闭语音连续对话：只停 VAD/录音，不停流式 TTS（普通模式仍可逐句朗读）
      stopVADLoop();
      speechActive = false;
      sending = false;
      if (stopRec) { try { stopRec(true); } catch { /* 忽略 */ } stopRec = null; }
    }
    applyState();
  }

  function requestToggle() {
    // 走主进程（统一持久化 + 双窗口广播）；主进程不可达时本地兜底
    if (window.planner.setVoiceMode) {
      window.planner.setVoiceMode(!enabled);
    } else {
      setEnabled(!enabled);
    }
  }

  // 窗口形态变化（主进程广播 / morph 事件）时由窗口调用：重查 active
  function notifyVisibility() {
    if (!enabled) return;
    if (isActive()) {
      if (!rafId) startVAD();
    } else {
      stopVADLoop();
      if (stopRec) { try { stopRec(true); } catch { /* 忽略 */ } stopRec = null; }
      speechActive = false;
    }
  }

  function setThinking(v) {
    const was = thinking;
    thinking = !!v;
    if (was && !thinking) handleGenerationEnd();   // 生成结束 → 流式缓冲收尾
  }

  // ── VAD ─────────────────────────────────────────────────
  async function startVAD() {
    if (rafId) return;               // 已有循环（双通道触发幂等）
    const analyser = await window.mic.getAnalyser();
    if (rafId) return;               // await 期间已被其他入口启动
    if (!analyser || !enabled || !isActive()) { setState('off'); return; }
    const buf = new Uint8Array(analyser.fftSize);
    const tick = () => {
      if (!enabled || !isActive()) return;
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      const now = Date.now();
      // 自己的 TTS 播放中：需要明显大声（且持续更久）才判定为"用户说话/打断"，
      // 避免扬声器声音自我触发；冷却期内不启动新的说话判定
      const threshold = ownAudio ? START_DB_HIGH : START_DB;
      const holdMs = ownAudio ? START_MS_HIGH : START_MS;
      if (rms > threshold) {
        // 有声音
        if (!speechActive) {
          if (!startTimer && now >= cooldownUntil) {
            startTimer = setTimeout(beginRecording, holdMs);
          }
        } else {
          clearTimeout(endTimer);
          endTimer = null;
        }
      } else if (rms < END_DB) {
        // 安静
        if (speechActive && !endTimer && !sending) {
          endTimer = setTimeout(finishRecording, END_MS);
        }
      }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
  }

  function stopVADLoop() {
    clearTimeout(startTimer);
    clearTimeout(endTimer);
    clearTimeout(maxTimer);
    startTimer = endTimer = maxTimer = null;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
  }

  function beginRecording() {
    clearTimeout(startTimer);
    startTimer = null;
    if (sending || !isActive()) return;
    speechActive = true;
    setState('recording');
    interrupt();                     // 打断：停嘴 + 停止生成
    window.mic.begin().then((stop) => {
      if (!speechActive) { if (stop) stop(true); return; }
      stopRec = stop;
      maxTimer = setTimeout(finishRecording, MAX_UTTERANCE_MS);
    }).catch(() => { /* 麦克风不可用：忽略 */ });
  }

  async function finishRecording() {
    clearTimeout(endTimer);
    clearTimeout(maxTimer);
    endTimer = maxTimer = null;
    speechActive = false;
    const s = stopRec;
    stopRec = null;
    if (!s || sending) { if (s) s(true); return; }
    setState('sending');
    sending = true;
    let wav = null;
    try {
      wav = await s(false);
    } catch { /* 静默 */ }
    if (wav) {
      try {
        const r = await apiFetch('/asr', {
          method: 'POST',
          headers: { 'Content-Type': 'audio/wav' },
          body: wav,
        });
        const d = JSON.parse(r.text);
        if (d.ok && d.text) {
          await apiFetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: d.text }),
          });
        }
      } catch { /* 后端不可达：静默 */ }
    }
    sending = false;
    setState('listening');
  }

  // ── 打断（说话开始即触发）────────────────────────────────
  function interrupt() {
    mutedTts = true;
    // 立即停嘴（正在播放的 TTS）
    if (ttsAudio) { try { ttsAudio.pause(); } catch { /* 忽略 */ } }
    if (onUi && typeof onUi.interruptSpeech === 'function') onUi.interruptSpeech();
    playQueue = [];
    // 停止后端生成（安全位置收尾，已生成文本保留）
    if (thinking || sending) {
      thinking = false;
      apiFetch('/stop', { method: 'POST' }).catch(() => { /* 静默 */ });
    }
  }

  // ── 流式 TTS（text_stream 驱动）──────────────────────────
  function handleTextStream(content) {
    // 普通模式也走流式 TTS：不依赖 voice_mode 开关，只要有 text_stream 就尽早出声
    if (!isActive()) return;
    if (mutedTts) mutedTts = false;    // 新一轮生成恢复播放
    streamBuf += content;
    splitAndQueue(false);
  }

  function handleGenerationEnd() {
    if (mutedTts) { streamBuf = ''; return; }
    splitAndQueue(true);               // 收尾剩余缓冲
  }

  function splitAndQueue(force) {
    const parts = streamBuf.split(SPLIT_RE);
    while (parts.length > 1) {
      const seg = parts.shift().trim();
      if (seg) queueSpeak(seg);
    }
    streamBuf = parts[0] || '';
    if (force) {
      const rest = streamBuf.trim();
      if (rest) queueSpeak(rest);
      streamBuf = '';
    } else if (streamBuf.length > SPEAK_MAX_CHARS) {
      const rest = streamBuf.trim();
      if (rest) queueSpeak(rest);
      streamBuf = '';
    }
  }

  function queueSpeak(text) {
    if (!ttsEnabled || !text || text.length < 2) return;
    playQueue.push(text);
    pumpPlay();
  }

  async function pumpPlay() {
    if (playing || !playQueue.length || !ttsAudio || !isActive()) return;
    playing = true;
    const text = playQueue.shift();
    try {
      if (!mutedTts) {
        const r = await apiFetch('/tts/say?text=' + encodeURIComponent(text));
        const d = JSON.parse(r.text);
        if (d.ok && d.url) {
          const fileUrl = await audioFetch(d.url);
          if (fileUrl && !mutedTts) {
            await new Promise((resolve) => {
              const onEnd = () => { ttsAudio.removeEventListener('ended', onEnd); resolve(); };
              ttsAudio.addEventListener('ended', onEnd);
              ttsAudio.onerror = () => { try { ttsAudio.removeAttribute('src'); } catch { /* 忽略 */ } resolve(); };
              ttsAudio.src = fileUrl;
              ttsAudio.play().catch(() => resolve());
            });
          }
        }
      }
    } catch { /* 静默 */ } finally {
      playing = false;
      if (playQueue.length) pumpPlay();
    }
  }

  // ── 关闭清理 ─────────────────────────────────────────────
  function stopEverything() {
    stopVADLoop();
    speechActive = false;
    sending = false;
    if (stopRec) { try { stopRec(true); } catch { /* 忽略 */ } stopRec = null; }
    if (ttsAudio) { try { ttsAudio.pause(); } catch { /* 忽略 */ } }
    playQueue = [];
    streamBuf = '';
    setState('off');
  }

  // ── 状态与 UI ────────────────────────────────────────────
  function setState(s) {
    state = s;
    applyState();
  }

  function applyState() {
    if (onUi && typeof onUi.setState === 'function') onUi.setState(state, enabled);
  }

  // ── 附加到窗口（bubble / app 各调一次）───────────────────
  function attach(opts) {
    ttsAudio = opts.audioEl || null;
    audioFetch = opts.audioFile || window.planner.audioFile;
    onUi = opts.onUi || null;
    isActive = (typeof opts.isActive === 'function') ? opts.isActive : () => true;
    // 追踪自己的 TTS 播放状态：扬声器出声时提高 VAD 阈值（回声防护）
    if (ttsAudio) {
      ttsAudio.addEventListener('playing', () => { ownAudio = true; });
      ttsAudio.addEventListener('pause', () => {
        ownAudio = false;
        cooldownUntil = Date.now() + COOLDOWN_MS;
      });
      ttsAudio.addEventListener('ended', () => {
        ownAudio = false;
        cooldownUntil = Date.now() + COOLDOWN_MS;
      });
    }
    window.planner.onState((s) => setThinking(!!(s && s.thinking)));
    window.planner.onEvents((events) => {
      for (const ev of events || []) {
        if (ev.type === 'text_stream' && ev.content) {
          handleTextStream(ev.content);
        }
      }
    });
    // 初始开关状态（主进程 uiSettings / 广播）
    window.planner.onUiSettings((s) => {
      setEnabled(!!(s && s.voice_mode));
    });
    window.planner.onVoiceMode((on) => setEnabled(on));
    // 自动语音播报开关（普通模式流式 TTS 也遵守）
    window.planner.onSettings((s) => {
      if (s && typeof s.tts_enabled !== 'undefined') ttsEnabled = !!s.tts_enabled;
    });
    apiFetch('/settings').then((r) => {
      try {
        const d = JSON.parse(r.text);
        if (d && d.settings && typeof d.settings.tts_enabled !== 'undefined') {
          ttsEnabled = !!d.settings.tts_enabled;
        }
      } catch { /* 忽略 */ }
    }).catch(() => { /* 忽略 */ });
    window.planner.getUiSettings().then((s) => {
      enabled = !!(s && s.voice_mode);
      if (enabled && isActive()) startVAD();
      applyState();
    }).catch(() => {});
  }

  window.voiceMode = {
    attach,
    requestToggle,
    setEnabled,
    setThinking,
    notifyVisibility,
    isEnabled: () => enabled,
    state: () => state,
    // 流式 TTS 接管后，抑制后端整句 audio 事件（防双播）
    shouldSuppressAudio: () => true,
    suppressAudio: () => {
      mutedTts = true;
      if (ttsAudio) { try { ttsAudio.pause(); } catch { /* 忽略 */ } }
    },
    // 用户开始语音输入/打断时调用：停嘴 + 停止后端生成
    interrupt,
  };
})();
