// mic.js —— 语音输入公共模块：录音 → 16k/16bit/mono WAV bytes
// 用法：const stop = await window.mic.begin();
//       按住说话结束：const wav = await stop();       // Uint8Array 或 null
//       取消：await stop(true);                       // 丢弃录音
//
// 关键设计：麦克风流常驻（init 一次后不再释放）。实测 getUserMedia
// 每次启动 0.4~1.3s，而 MediaRecorder 创建 0ms——保持流常驻后，
// 每次录音几乎零延迟，变红即开录，不会丢说话内容。
(() => {
  'use strict';

  const SR = 16000;
  let audioCtx = null;
  let sharedStream = null;    // 常驻麦克风流（应用生命周期内不释放）
  let streamPromise = null;   // init 幂等（并发调用共享同一个初始化）

  async function getCtx() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    return audioCtx;
  }

  // 确保常驻流就绪（幂等；失败可重试）
  // 显式开启回声消除（AEC）+ 降噪 + 自动增益：自己的 TTS 从扬声器出来会被
  // 麦克风重新捕获，AEC 在源头消掉（依赖 Chromium/系统实现，效果随环境）——
  // 不够的部分由 voiceMode 的"播放中提高 VAD 阈值"兜底
  function init() {
    if (!streamPromise) {
      streamPromise = navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
        .then((s) => {
          sharedStream = s;
          return s;
        })
        .catch((err) => {
          streamPromise = null;   // 失败后允许下次重试
          throw err;
        });
    }
    return streamPromise;
  }

  function isReady() {
    return !!sharedStream;
  }

  // 音量分析器（常驻流挂载一次）：录音可视化用（动态环随音量跳动）
  let analyserNode = null;
  async function getAnalyser() {
    if (!sharedStream) return null;
    if (!analyserNode) {
      try {
        const ctx = await getCtx();
        if (ctx.state === 'suspended') { try { await ctx.resume(); } catch { /* 忽略 */ } }
        const src = ctx.createMediaStreamSource(sharedStream);
        analyserNode = ctx.createAnalyser();
        analyserNode.fftSize = 256;
        src.connect(analyserNode);
      } catch {
        return null;
      }
    }
    return analyserNode;
  }

  async function begin() {
    const stream = await init();   // 常驻流：首次启动 0.4~1.3s，之后 ~0ms
    const rec = new MediaRecorder(stream);
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.start();
    // 返回停止函数：stop() → wav bytes（Promise）；stop(true) → 取消。
    // 流不释放（常驻），下次录音零启动延迟。
    return (cancel) => new Promise((resolve) => {
      rec.onstop = async () => {
        if (cancel) { resolve(null); return; }
        try {
          const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
          const buf = await (await getCtx()).decodeAudioData(await blob.arrayBuffer());
          const samples = resampleTo16k(buf);
          if (!samples || samples.length < SR * 0.5) { resolve(null); return; }   // <500ms 视为无效
          resolve(encodeWav(samples));
        } catch {
          resolve(null);
        }
      };
      try { rec.stop(); } catch { resolve(null); }
    });
  }

  // 线性插值重采样 → 16k 单声道 Float32Array
  function resampleTo16k(buf) {
    const src = buf.getChannelData(0);
    if (buf.sampleRate === SR) return src;
    const ratio = buf.sampleRate / SR;
    const len = Math.max(1, Math.round(src.length / ratio));
    const out = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, src.length - 1);
      const frac = pos - i0;
      out[i] = src[i0] * (1 - frac) + src[i1] * frac;
    }
    return out;
  }

  // Float32Array → 16k/16bit/mono WAV bytes（标准 44 字节头）
  function encodeWav(samples) {
    const n = samples.length;
    const buffer = new ArrayBuffer(44 + n * 2);
    const view = new DataView(buffer);
    const wstr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    wstr(0, 'RIFF'); view.setUint32(4, 36 + n * 2, true); wstr(8, 'WAVE');
    wstr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, SR, true);
    view.setUint32(28, SR * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    wstr(36, 'data'); view.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return new Uint8Array(buffer);
  }

  window.mic = { begin, init, isReady, getAnalyser };
})();
