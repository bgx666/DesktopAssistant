// app.js —— 渲染进程：轮询 /dequeue + 聊天 / 今日计划 / 任务列表
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const sendBtn = $('#send');   // 发送/停止/语音 三合一按钮（模块级，多处引用）
  const state = { heartbeat: null, agenda: null, lastAgendaDate: '' };

  // ── HTTP（走主进程代理：file:// 页面直连被 CORS/PNA 拦）────────
  async function api(path, opts = {}) {
    const r = await window.planner.apiFetch(path, opts);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return JSON.parse(r.text);
  }

  function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // ── 聊天 ─────────────────────────────────────────────
  const messages = $('#messages');

  // ── 回到最新消息按钮 ──────────────────────────────────
  // 向上翻历史时显示，点击平滑滚回底部（类主流 AI 对话界面）。
  const jumpBtn = $('#jump-bottom');
  messages.addEventListener('scroll', () => {
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    jumpBtn.classList.toggle('hidden', nearBottom);
  });
  jumpBtn.addEventListener('click', () => {
    messages.scrollTo({ top: messages.scrollHeight, behavior: 'smooth' });
  });

  // 自动滚动策略：**只在用户"贴底"时跟随新内容**；用户翻上去看历史时绝不打扰
  // （曾经流式/思考/工具追加都无条件拉到底，边思考边看旧消息会被反复拽回去）。
  // force=true 用于用户自己的动作（发消息、首次加载）。
  function scrollMessagesToBottom(force = false) {
    if (!force) {
      const near = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
      if (!near) return;
    }
    messages.scrollTop = messages.scrollHeight;
  }

  // ── 朗读按钮（气泡旁喇叭）三态：空闲 →（点击）合成中 → 播放中 →（再点）停止 ──
  // 需求（用户）：播放过程中再点喇叭要能取消播报；合成中点击=取消合成。
  // 实现要点：单音频元素 ttsAudio 被多处共用（自动朗读/流式 TTS），谁开始播放
  // 谁就把"播放中"按钮清掉（clearSpeakPlaying），避免按钮状态残留。
  let speakToken = 0;   // 每次点击/停止递增；旧异步流程凭 token 作废（防串台）
  const SPEAK_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">' +
    '<path d="M3 9v6h4l5 5V4L7 9H3z"/>' +
    '<path d="M16 8.5a5 5 0 0 1 0 7" stroke="currentColor" stroke-width="1.6" fill="none"/>' +
    '</svg>';
  const SPEAK_STOP_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">' +
    '<rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

  function setSpeakVisual(btn, state) {   // state: '' | 'loading' | 'playing'
    if (!btn) return;
    btn.classList.toggle('loading', state === 'loading');
    btn.classList.toggle('playing', state === 'playing');
    btn.title = state === 'loading' ? '合成中…再点一下取消'
      : state === 'playing' ? '停止播报'
      : '朗读这条消息';
    btn.innerHTML = state === 'playing' ? SPEAK_STOP_ICON : SPEAK_ICON;
  }

  // 清掉所有"播放中"按钮（新的播放（任何来源）即将顶替时调用）
  function clearSpeakPlaying() {
    document.querySelectorAll('.speak-btn.playing').forEach((b) => setSpeakVisual(b, ''));
  }

  function stopSpeakPlayback() {
    speakToken += 1;
    try { ttsAudio.pause(); } catch { /* 忽略 */ }
    try { ttsAudio.removeAttribute('src'); } catch { /* 忽略 */ }
    clearSpeakPlaying();
  }

  // 等当前音频结束/被打断/出错（用于把按钮从"播放中"复位）
  // 注意带上 'emptied'：换 src（别的播放顶替、流式 TTS 接管）在 Chromium 里
  // 不一定触发 'pause'，但一定 'emptied'——漏了会让按钮永远停在"播放中"。
  function waitAudioEnded() {
    return new Promise((resolve) => {
      const onEnd = () => {
        ttsAudio.removeEventListener('ended', onEnd);
        ttsAudio.removeEventListener('pause', onEnd);
        ttsAudio.removeEventListener('emptied', onEnd);
        ttsAudio.removeEventListener('error', onEnd);
        resolve();
      };
      ttsAudio.addEventListener('ended', onEnd);
      ttsAudio.addEventListener('pause', onEnd);
      ttsAudio.addEventListener('emptied', onEnd);
      ttsAudio.addEventListener('error', onEnd);
    });
  }

  function addMessage(text, cls = 'assistant', msgId = null, forceScroll = true) {
    closeToolGroup();   // 普通消息出现 → 工具组封口（后续工具调用开新组）
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    // 气泡 + 喇叭按钮：横向一行（气泡左、喇叭右，底部对齐）
    const body = document.createElement('div');
    body.className = 'msg-body';
    el.appendChild(body);
    const bubble = document.createElement('div');
    bubble.className = 'bubble ' + cls;   // bubble user / bubble assistant（颜色区分）
    if (cls === 'assistant' || cls === 'memory') {
      // LLM 生成内容渲染轻量 markdown（加粗/代码）；用户消息保持原文
      bubble.innerHTML = window.md.render(text);
      if (cls === 'assistant') enhanceHtmlPreviews(bubble);   // ```html → 沙箱预览卡
    } else {
      bubble.textContent = text;
    }
    body.appendChild(bubble);
    if (cls === 'assistant') {
      // 喇叭按钮：合成并朗读这条消息；播放中再点=停止播报，合成中再点=取消
      // （用户手势播放，不受自动播放限制）
      const speak = document.createElement('button');
      speak.className = 'speak-btn';
      speak.title = '朗读这条消息';
      speak.innerHTML = SPEAK_ICON;
      speak.addEventListener('click', async () => {
        if (speak.classList.contains('playing')) {   // 播放中 → 停止播报
          stopSpeakPlayback();
          return;
        }
        if (speak.classList.contains('loading')) {   // 合成中 → 取消
          speakToken += 1;
          setSpeakVisual(speak, '');
          return;
        }
        const bubbleEl = body.querySelector('.bubble');
        if (!bubbleEl || !bubbleEl.textContent.trim()) return;
        const myToken = ++speakToken;
        speak.dataset.gen = String(myToken);   // 本按钮本轮流程的代号（复位判定用）
        setSpeakVisual(speak, 'loading');
        let r;
        try {
          r = await window.planner.apiFetch('/tts/say?text=' + encodeURIComponent(bubbleEl.textContent.trim()));
        } catch {
          if (myToken === speakToken) {
            addMessage('语音合成失败（后端未连接）', 'log');
            setSpeakVisual(speak, '');
          }
          return;
        }
        if (myToken !== speakToken) return;          // 等待合成期间已被取消
        try {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const d = JSON.parse(r.text);
          if (!d.ok || !d.url) throw new Error(d.error || 'tts_unavailable');
          const ok = await playAudioUrl(d.url);      // 新播放会清掉别的"播放中"按钮
          if (myToken !== speakToken) return;        // 下载期间被取消/停止
          if (!ok) throw new Error('play_failed');
          setSpeakVisual(speak, 'playing');
          await waitAudioEnded();                    // 播完 / 被顶替 / 被停止
        } catch (e) {
          console.error('[tts] 播放失败:', e);
          if (myToken === speakToken) addMessage('语音播放失败', 'log');
        }
        // 复位按"本按钮有没有开新的一轮"判定（而不是全局 token）：别的按钮
        // 被点会 bump 全局 token，但那不影响本按钮的播放结束复位——否则本按钮
        // 播完时若全局 token 已被别的点击改变，会永远停在"播放中"。
        if (Number(speak.dataset.gen) === myToken) setSpeakVisual(speak, '');
      });
      body.appendChild(speak);
    }
    if (cls === 'user' && msgId) {
      const undo = document.createElement('button');
      undo.className = 'undo-btn';
      undo.textContent = '撤销';
      undo.title = '撤回这条消息及其之后的对话，内容恢复到输入框';
      undo.addEventListener('click', async () => {
        // 两段式确认：第一次点击进入"确认撤销？"，3 秒内再点才执行
        if (undo.dataset.confirm !== '1') {
          undo.dataset.confirm = '1';
          undo.textContent = '确认撤销？';
          undo.classList.add('confirm');
          clearTimeout(undo._confirmTimer);
          undo._confirmTimer = setTimeout(() => {
            delete undo.dataset.confirm;
            undo.textContent = '撤销';
            undo.classList.remove('confirm');
          }, 3000);
          return;
        }
        clearTimeout(undo._confirmTimer);
        undo.disabled = true;
        try {
          const r = await post('/undo', { msg_id: msgId });
          if (r.ok) {
            // 被撤销的消息内容恢复到输入框（方便修改后重发，而非直接删掉）
            const bubble = el.querySelector('.bubble');
            const text = bubble ? bubble.textContent : '';
            const input = $('#input');
            if (text) {
              input.value = text;
              input.focus();
            }
            loadHistory(true);
          } else {
            addMessage(r.error || '无法撤销', 'log');
          }
        } catch {
          addMessage('撤销失败（后端未连接）', 'log');
        }
      });
      el.appendChild(undo);
    }
    if (cls === 'user' && msgId) el.dataset.msgId = String(msgId);
    messages.appendChild(el);
    scrollMessagesToBottom(cls === 'user' && forceScroll);   // 自己发的消息强制到底；其他/事件到达只在贴底时跟随
    return el;
  }

  // 用户消息去重：面板本地提交与主进程 user_message 事件可能几乎同时到达
  // （悬浮球语音 / 快速输入浮层发的消息只有事件——旧代码展开面板看不到，用户实测）
  function userAttachmentExt(a) {
    const raw = String(a.ext || '')
      || (String(a.name || '').includes('.')
          ? String(a.name).split('.').pop().toLowerCase() : '');
    return (raw.replace(/^\./, '').toUpperCase() || 'FILE').slice(0, 5);
  }

  function openUserImage(url, name) {
    const host = document.getElementById('panel-ui') || document.body;
    const overlay = document.createElement('div');
    overlay.className = 'img-lightbox';
    const img = document.createElement('img');
    img.alt = name || '图片';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'lb-close';
    close.textContent = '×';
    close.title = '关闭（Esc）';
    overlay.appendChild(img);
    overlay.appendChild(close);
    if (name) {
      const cap = document.createElement('div');
      cap.className = 'lb-caption';
      cap.textContent = name;
      overlay.appendChild(cap);
    }
    let closed = false;
    const doClose = () => {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKey);
      overlay.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape') doClose(); };
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay || e.target === close) doClose();
    });
    document.addEventListener('keydown', onKey);
    host.appendChild(overlay);
    window.planner.imageFile(url).then((fileUrl) => {
      if (closed) return;
      if (!fileUrl) { img.replaceWith(document.createTextNode('（图片加载失败）')); return; }
      img.src = fileUrl;
    }).catch(() => {
      if (!closed) img.replaceWith(document.createTextNode('（图片加载失败）'));
    });
  }

  function attachUserAttachments(el, attachments) {
    const atts = Array.isArray(attachments) ? attachments.filter(Boolean) : [];
    if (!el || !atts.length) return;
    const old = el.querySelector('.user-attachments');
    if (old) old.remove();
    const wrap = document.createElement('div');
    wrap.className = 'user-attachments';
    const images = atts.filter((a) => a.kind === 'image' && a.url);
    if (images.length) {
      // 图片缩略图在上方，点击打开大图
      const grid = document.createElement('div');
      grid.className = 'user-image-grid';
      images.forEach((a) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'user-image-thumb';
        btn.title = a.name || '点击放大';
        const ph = document.createElement('span');
        ph.className = 'ua-loading';
        ph.textContent = '加载中…';
        btn.appendChild(ph);
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          openUserImage(a.url, a.name || '');
        });
        window.planner.imageFile(a.url).then((fileUrl) => {
          if (!fileUrl) { ph.textContent = '加载失败'; return; }
          const img = document.createElement('img');
          img.alt = a.name || '图片';
          img.onload = () => { try { ph.remove(); } catch { /* 忽略 */ } };
          img.onerror = () => { ph.textContent = '加载失败'; img.remove(); };
          img.src = fileUrl;
          btn.insertBefore(img, ph);
        }).catch(() => { ph.textContent = '加载失败'; });
        grid.appendChild(btn);
      });
      wrap.appendChild(grid);
    }
    // 非图片 / 图片复制失败的附件 → 可点击文件快捷方式
    const files = atts.filter((a) => !(a.kind === 'image' && a.url));
    if (files.length) {
      const row = document.createElement('div');
      row.className = 'user-file-row';
      files.forEach((a) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'user-file-chip';
        chip.title = a.path || a.name || '';
        if (!a.path) chip.disabled = true;
        const ext = document.createElement('span');
        ext.className = 'ua-ext';
        ext.textContent = userAttachmentExt(a);
        const name = document.createElement('span');
        name.className = 'ua-name';
        name.textContent = a.name || '未命名';
        chip.appendChild(ext);
        chip.appendChild(name);
        chip.addEventListener('click', async () => {
          if (!a.path) return;
          try {
            const r = await window.planner.openInExplorer(a.path);
            if (!r || !r.ok) addMessage(`（打开失败：${a.path}）`, 'log');
          } catch {
            addMessage(`（打开失败：${a.path}）`, 'log');
          }
        });
        row.appendChild(chip);
      });
      wrap.appendChild(row);
    }
    el.insertBefore(wrap, el.firstChild);
  }

  function addUserMessage(text, msgId, forceScroll = true, attachments = []) {
    const atts = Array.isArray(attachments) ? attachments : [];
    if (msgId) {
      const dup = Array.from(messages.querySelectorAll('.msg.user'))
        .find((el) => el.dataset.msgId === String(msgId));
      if (dup) {
        // 本地提交与 user_message 事件可能先后到达；后来的一份补附件
        if (atts.length) attachUserAttachments(dup, atts);
        return dup;
      }
    }
    const el = addMessage(text, 'user', msgId || null, forceScroll);
    if (atts.length) attachUserAttachments(el, atts);
    return el;
  }

  // 错误气泡：正文（发生了什么）+ 提示（下一步怎么做）；不再只留一行灰色日志
  function addError(text, hint) {
    const el = addMessage('', 'error');
    const bubble = el.querySelector('.bubble');
    if (bubble) {
      bubble.innerHTML = '';
      const main = document.createElement('div');
      main.className = 'err-main';
      main.textContent = text || '出错了';
      bubble.appendChild(main);
      if (hint) {
        const h = document.createElement('div');
        h.className = 'err-hint';
        h.textContent = hint;
        bubble.appendChild(h);
      }
    }
    scrollMessagesToBottom();
    return el;
  }

  // ── 输入框（textarea）：自动增高 + Enter 发送 / Shift+Enter 换行 ──
  const input = $('#input');

  // 输入框随内容增长；上限随窗口高度变化（默认窗口约 10 行，小窗口自动收紧）。
  // 之前固定 120px（约 5 行），内容再多就只剩内部滚动，容易被误以为“不会变高”。
  function inputMaxHeight() {
    return Math.min(240, Math.max(120, Math.floor(window.innerHeight * 0.32)));
  }
  function autoResizeInput() {
    // 空输入永远回到 CSS 的 34px：面板展开/窗口 resize 时不能拿
    // 未稳定布局下的 scrollHeight 把空框撑高。
    if (!input.value) {
      input.style.height = '';
      return;
    }
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, inputMaxHeight()) + 'px';
  }
  input.addEventListener('input', autoResizeInput);
  input.addEventListener('compositionend', autoResizeInput);
  window.addEventListener('resize', autoResizeInput);
  input.addEventListener('keydown', (e) => {
    if (handleAtKey(e)) return;              // @ 补全下拉打开时优先接管方向键/回车
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();                    // Enter 发送（Shift+Enter 换行）
      $('#input-bar').dispatchEvent(new Event('submit', { cancelable: true }));
    }
  });
  // 输入框粘贴（Ctrl+V）：图片/文件 → 挂载为待发送（同拖拽/截图/加号）；
  // 纯文本没有 files → 不拦截，保持原生粘贴
  input.addEventListener('paste', async (e) => {
    const dt = e.clipboardData;
    if (!dt || !dt.files || !dt.files.length) return;
    e.preventDefault();
    const files = await window.fileDrop.handleClipboardFiles(dt.files);
    if (files.length) window.planner.mountFiles(files);
  });

  // 键盘焦点兜底（Windows）：原生对话框（旧版遗留）/窗口切换等边缘情况会把键盘
  // 焦点带走——"鼠标能点、输入框打不了字"（document.hasFocus() 都还是 true，
  // 只有 OS 级焦点丢了，所以这里不判断条件、点击输入区就重申一次焦点）
  if ($('#input-bar')) {
    $('#input-bar').addEventListener('mousedown', () => {
      window.planner.refocusWindow();
    }, true);
  }

  $('#input-bar').addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    autoResizeInput();                       // 空值 → 回到单行高度
    sendBtn.classList.add('stopping');       // 立即变"停止"（防快速双击重复提交）
    sendBtn.textContent = '停止';
    try {
      // 发送时自动附带全局挂载文件（拖入的文件），发送后清空
      const mounted = await window.planner.getMounted();
      const r = await post('/chat', { message: text, files: mounted });
      if (mounted && mounted.length) window.planner.clearMounted();
      addUserMessage(text, r.msg_id || '', true, r.attachments || []);
    } catch (err) {
      addMessage('（连接后端失败，稍后重试）', 'log');
      sendBtn.classList.remove('stopping');
      sendBtn.textContent = '发送';
    }
  });

  // ── 拖拽文件挂载：拖到面板任意处 → 主进程挂载（对话框上方文件条显示）──
  const mountedBar = $('#mounted-bar');
  let dropOverlayTimer = null;
  document.addEventListener('dragover', (e) => {
    e.preventDefault();                       // 必须，否则 Chromium 拒绝 drop
    document.body.classList.add('drop-overlay');
    if (dropOverlayTimer) { clearTimeout(dropOverlayTimer); dropOverlayTimer = null; }
  });
  document.addEventListener('dragleave', () => {
    if (!dropOverlayTimer) {
      dropOverlayTimer = setTimeout(() => {
        document.body.classList.remove('drop-overlay');
        dropOverlayTimer = null;
      }, 120);
    }
  });
  document.addEventListener('drop', async (e) => {
    e.preventDefault();
    document.body.classList.remove('drop-overlay');
    if (dropOverlayTimer) { clearTimeout(dropOverlayTimer); dropOverlayTimer = null; }
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    const files = await window.fileDrop.handleFiles(e.dataTransfer.files);
    if (files.length) window.planner.mountFiles(files);
  });

  // 输入框上方"已挂载文件"条：名称列表 + 逐个移除 + 清除
  // 渲染逻辑独立成函数：既响应实时广播，也支持展开时主动拉取
  // （悬浮球形态挂载时面板窗口未创建，收不到广播，展开后需主动同步）
  function renderMountedBar(files) {
    const list = files || [];
    mountedBar.classList.toggle('hidden', !list.length);
    if (!list.length) { mountedBar.textContent = ''; return; }
    mountedBar.innerHTML = '<span class="mb-label">已挂载</span>';
    list.forEach((f, i) => {
      const chip = document.createElement('span');
      chip.className = 'mb-chip';
      chip.textContent = f.name || '未命名';
      chip.title = f.path || '';
      const x = document.createElement('button');
      x.className = 'mb-x';
      x.textContent = '×';
      x.addEventListener('click', () => window.planner.removeMounted(i));
      chip.appendChild(x);
      mountedBar.appendChild(chip);
    });
    const clearBtn = document.createElement('button');
    clearBtn.className = 'mb-clear';
    clearBtn.textContent = '清除';
    clearBtn.addEventListener('click', () => window.planner.clearMounted());
    mountedBar.appendChild(clearBtn);
  }
  window.planner.onMountedChanged(renderMountedBar);

  // 加号：菜单（截屏 / 添加本地文件）——都挂进 mounted（待发送区）
  if ($('#plus-btn')) {
    $('#plus-btn').addEventListener('click', () => {
      toggleMenu('#plus-menu', $('#plus-btn'));
    });
    $('#plus-shot').addEventListener('click', () => {
      closePopovers('');
      window.planner.startScreenshot();
    });
    $('#plus-file').addEventListener('click', async () => {
      closePopovers('');
      const files = await window.planner.pickFiles();
      if (files && files.length) window.planner.mountFiles(files);
    });
  }

  // ── 语音输入：面板里用 🎤 按钮（点击开始 / 再点结束→转文字进输入框）；
  //    悬浮球长按录音仍见 bubble.js。发送按钮不再长按录音（2026-10-17 起）。──

  // ── 标题栏拖拽（仅 titlebar 本体，按钮区域除外）────────
  (function setupTitlebarDrag() {
    const bar = $('#titlebar');
    let dragging = false;
    let startX = 0, startY = 0, winX = 0, winY = 0;
    let moved = 0;
    bar.addEventListener('mousedown', async (e) => {
      if (e.button !== 0 || e.target.closest('button')) return;
      dragging = true;
      moved = 0;
      startX = e.screenX;
      startY = e.screenY;
      try {
        const pos = await window.planner.getPanelPos();
        winX = pos.x; winY = pos.y;
      } catch { /* 忽略 */ }
    });
    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const dx = e.screenX - startX;
      const dy = e.screenY - startY;
      moved = Math.max(moved, Math.abs(dx), Math.abs(dy));
      if (moved > 4) window.planner.movePanel(winX + dx, winY + dy);
    });
    document.addEventListener('mouseup', () => { dragging = false; });
  })();

  // ── 图片消息（show_image：工作区图片 / 网络图片直链）──
  // file:// 页面不能直连 http 图片（同音频），经主进程下载成本地文件后显示。
  function addImage(url, caption) {
    closeToolGroup();   // 图片算普通内容：工具组封口
    const el = document.createElement('div');
    el.className = 'msg img-msg';
    const body = document.createElement('div');
    body.className = 'msg-body';
    el.appendChild(body);
    const wrap = document.createElement('div');
    wrap.className = 'img-wrap';
    const ph = document.createElement('div');
    ph.className = 'img-loading';
    ph.textContent = '（图片加载中…）';
    wrap.appendChild(ph);
    body.appendChild(wrap);
    if (caption) {
      const cap = document.createElement('div');
      cap.className = 'img-caption';
      cap.textContent = caption;
      body.appendChild(cap);
    }
    wrap.addEventListener('click', () => wrap.classList.toggle('zoomed'));
    messages.appendChild(el);
    scrollMessagesToBottom();
    window.planner.imageFile(url).then((fileUrl) => {
      if (!fileUrl) { ph.textContent = '（图片加载失败）'; return; }
      const img = document.createElement('img');
      img.alt = caption || '图片';
      img.onload = () => { ph.remove(); };
      img.onerror = () => { ph.textContent = '（图片加载失败）'; img.remove(); };
      img.src = fileUrl;
      wrap.appendChild(img);
    }).catch(() => { ph.textContent = '（图片加载失败）'; });
    return el;
  }

  // 本轮改动过的文件快捷块（回复下面）：**默认收成一行**（与工具组行同款观感）——
  // 「本轮改动 N 个文件 · 文件名1、文件名2…」按改动次数降序、超出一行从尾部截断；
  // 点这一行展开：每文件一行 + 「打开」「目录」按钮。后端聚合（changed_files 事件 /
  // /history 的 role=changed_files），纯代码链路、无模型参与。
  function addChangedFiles(files) {
    const list = Array.isArray(files) ? files : [];
    if (!list.length) return null;
    const el = document.createElement('div');
    el.className = 'msg changed-files';
    const box = document.createElement('div');
    box.className = 'cf-box';
    const line = document.createElement('div');
    line.className = 'cf-line';
    line.title = '点击展开/收起改动文件';
    const names = list.map((f) => f.rel || f.name || f.path || '').join('、');
    line.textContent = `本轮改动 ${list.length} 个文件 · ${names}`;
    line.addEventListener('click', () => el.classList.toggle('open'));
    box.appendChild(line);
    const body = document.createElement('div');
    body.className = 'cf-body';
    list.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'cf-row';
      const name = document.createElement('span');
      name.className = 'cf-name';
      name.textContent = f.rel || f.name || f.path || '';
      name.title = f.path || '';
      row.appendChild(name);
      if (f.count > 1) {
        const cnt = document.createElement('span');
        cnt.className = 'cf-count';
        cnt.textContent = '×' + f.count;
        row.appendChild(cnt);
      }
      if (f.missing) {
        const tag = document.createElement('span');
        tag.className = 'cf-missing';
        tag.textContent = '已不存在';
        row.appendChild(tag);
      } else {
        const openBtn = document.createElement('button');
        openBtn.className = 'cf-btn';
        openBtn.textContent = '打开';
        openBtn.title = '直接打开这个文件';
        openBtn.addEventListener('click', async () => {
          const r = await window.planner.openInExplorer(f.path);
          if (!r || !r.ok) addMessage(`（打开失败：${f.path}）`, 'log');
        });
        row.appendChild(openBtn);
      }
      const dirBtn = document.createElement('button');
      dirBtn.className = 'cf-btn';
      dirBtn.textContent = '目录';
      dirBtn.title = '打开所在目录';
      dirBtn.addEventListener('click', async () => {
        const r = await window.planner.showInFolder(f.path);
        if (!r || !r.ok) addMessage(`（打开失败：${f.path}）`, 'log');
      });
      row.appendChild(dirBtn);
      body.appendChild(row);
    });
    box.appendChild(body);
    el.appendChild(box);
    messages.appendChild(el);
    scrollMessagesToBottom();
    return el;
  }

  // ── 事件处理（/dequeue）──────────────────────────────
  // 流式：text_stream 逐字追加（msg_id 区分多轮）；text 完整文本已由
  // 流式渲染过，面板忽略（气泡 toast 用）。
  const streamMsgs = {};
  const toolLines = {};     // tool 事件 msg/call id → 工具行元素
  const thinkBlocks = {};   // thinking_stream：msg_id → 思考行元素

  // 思考过程行（DeepSeek reasoning_content 流）：**默认单行折叠**——
  // 那一行本身就是窗口：新内容不断进入、旧内容向左移出（尾部对齐滚动）；
  // 点击展开看全文。历史重建（/history 的 thinking 条目）与实时流共用同一套组件。
  function createThinkingBlock() {
    const el = document.createElement('div');
    el.className = 'thinking-block collapsed';
    el.innerHTML = `
      <div class="tb-line" title="点击展开/折叠全文">
        <span class="tb-label">思考过程</span>
        <span class="tb-ticker"></span>
      </div>
      <div class="tb-body"></div>`;
    el.querySelector('.tb-line').addEventListener('click', () => el.classList.toggle('collapsed'));
    messages.appendChild(el);
    return el;
  }

  function appendThinking(el, text) {
    const ticker = el.querySelector('.tb-ticker');
    const body = el.querySelector('.tb-body');
    if (ticker) {
      ticker.textContent += text;
      ticker.scrollLeft = ticker.scrollWidth;   // 窗口只显示最新一段
    }
    if (body) body.textContent += text;
    scrollMessagesToBottom();
  }

  function thinkingStream(ev) {
    let el = thinkBlocks[ev.msg_id];
    if (!el) {
      closeToolGroup();   // 思考行算普通内容：工具组封口
      el = createThinkingBlock();
      thinkBlocks[ev.msg_id] = el;
    }
    appendThinking(el, ev.content);
  }

  function collapseThinking() {
    // 回答文本开始/轮次结束 → 手动展开过的思考行收回（默认本来就是折叠）
    document.querySelectorAll('.thinking-block:not(.collapsed)').forEach((el) => {
      el.classList.add('collapsed');
    });
  }

  // 工具调用行：与思考过程同款单行样式（不再是卡片）——
  // 一行显示「工具 · 目标 · 状态 · 结果首行」，点击展开参数与完整结果。
  // 工具显示名映射（英文工具名 → 中文展示）
  const TOOL_DISPLAY = { heartbeat: '设置定时唤醒' };
  // 代码类工具：参数/结果按代码样式渲染（路径头 / 行号 / 不折行横滚）
  const CODE_TOOLS = new Set(['read_file', 'write_file', 'edit_file', 'create_dir',
                              'move_file', 'delete_file', 'glob', 'grep', 'run_command']);
  const CODE_RESULT_TOOLS = new Set(['read_file', 'grep', 'glob', 'run_command']);

  function toolDisplayName(name) {
    return TOOL_DISPLAY[name] || name;
  }

  function toolTarget(name, args) {
    const a = args || {};
    if (name === 'move_file') return `${a.src || '?'} → ${a.dst || '?'}`;
    if (name === 'grep') return `${a.pattern || ''}${a.include ? '  (' + a.include + ')' : ''}`;
    if (name === 'glob') return a.pattern || '';
    if (name === 'run_command') return `$ ${String(a.command || '').slice(0, 200)}`;
    if (name === 'ask_user') return String(a.question || '').slice(0, 60);
    return a.path || '';
  }

  function toolArgsSummary(name, args) {
    // write/edit 的参数太大（整份内容）→ 折叠成摘要；diff 形态给 edit_file
    const a = args || {};
    if (name === 'write_file') {
      const c = String(a.content || '');
      const linesArr = c.split('\n');
      const head = linesArr.slice(0, 5).join('\n');
      return `共 ${c.length} 字符\n${head}${linesArr.length > 5 ? '\n…' : ''}`;
    }
    if (name === 'edit_file') {
      const oldLines = String(a.old_string || '').split('\n');
      const newLines = String(a.new_string || '').split('\n');
      const more = (oldLines.length + newLines.length > 2) ? '\n…' : '';
      return `- ${oldLines[0].slice(0, 120)}\n+ ${newLines[0].slice(0, 120)}${more}`;
    }
    return null;   // 其他工具显示完整 JSON（代码工具的 target 行已带主要参数）
  }

  function renderCodeLines(text, maxLines = 400) {
    const lines = String(text || '').split('\n');
    const shown = lines.slice(0, maxLines);
    const body = shown.map((line, i) =>
      `<div class="code-line"><span class="ln">${i + 1}</span>`
      + `<span class="tx">${escapeHtml(line) || '&nbsp;'}</span></div>`).join('');
    const more = lines.length > maxLines
      ? `<div class="code-line"><span class="ln">…</span>`
        + `<span class="tx">（其余 ${lines.length - maxLines} 行已省略）</span></div>`
      : '';
    return `<div class="code-block">${body}${more}</div>`;
  }

  // ── 工具行：单行样式（与思考过程行同款；点整行展开参数与完整结果）──
  function resultPreview(name, content) {
    const text = String(content || '');
    const first = (text.trim().split('\n')[0] || '').trim();
    if (!first) return '（无输出）';
    return first.length > 90 ? first.slice(0, 90) + '…' : first;
  }

  // 修改前后对比：+新增/−删除 统计（折叠行的预览）；判断是不是 unified diff
  function diffStat(diff) {
    let add = 0; let del = 0;
    String(diff).split('\n').forEach((l) => {
      if (l.startsWith('+++') || l.startsWith('---')) return;
      if (l.startsWith('+')) add += 1;
      else if (l.startsWith('-')) del += 1;
    });
    return `+${add} −${del}`;
  }

  function diffLooksLikeUnified(diff) {
    const s = String(diff || '');
    return s.includes('@@') || /(^|\n)\+[^+]/.test(s) || s.startsWith('---');
  }

  // ── 工具组：相邻工具调用自动合并成一行（默认折叠）——
  // 收起时单行显示「N 个操作 · 工具名…」（同一行，省空间）；
  // 展开后每个工具**各占一行**（每行仍可单独点开看参数/结果）。
  let toolGroup = null;       // 当前正在累积的工具组（遇普通消息封组）
  function closeToolGroup() { toolGroup = null; }

  function newToolGroup() {
    const g = document.createElement('div');
    g.className = 'tool-group';
    g.innerHTML = `
      <div class="tg-line" title="点击展开/收起工具列表">
        <span class="tg-count"></span>
        <span class="tg-title"></span>
        <span class="tg-status"></span>
      </div>
      <div class="tg-body"></div>`;
    g.querySelector('.tg-line').addEventListener('click', () => {
      g.classList.toggle('open');
    });
    messages.appendChild(g);
    toolGroup = g;
    return g;
  }

  function updateToolGroup(g) {
    if (!g) return;
    const lines = [...g.querySelectorAll('.tool-line')];
    if (!lines.length) return;
    // 工具名列表：**按调用顺序全量列出**（不设上限、不做去重/计数）；窗口放不下时
    // 靠滑动窗口只露出尾部（最新的在右端，和思考行一致）
    const nameText = lines
      .map((el) => toolDisplayName(el.dataset.tool || '?'))
      .join(' / ');
    const running = lines.filter((el) => el.classList.contains('running'));
    const count = g.querySelector('.tg-count');
    const title = g.querySelector('.tg-title');
    const status = g.querySelector('.tg-status');
    if (running.length) {
      count.textContent = `正在执行 ${running[running.length - 1].dataset.tool || ''}…（${lines.length - running.length}/${lines.length}）`;
      status.textContent = '';
      // 单个工具执行中：名字已在 count 里，标题不再重复
      title.textContent = lines.length === 1 ? '' : nameText;
    } else {
      count.textContent = `${lines.length} 个操作`;
      status.textContent = '✓';
      title.textContent = nameText;
    }
    title.scrollLeft = title.scrollWidth;   // 滑动窗口：太多时只显示尾部
    // 单个操作：隐藏组头，直接显示那一行（保持"一条就是一行"的观感）
    g.classList.toggle('single', lines.length === 1 && !running.length);
  }

  function addToolLine(name, args, id, done) {
    const el = document.createElement('div');
    el.className = 'tool-line' + (done ? ' done' : ' running');
    el.dataset.tool = name;
    const target = toolTarget(name, args);
    const isCode = CODE_TOOLS.has(name);
    const summary = isCode ? toolArgsSummary(name, args) : null;
    const argsText = args ? JSON.stringify(args, null, 1) : '';
    el.innerHTML = `
      <div class="tl-line" title="点击展开参数与结果">
        <span class="tl-name">${escapeHtml(toolDisplayName(name))}</span>
        <span class="tl-target">${escapeHtml(target)}</span>
        <span class="tl-status">${done ? '✓' : '<span class="tl-spinner"></span>执行中…'}</span>
        <span class="tl-preview"></span>
      </div>
      <div class="tl-body hidden">
        ${summary != null
          ? `<div class="tool-sec">参数</div><pre class="tool-pre">${escapeHtml(summary)}</pre>`
          : (!isCode && argsText ? `<div class="tool-sec">参数</div><pre class="tool-pre">${escapeHtml(argsText)}</pre>` : '')}
        <div class="tool-sec">结果</div><pre class="tool-pre tool-result">…</pre>
      </div>`;
    el.querySelector('.tl-line').addEventListener('click', () => {
      el.classList.toggle('expanded');
      el.querySelector('.tl-body').classList.toggle('hidden', !el.classList.contains('expanded'));
    });
    if (id) toolLines[id] = el;
    const g = toolGroup || newToolGroup();
    g.querySelector('.tg-body').appendChild(el);
    updateToolGroup(g);
    scrollMessagesToBottom();
    return el;
  }

  function setToolResult(id, content, diff) {
    const el = toolLines[id];
    if (!el) return;
    el.classList.remove('running');
    el.classList.add('done');
    const status = el.querySelector('.tl-status');
    if (status) status.textContent = '✓';
    const preview = el.querySelector('.tl-preview');
    const name = el.dataset.tool || '';
    const hasDiff = !!diff && String(diff).trim();
    if (hasDiff && diffLooksLikeUnified(diff)) {
      // 写操作：折叠行预览显示 +新增/−删除；展开体加「修改（前 → 后）」对比块
      if (preview) preview.textContent = diffStat(diff);
      const body = el.querySelector('.tl-body');
      const resultPre = body ? body.querySelector('.tool-result') : null;
      if (body && !body.querySelector('.tool-diff')) {
        const wrap = document.createElement('div');
        wrap.innerHTML = '<div class="tool-sec">修改（前 → 后）</div>'
          + '<div class="tool-diff"></div>';
        wrap.querySelector('.tool-diff').innerHTML = renderDiff(diff);
        const resSec = resultPre ? resultPre.previousElementSibling : null;
        if (resSec && resSec.classList.contains('tool-sec')) {
          body.insertBefore(wrap, resSec);   // 参数 → 修改 → 结果
        } else {
          body.appendChild(wrap);
        }
      }
    } else if (hasDiff) {
      // 非 unified（如 move 的说明句）：按普通说明显示
      if (preview) preview.textContent = String(diff).slice(0, 60);
      const body = el.querySelector('.tl-body');
      if (body && !body.querySelector('.tool-diff')) {
        const wrap = document.createElement('div');
        wrap.innerHTML = '<div class="tool-sec">修改</div>'
          + `<div class="tool-diff tool-diff-note">${escapeHtml(String(diff))}</div>`;
        body.appendChild(wrap);
      }
    } else if (preview) {
      preview.textContent = resultPreview(name, content);
    }
    const pre = el.querySelector('.tool-result');
    if (CODE_RESULT_TOOLS.has(name) && content && pre) {
      pre.outerHTML = renderCodeLines(content);   // 代码结果：行号 + 横滚
    } else if (pre) {
      pre.textContent = content;
    }
    updateToolGroup(el.closest('.tool-group'));
  }

  // ── 轻量 Markdown 渲染（加粗 + 行内代码 + 代码块）────────
  // 公共实现见 md.js（先 escapeHtml 再替换标记，XSS 安全）。

  // ── HTML 预览卡（```html 代码块 → 沙箱 iframe）──────────────
  // 安全设计：
  // - 预览文档由后端 http 提供（/preview）——srcdoc 会继承面板 CSP（script-src 'self'）
  //   导致内联脚本被拦；http 文档不继承，由后端注入**收紧版 CSP**（connect-src 'none'
  //   断掉调后端 API 的路，form/嵌套禁用）；
  // - iframe 带 sandbox="allow-scripts"（**绝不给 allow-same-origin**）→ 拿不到
  //   父页面 DOM / window.planner（无任何应用权限）。
  function enhanceHtmlPreviews(root) {
    root.querySelectorAll('pre.md-code[data-lang="html"], pre.md-code[data-lang="htm"]')
      .forEach((pre) => {
        const code = (pre.querySelector('code') || pre).textContent || '';
        if (!code.trim()) return;
        const wrap = document.createElement('div');
        wrap.className = 'html-preview';
        const bar = document.createElement('div');
        bar.className = 'hp-bar';
        const title = document.createElement('span');
        title.className = 'hp-title';
        title.textContent = 'HTML 预览（沙箱可交互）';
        const srcBtn = document.createElement('button');
        srcBtn.type = 'button';
        srcBtn.className = 'hp-btn';
        srcBtn.textContent = '查看源码';
        const bigBtn = document.createElement('button');
        bigBtn.type = 'button';
        bigBtn.className = 'hp-btn';
        bigBtn.textContent = '放大';
        bar.appendChild(title);
        bar.appendChild(srcBtn);
        bar.appendChild(bigBtn);
        const frame = document.createElement('iframe');
        frame.className = 'hp-frame';
        frame.setAttribute('sandbox', 'allow-scripts');
        pre.classList.add('hidden');   // 源码默认收起
        wrap.appendChild(bar);
        wrap.appendChild(frame);
        pre.parentNode.insertBefore(wrap, pre);   // 卡片放到 pre 原位置
        wrap.appendChild(pre);                    // 再把源码 pre 移进卡片底部
        srcBtn.addEventListener('click', () => {
          pre.classList.toggle('hidden');
          srcBtn.textContent = pre.classList.contains('hidden') ? '查看源码' : '收起源码';
        });
        bigBtn.addEventListener('click', () => {
          wrap.classList.toggle('expanded');
          bigBtn.textContent = wrap.classList.contains('expanded') ? '还原' : '放大';
        });
        // 寄存到后端换一个可加载的 URL（大小超限/后端不可用时给提示，源码仍可看）
        window.planner.apiFetch('/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ html: code }),
        }).then((r) => JSON.parse(r.text)).then((d) => {
          if (d && d.ok && d.url) {
            frame.src = window.planner.apiBase + d.url;
          } else {
            const note = document.createElement('div');
            note.className = 'hp-error';
            note.textContent = '（预览不可用，可查看源码）';
            bar.appendChild(note);
          }
        }).catch(() => { /* 后端不可用：保留源码按钮 */ });
      });
  }

  // ── 审批弹窗（代码工作区：写操作 / 工作区外读）────────────
  // 后端 push approval_request → 弹窗；按钮回 POST /approval（超时后端默认拒绝）。
  let approvalReqId = null;
  let approvalTimer = null;

  // 统一 diff 着色（+绿 / −红 / @@灰 / 文件头）——逐行构建，内容全部转义
  function renderDiff(text) {
    return String(text || '').split('\n').map((line) => {
      let cls = 'diff-no';
      if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-file';
      else if (line.startsWith('@@')) cls = 'diff-hunk';
      else if (line.startsWith('+')) cls = 'diff-add';
      else if (line.startsWith('-')) cls = 'diff-del';
      return `<div class="diff-line ${cls}">${escapeHtml(line) || '&nbsp;'}</div>`;
    }).join('');
  }

  function showApproval(ev) {
    const modal = $('#approval');
    if (!modal) return;
    approvalReqId = ev.id || null;
    $('#approval-tool').textContent = ev.tool || '';
    const agentEl = $('#approval-agent');
    if (agentEl) {
      const fromWhite = ev.agent === 'white';
      agentEl.textContent = fromWhite ? '来自 White（子代理）' : '';
      agentEl.classList.toggle('hidden', !fromWhite);
    }
    const box = $('#approval-paths');
    box.innerHTML = '';
    (ev.paths || []).forEach((p) => {
      const div = document.createElement('div');
      div.className = 'approval-path';
      div.textContent = p;
      box.appendChild(div);
    });
    const preview = $('#approval-preview');
    if (ev.preview) {
      preview.innerHTML = renderDiff(ev.preview);
      preview.classList.remove('hidden');
    } else {
      preview.innerHTML = '';
      preview.classList.add('hidden');
    }
    // 「记住并允许」的范围单选（仅此文件 / 此目录 / 整个工作区）
    const scopeBox = $('#approval-scopes');
    scopeBox.innerHTML = '';
    (ev.scopes || []).forEach((sc, i) => {
      const label = document.createElement('label');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'approval-scope';
      radio.value = sc.kind || 'file';
      if (i === 0) radio.checked = true;
      label.appendChild(radio);
      const span = document.createElement('span');
      span.textContent = sc.label || sc.kind || 'file';
      label.appendChild(span);
      scopeBox.appendChild(label);
    });
    scopeBox.classList.toggle('hidden', !(ev.scopes || []).length);
    // 待批徽标 + 倒计时（超时 → 后端默认拒绝）
    $('#approval-badge').classList.remove('hidden');
    startApprovalCountdown(ev.timeout || 120);
    modal.classList.remove('hidden');
  }

  function startApprovalCountdown(seconds) {
    if (approvalTimer) clearInterval(approvalTimer);
    let left = Math.max(0, Math.round(seconds));
    const el = $('#approval-countdown');
    const tick = () => {
      if (el) el.textContent = `${left}s`;
      if (left <= 0 && approvalTimer) {
        clearInterval(approvalTimer);
        approvalTimer = null;
      }
      left -= 1;
    };
    tick();
    approvalTimer = setInterval(tick, 1000);
  }

  function hideApproval() {
    approvalReqId = null;
    if (approvalTimer) {
      clearInterval(approvalTimer);
      approvalTimer = null;
    }
    $('#approval-badge').classList.add('hidden');
    const modal = $('#approval');
    if (modal) modal.classList.add('hidden');
  }

  function selectedScope() {
    const r = document.querySelector('input[name="approval-scope"]:checked');
    return r ? r.value : '';
  }

  function resolveApproval(decision) {
    const id = approvalReqId;
    const scope = decision === 'always' ? selectedScope() : '';
    hideApproval();
    if (!id) return;
    window.planner.apiFetch('/approval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, decision, scope }),
    }).catch(() => { /* 后端不可用：等超时 */ });
  }

  // 按钮绑定加空值保护：弹窗标记被挪走/改名时不能连带拖死整个 app.js
  // （历史坑：弹窗 DOM 曾放在 <script> 之后，app.js 加载即 TypeError → 面板全哑）
  [['#approval-once', 'allow_once'],
   ['#approval-always', 'always'],
   ['#approval-deny', 'deny']].forEach(([sel, decision]) => {
    const btn = $(sel);
    if (btn) btn.addEventListener('click', () => resolveApproval(decision));
  });

  // ── ask_user 提问卡（对话区内联卡片）─────────────────────────
  // 后端 push question_request → 在消息流尾部插入一张提问卡（选项 / 自由输入）；
  // 回答 → POST /question；`question_resolved`（回答或超时）→ 卡片收成一行状态。
  // 设计（用户反馈 2026-10-17）：原来全屏遮罩弹窗会挡住对话内容（想参考上文时
  // 反而被挡）——改为对话区的一部分，不遮挡任何其它内容。
  let questionTimer = null;
  let activeQuestionCard = null;

  function questionCard(id) {
    return Array.from(document.querySelectorAll('.question-inline'))
      .find((el) => el.dataset.id === String(id || '')) || null;
  }

  function settleQuestionCard(card, statusText) {
    if (!card || card.classList.contains('resolved')) return;
    card.classList.add('resolved');
    if (card === activeQuestionCard && questionTimer) {
      clearInterval(questionTimer);
      questionTimer = null;
      activeQuestionCard = null;
    }
    const controls = card.querySelector('.q-controls');
    if (controls) controls.remove();
    const note = card.querySelector('.q-note');
    if (note) {
      note.textContent = statusText;
      note.classList.add('done');
    }
  }

  function showQuestion(ev) {
    const id = ev.id || null;
    // 上一张未决卡（理论上不该同时出现两张）直接标记失效，防串台
    document.querySelectorAll('.question-inline:not(.resolved)').forEach((el) => {
      if (el.dataset.id !== String(id || '')) settleQuestionCard(el, '（被新的提问取代）');
    });
    const card = document.createElement('div');
    card.className = 'msg question-inline';
    card.dataset.id = String(id || '');
    const head = document.createElement('div');
    head.className = 'q-head';
    head.textContent = '小助想问你';
    const text = document.createElement('div');
    text.className = 'question-text';
    text.textContent = ev.question || '';
    const controls = document.createElement('div');
    controls.className = 'q-controls';
    const options = document.createElement('div');
    options.className = 'question-options';
    (ev.options || []).forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = opt;
      btn.addEventListener('click', () => resolveQuestion(card, { selected: opt }));
      options.appendChild(btn);
    });
    const row = document.createElement('div');
    row.className = 'question-input-row';
    const qInput = document.createElement('input');
    qInput.className = 'q-input';
    qInput.type = 'text';
    qInput.placeholder = '也可以直接打字回答…';
    qInput.autocomplete = 'off';
    const qSend = document.createElement('button');
    qSend.type = 'button';
    qSend.className = 'q-send';
    qSend.textContent = '发送';
    const sendAnswer = () => {
      if (qInput.value.trim()) resolveQuestion(card, { fromInput: true, input: qInput });
    };
    qSend.addEventListener('click', sendAnswer);
    qInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); sendAnswer(); }
    });
    row.appendChild(qInput);
    row.appendChild(qSend);
    const note = document.createElement('div');
    note.className = 'q-note';
    note.innerHTML = '不回答则 <b class="q-countdown">…</b> 后自动跳过。';
    controls.appendChild(options);
    controls.appendChild(row);
    card.appendChild(head);
    card.appendChild(text);
    card.appendChild(controls);
    card.appendChild(note);
    messages.appendChild(card);
    scrollMessagesToBottom(true);          // 等待用户动作 → 强制拉底提醒
    activeQuestionCard = card;
    startQuestionCountdown(card, ev.timeout || 120);
    qInput.focus();
  }

  function startQuestionCountdown(card, seconds) {
    if (questionTimer) { clearInterval(questionTimer); questionTimer = null; }
    let left = Math.max(0, Math.round(seconds));
    const el = card.querySelector('.q-countdown');
    const tick = () => {
      if (el) el.textContent = `${left}s`;
      if (left <= 0 && questionTimer) {
        clearInterval(questionTimer);
        questionTimer = null;
      }
      left -= 1;
    };
    tick();
    questionTimer = setInterval(tick, 1000);
  }

  function resolveQuestion(card, payload) {
    const id = card ? card.dataset.id : null;
    settleQuestionCard(card, payload.fromInput ? '（已提交你的回答）' : `（已选择：${payload.selected}）`);
    if (!id) return;
    const body = { id, selected: payload.selected || '' };
    if (payload.fromInput && payload.input) body.answer = payload.input.value.trim();
    window.planner.apiFetch('/question', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => { /* 后端不可用：等超时 */ });
  }

  // ── 应用内确认框（替代 window.confirm）────────────────────────
  // 原因（用户在 Windows 实机遇到）：原生 confirm 关闭后键盘焦点可能不还给页面——
  // 鼠标能点按钮但输入框打不了字，要"任务栏最小化再还原"才恢复。一律走应用内弹窗。
  let uiConfirmResolve = null;
  function uiConfirm(message, opts = {}) {
    const modal = $('#ui-confirm');
    if (!modal) return Promise.resolve(window.confirm(message));   // DOM 缺失兜底
    $('#ui-confirm-title').textContent = opts.title || '请确认';
    $('#ui-confirm-text').textContent = String(message || '');
    $('#ui-confirm-ok').textContent = opts.okText || '确定';
    modal.classList.remove('hidden');
    return new Promise((resolve) => { uiConfirmResolve = resolve; });
  }
  function settleUiConfirm(ok) {
    const modal = $('#ui-confirm');
    if (modal) modal.classList.add('hidden');
    const r = uiConfirmResolve;
    uiConfirmResolve = null;
    if (r) r(ok);
  }
  if ($('#ui-confirm-ok')) {
    $('#ui-confirm-ok').addEventListener('click', () => settleUiConfirm(true));
    $('#ui-confirm-cancel').addEventListener('click', () => settleUiConfirm(false));
    document.addEventListener('keydown', (e) => {
      if (!uiConfirmResolve) return;
      if (e.key === 'Escape') { e.preventDefault(); settleUiConfirm(false); }
      else if (e.key === 'Enter') { e.preventDefault(); settleUiConfirm(true); }
    });
  }

  // ── 输入框下方状态条：目录 / 权限模式 / 模型 / 思考强度 ──────
  const MODE_LABEL = { ask: 'Ask', auto: 'Auto', read_only: '只读' };
  const EFFORT_LABEL = { low: '少想', high: '默认', max: '多想' };
  const wsState = { workspace: '', cwd: '', recent: [], permission_mode: 'ask' };
  const llmState = { provider: 'deepseek', provider_label: 'DeepSeek 官方', model: '',
                     providers: [], effort: 'high', thinking: 'adaptive',
                     live_provider: '', live_models: [],
                     stats: null, stats_records: [] };
  let wsBrowse = '';        // 工作目录面板当前浏览的目录（绝对路径）
  let lastCtx = null;       // 最近一次 /state 的上下文统计（分解弹层用）

  function baseName(p) {
    const s = String(p || '').replace(/[\\/]+$/, '');
    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i >= 0 ? s.slice(i + 1) : s;
  }

  function shortPath(p) {
    const s = String(p || '');
    return s.length <= 42 ? s : '…' + s.slice(s.length - 41);
  }

  function joinPath(base, name) {
    if (!base) return name;
    return base.replace(/[\\/]+$/, '') + (String(base).includes('\\') ? '\\' : '/') + name;
  }

  function renderWsChips() {
    const dir = $('#cb-dir');
    const mode = $('#cb-mode');
    if (!dir) return;
    if (wsState.workspace) {
      dir.classList.remove('hidden');
      dir.textContent = baseName(wsState.cwd || wsState.workspace);
      dir.title = `工作区：${wsState.workspace}\n当前目录：${wsState.cwd || wsState.workspace}`;
      mode.classList.remove('hidden');
      mode.textContent = MODE_LABEL[wsState.permission_mode] || MODE_LABEL.ask;
      mode.className = 'cb-chip' + (wsState.permission_mode === 'ask'
        ? '' : ' ' + wsState.permission_mode);
    } else {
      dir.classList.remove('hidden');
      dir.textContent = '未启用代码';
      dir.title = '点击去设置里选择代码工作区';
      mode.classList.add('hidden');
    }
    document.querySelectorAll('#mode-menu .mode-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.mode === wsState.permission_mode);
    });
  }

  function renderLlmChips() {
    const model = $('#cb-model');
    const effort = $('#cb-effort');
    if (!model) return;
    model.textContent = llmState.model || '模型';
    model.title = `模型：${llmState.provider_label} / ${llmState.model || '—'}（点击切换）`;
    const eff = llmState.effort || 'high';
    effort.textContent = EFFORT_LABEL[eff] || eff;
    effort.title = `思考强度：${eff}（点击切换；思考开关在设置页）`;
    document.querySelectorAll('#effort-menu .mode-item').forEach((el) => {
      el.classList.toggle('active', el.dataset.effort === eff);
    });
  }

  async function refreshWs() {
    try {
      const r = await window.planner.apiFetch('/workspace');
      const d = JSON.parse(r.text);
      if (!d.ok) return;
      wsState.workspace = d.workspace || '';
      wsState.cwd = d.cwd || '';
      wsState.recent = d.recent || [];
      wsState.permission_mode = d.permission_mode || 'ask';
    } catch { /* 后端不可用 */ }
    renderWsChips();
  }

  async function refreshLlm() {
    try {
      const r = await window.planner.apiFetch('/llm/providers');
      const d = JSON.parse(r.text);
      if (d.ok) {
        llmState.provider = d.current.provider;
        llmState.model = d.current.model;
        llmState.providers = d.providers || [];
        const preset = (d.providers || []).find((p) => p.id === d.current.provider);
        llmState.provider_label = preset ? preset.label : d.current.provider;
      }
    } catch { /* 后端不可用 */ }
    try {
      const r = await window.planner.apiFetch('/settings');
      const d = JSON.parse(r.text);
      if (d.ok) {
        llmState.effort = d.settings.reasoning_effort || 'high';
        llmState.thinking = d.settings.thinking_mode || 'adaptive';
      }
    } catch { /* 后端不可用 */ }
    renderLlmChips();
  }

  // ── 状态条合并 chip：原始消息上下文长度 · 缓存命中率 · Token 速度 ──
  //（原「标题栏上下文占用」与「用量统计」合并为一个；命中率/速度取最近 50 轮平均；
  //  点击展开合并面板：上下文分解 + 用量明细）
  function fmtTok(n) {
    n = Number(n) || 0;
    return n >= 10000 ? (n / 1000).toFixed(1) + 'k' : n.toLocaleString();
  }

  function renderStatsChip() {
    const chip = $('#cb-stats');
    if (!chip) return;
    const s = llmState.stats;
    const raw = (lastCtx && lastCtx.breakdown) ? Number(lastCtx.breakdown.raw || 0) : 0;
    const rawText = raw > 0 ? fmtTok(raw) : '—';
    let rate = '—';
    let tps = '—';
    if (s && s.count) {
      rate = (s.cache_rate === null || s.cache_rate === undefined) ? '—' : `${s.cache_rate}%`;
      tps = `${s.avg_tps}`;
    }
    chip.classList.remove('hidden');   // 始终可见（无数据时显示 —）
    chip.textContent = `原始 ${rawText} · 命中 ${rate} · ${tps} t/s`;
    chip.title =
      `原始消息上下文：${raw > 0 ? `约 ${raw.toLocaleString()} token（字符估算；记忆压缩按它判定）` : '—'}`
      + `\n最近 ${(s && s.count) || 0} 轮平均：缓存命中 ${rate} · ${tps} t/s`
      + '\n（点击看上下文分解与用量明细）';
  }

  function renderStatsPanel() {
    const sum = $('#stats-summary');
    const list = $('#stats-list');
    if (!sum || !list) return;
    const s = llmState.stats || {};
    if (!s.count) {
      sum.innerHTML = '';
      list.innerHTML = '<div class="hint">（还没有统计——完成一次对话生成后出现）</div>';
      return;
    }
    const rate = (s.cache_rate === null || s.cache_rate === undefined) ? '端点未报' : `${s.cache_rate}%`;
    const rows = [
      ['缓存命中', rate],
      ['平均 TPS', `${s.avg_tps} t/s`],
      ['平均输入', `${s.avg_in} token`],
      ['平均输出', `${s.avg_out} token`],
    ];
    if (s.avg_reason) rows.push(['平均思考', `${s.avg_reason} token`]);
    sum.innerHTML = rows.map(([k, v]) =>
      `<span class="stat-k">${k}</span><span class="stat-v">${escapeHtml(String(v))}</span>`).join('')
      + '<span class="stat-note">输入/输出为整轮生成（含多轮工具调用）的 API 实报合计；'
      + 'TPS = 输出 token / 整轮耗时；命中率 = 缓存读取 / 输入（端点实报）</span>';
    list.innerHTML = (llmState.stats_records || []).slice().reverse().map((r) => {
      const t = new Date((r.ts || 0) * 1000);
      const hh = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
      const rr = (r.cache_known && r.in > 0) ? `${Math.round((r.hit / r.in) * 100)}%` : '—';
      return `<div class="stats-row"><span class="sr-time">${hh}</span>`
        + `<span class="sr-rate">命中 ${rr}</span>`
        + `<span class="sr-tps">${r.tps} t/s</span>`
        + `<span class="sr-tok">${r.in}→${r.out}</span></div>`;
    }).join('');
  }

  async function refreshStats() {
    try {
      const r = await window.planner.apiFetch('/llm/stats');
      const d = JSON.parse(r.text);
      if (d.ok) {
        llmState.stats = d.summary || null;
        llmState.stats_records = d.records || [];
      }
    } catch { /* 后端不可用 */ }
    renderStatsChip();
    const panel = $('#stats-panel');
    if (panel && !panel.classList.contains('hidden')) renderStatsPanel();
  }

  if ($('#cb-stats')) {
    $('#cb-stats').addEventListener('click', async () => {
      renderCtxPanel();     // 合并面板：上下文分解 + 用量明细
      renderStatsPanel();
      if (toggleMenu('#stats-panel', $('#cb-stats'))) refreshStats();
    });
    $('#stats-close').addEventListener('click', () => closePopovers(''));
  }

  function closePopovers(except) {
    ['#mode-menu', '#ws-panel', '#model-menu', '#effort-menu',
     '#stats-panel', '#plus-menu'].forEach((sel) => {
      const el = $(sel);
      if (el && sel !== except) el.classList.add('hidden');
    });
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('.popover') || e.target.closest('#cb-dir')
        || e.target.closest('#cb-mode') || e.target.closest('#cb-model')
        || e.target.closest('#cb-effort') || e.target.closest('#cb-stats')
        || e.target.closest('#plus-btn')) return;
    // 处理过程中目标被原地重渲染移除（如工作目录面板里点目录项）→ 不算"点了外面"，
    // 否则 closest('.popover') 落空会误关浮层（历史 bug：点目录面板整个消失）
    if (!document.body.contains(e.target)) return;
    closePopovers('');
  });

  // 浮层定位：贴在被点的按钮**正上方**（左侧对齐按钮；右边放不下就左移/收窄）
  const popoverAnchors = {};
  function positionPopover(sel, anchor) {
    const menu = $(sel);
    if (!menu || !anchor) return;
    const panel = document.getElementById('panel-ui');
    if (!panel) return;
    const pr = panel.getBoundingClientRect();
    const ar = anchor.getBoundingClientRect();
    menu.style.visibility = 'hidden';        // 先显示以便量宽度，但不闪
    menu.classList.remove('hidden');
    const mw = menu.offsetWidth || 240;
    let left = ar.left - pr.left;
    const maxLeft = pr.width - mw - 8;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    menu.style.left = Math.round(left) + 'px';
    menu.style.right = 'auto';
    // 菜单底边贴按钮顶部上方 6px（无论状态条在哪一行）
    menu.style.bottom = Math.round(pr.bottom - ar.top + 6) + 'px';
    menu.style.top = 'auto';
    menu.style.visibility = '';
  }

  function toggleMenu(sel, anchor) {
    const menu = $(sel);
    if (!menu) return false;
    const willShow = menu.classList.contains('hidden');
    closePopovers(sel);
    if (!willShow) {
      menu.classList.add('hidden');
      return false;
    }
    if (anchor) popoverAnchors[sel] = anchor;
    positionPopover(sel, anchor || popoverAnchors[sel]);
    return true;
  }

  // 面板尺寸变化时浮层会失去对齐 → 直接收起
  window.addEventListener('resize', () => closePopovers(''));

  // 权限模式菜单（模仿 Codex /permissions）
  if ($('#cb-mode')) {
    $('#cb-mode').addEventListener('click', () => {
      if (!wsState.workspace) {          // 未启用代码 → 直接去设置选工作区
        window.planner.openSettings();
        return;
      }
      toggleMenu('#mode-menu', $('#cb-mode'));
    });
    document.querySelectorAll('#mode-menu .mode-item').forEach((el) => {
      el.addEventListener('click', async () => {
        closePopovers('');
        try {
          await post('/settings', { updates: { permission_mode: el.dataset.mode } });
        } catch { /* 后端不可用 */ }
        await refreshWs();
        addMessage(`（权限模式：${MODE_LABEL[el.dataset.mode] || el.dataset.mode}）`, 'log');
      });
    });
  }

  // 模型菜单：按服务商分组（DeepSeek 官方 / OpenCode Go）；打开时尝试拉实时模型清单
  function renderModelMenu() {
    const menu = $('#model-menu');
    if (!menu) return;
    menu.innerHTML = '<div class="pop-title">模型（按服务商）</div>';
    (llmState.providers || []).forEach((p) => {
      const head = document.createElement('div');
      head.className = 'pop-sec';
      head.textContent = `${p.label} · ${String(p.base_url || '').replace(/^https?:\/\//, '')}`
        + (p.has_key ? '' : '（未配置 Key）');
      menu.appendChild(head);
      const models = (p.id === llmState.live_provider && llmState.live_models.length)
        ? llmState.live_models : (p.models || []);
      models.forEach((m) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'mode-item' + (p.id === llmState.provider && m === llmState.model
          ? ' active' : '');
        btn.innerHTML = `<b>${escapeHtml(m)}</b>`
          + (m === p.default ? '<span>默认</span>' : '');
        btn.addEventListener('click', async () => {
          closePopovers('');
          const updates = { llm_provider: p.id };
          updates[`llm_model_${p.id}`] = m;
          const res = await postJson('/settings', { updates });
          await refreshLlm();
          if (!res.ok) {
            addError('模型切换失败（设置保存出错）。', '去 设置 → 模型 API 检查配置后重试。');
          } else if (!p.has_key) {
            addError(`已切到 ${p.label}，但该服务商还没有配置 API Key。`,
                     '设置 → 模型 API：选中该服务商，填入 Key 后保存。');
          } else {
            addMessage(`（模型切换为 ${p.label} / ${m}）`, 'log');
          }
        });
        menu.appendChild(btn);
      });
    });
  }

  async function loadLiveModels(providerId) {
    try {
      const r = await window.planner.apiFetch(
        '/llm/models?provider=' + encodeURIComponent(providerId));
      const d = JSON.parse(r.text);
      if (d.ok && d.source === 'live' && (d.models || []).length) {
        llmState.live_provider = providerId;
        llmState.live_models = d.models;
        renderModelMenu();
        // 宽度可能因长模型名变化 → 重新对齐
        const menu = $('#model-menu');
        if (menu && !menu.classList.contains('hidden')) {
          positionPopover('#model-menu', popoverAnchors['#model-menu']);
        }
      }
    } catch { /* 离线/无 key → 用预设清单 */ }
  }

  if ($('#cb-model')) {
    $('#cb-model').addEventListener('click', () => {
      if (toggleMenu('#model-menu', $('#cb-model'))) {
        renderModelMenu();
        positionPopover('#model-menu', $('#cb-model'));
        loadLiveModels(llmState.provider);
      }
    });
  }

  // 思考强度菜单（low / high / max；思考开关本身在设置页）
  if ($('#cb-effort')) {
    $('#cb-effort').addEventListener('click', () => {
      toggleMenu('#effort-menu', $('#cb-effort'));
      renderLlmChips();
    });
    document.querySelectorAll('#effort-menu .mode-item').forEach((el) => {
      el.addEventListener('click', async () => {
        closePopovers('');
        try {
          await post('/settings', { updates: { reasoning_effort: el.dataset.effort } });
        } catch { /* 后端不可用 */ }
        await refreshLlm();
        addMessage(`（思考强度：${EFFORT_LABEL[el.dataset.effort] || el.dataset.effort}）`, 'log');
      });
    });
  }

  // 工作目录面板
  function renderWsCrumbs() {
    const box = $('#ws-crumbs');
    if (!box) return;
    box.innerHTML = '';
    const wsRoot = wsState.workspace || '';
    const cur = wsBrowse || wsRoot;
    if (!wsRoot) return;
    const lower = cur.toLowerCase();
    const rootLower = wsRoot.toLowerCase();
    const rel = lower.startsWith(rootLower) ? cur.slice(wsRoot.length) : '';
    const parts = rel.split(/[\\/]+/).filter(Boolean);
    const addCrumb = (label, path, last) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = label;
      btn.title = path;
      if (!last) {
        btn.addEventListener('click', async () => { wsBrowse = path; await renderWsPanel(); });
      } else {
        btn.style.color = '#2b2f38';
      }
      box.appendChild(btn);
    };
    addCrumb(baseName(wsRoot) || wsRoot, wsRoot, parts.length === 0);
    let acc = wsRoot;
    parts.forEach((p, i) => {
      acc = joinPath(acc, p);
      const sep = document.createElement('span');
      sep.textContent = '›';
      box.appendChild(sep);
      addCrumb(p, acc, i === parts.length - 1);
    });
  }

  async function renderWsPanel() {
    renderWsCrumbs();
    const listBox = $('#ws-dirs');
    listBox.innerHTML = '';
    try {
      const r = await window.planner.apiFetch(
        '/workspace/dirs?path=' + encodeURIComponent(wsBrowse || ''));
      const d = JSON.parse(r.text);
      if (d.ok) {
        if (d.path) wsBrowse = d.path;
        (d.dirs || []).forEach((name) => {
          const btn = document.createElement('button');
          btn.className = 'ws-item';
          btn.type = 'button';
          btn.textContent = name + '/';
          btn.addEventListener('click', async () => {
            wsBrowse = joinPath(d.path, name);
            await renderWsPanel();
          });
          listBox.appendChild(btn);
        });
        if (!(d.dirs || []).length) {
          const div = document.createElement('div');
          div.className = 'ws-item dim';
          div.textContent = '（没有子目录）';
          listBox.appendChild(div);
        }
      }
    } catch { /* 后端不可用 */ }
    const recentBox = $('#ws-recent');
    recentBox.innerHTML = '';
    (wsState.recent || []).slice(0, 8).forEach((p) => {
      const btn = document.createElement('button');
      btn.className = 'ws-item';
      btn.type = 'button';
      btn.innerHTML = escapeHtml(baseName(p))
        + ' <span class="dim">' + escapeHtml(shortPath(p)) + '</span>';
      btn.title = p;
      btn.addEventListener('click', () => switchDir(p));
      recentBox.appendChild(btn);
    });
    // 按钮随浏览位置动态化：当前位置已是 cwd → 禁用"设为当前目录"
    const browse = (wsBrowse || '').toLowerCase();
    const curCwd = (wsState.cwd || wsState.workspace || '').toLowerCase();
    const useBtn = $('#ws-use');
    if (useBtn) {
      useBtn.textContent = wsBrowse && browse !== curCwd
        ? `设为当前目录（${baseName(wsBrowse)}）`
        : '设为当前目录';
      useBtn.disabled = !wsBrowse || browse === curCwd;
    }
    const rootBtn = $('#ws-root');
    if (rootBtn) {
      rootBtn.disabled = curCwd === (wsState.workspace || '').toLowerCase();
    }
  }

  async function switchDir(path) {
    const p = String(path || '');
    const inWs = wsState.workspace
      && p.toLowerCase().startsWith(wsState.workspace.toLowerCase());
    if (inWs) {
      await postJson('/workspace/cwd', { path: p });
    } else if (p && await uiConfirm(`把工作区切换到：\n${p} ？`, { title: '切换工作区' })) {
      await postJson('/workspace', { path: p });
    }
    await refreshWs();
    closePopovers('');
  }

  async function postJson(path, body) {
    try {
      const r = await window.planner.apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      return JSON.parse(r.text);
    } catch {
      return { ok: false };
    }
  }

  if ($('#cb-dir')) {
    $('#cb-dir').addEventListener('click', async () => {
      if (!wsState.workspace) {
        // 未启用代码工作区：直接弹目录选择器（不再绕设置页）
        const p = await window.planner.pickDirectory();
        if (p) {
          await postJson('/workspace', { path: p });
          await refreshWs();
        }
        return;
      }
      const panel = $('#ws-panel');
      const willShow = panel.classList.contains('hidden');
      closePopovers('#ws-panel');
      if (!willShow) {
        panel.classList.add('hidden');
        return;
      }
      wsBrowse = wsState.cwd || wsState.workspace;
      // 悬在整个输入框上方（#input-bar）：面板高、操作后会保持打开，
      // 若按 chip 定位会盖住输入区——点"输入框"其实点在面板上，第一下只关面板
      // 且不落光标（activeElement=BODY），表现为"点不进/打不了字"（历史 bug）。
      positionPopover('#ws-panel', $('#input-bar'));
      await renderWsPanel();
      positionPopover('#ws-panel', $('#input-bar'));   // 内容渲染后宽度可能变化 → 再对齐
    });
    $('#ws-close').addEventListener('click', () => closePopovers(''));
    $('#ws-use').addEventListener('click', async () => {
      await postJson('/workspace/cwd', { path: wsBrowse || '' });
      await refreshWs();
      closePopovers('');
    });
    $('#ws-root').addEventListener('click', async () => {
      await postJson('/workspace/cwd', { path: '' });
      await refreshWs();
      closePopovers('');
    });
    // 在系统文件管理器里打开当前浏览目录
    if ($('#ws-open')) {
      $('#ws-open').addEventListener('click', async () => {
        const p = wsBrowse || wsState.cwd || wsState.workspace;
        if (!p) return;
        const r = await window.planner.openInExplorer(p);
        if (r && !r.ok) addMessage(`（无法打开文件夹：${r.error || '未知错误'}）`, 'log');
      });
    }
    // 更换工作区：直接弹目录选择器切换（不再绕设置页）
    $('#ws-change').addEventListener('click', async () => {
      const p = await window.planner.pickDirectory();
      if (!p) return;                                  // 用户取消（面板保持打开）
      await postJson('/workspace', { path: p });
      await refreshWs();
      closePopovers('');                               // 切换完成即收起（不挡输入区）
    });
  }

  // 上下文分解弹层（合并面板上半）：单条堆叠，各分项占"固定 100 万总窗口"的比例
  const CTX_WINDOW_TOKENS = 1000000;   // 总上下文窗口固定 100 万（用户指定口径）

  function renderCtxPanel() {
    const box = $('#ctx-bars');
    if (!box) return;
    box.innerHTML = '';
    const ctx = lastCtx;
    if (!ctx || !ctx.breakdown) {
      box.innerHTML = '<div class="ws-item dim">暂无数据</div>';
      $('#ctx-foot').textContent = '';
      return;
    }
    const items = [
      ['system', '系统提示', '#2563eb'],
      ['nodes', '记忆节点', '#10b981'],
      ['raw', '原始消息', '#93c5fd'],
      ['images', '图片', '#f59e0b'],
    ];
    const stack = document.createElement('div');
    stack.className = 'ctx-stack';
    const legend = document.createElement('div');
    legend.className = 'ctx-legend';
    items.forEach(([key, label, color]) => {
      const v = ctx.breakdown[key] || 0;
      if (v > 0) {
        const seg = document.createElement('i');
        seg.style.width = (v / CTX_WINDOW_TOKENS * 100) + '%';
        seg.style.background = color;
        seg.title = `${label}：${v.toLocaleString()} token`;
        stack.appendChild(seg);
      }
      const pct = v / CTX_WINDOW_TOKENS * 100;
      const pctText = pct >= 0.1 ? pct.toFixed(1) + '%' : (v > 0 ? '<0.1%' : '0%');
      const row = document.createElement('span');
      row.className = 'ctx-legend-item';
      row.innerHTML = `<i style="background:${color}"></i>`
        + `${escapeHtml(label)} ${v.toLocaleString()} · ${escapeHtml(pctText)}`;
      legend.appendChild(row);
    });
    box.appendChild(stack);
    box.appendChild(legend);
    const total = Math.max(1, ctx.tokens || 1);
    const totalPct = (total / CTX_WINDOW_TOKENS * 100).toFixed(1);
    $('#ctx-foot').textContent =
      `共 ${total.toLocaleString()} token · 占 100 万窗口 ${totalPct}% · ${ctx.messages || 0} 条消息（含节点）`
      + (Number(ctx.real_tokens || 0) > 0
        ? `\nAPI 实报输入：${Number(ctx.real_tokens).toLocaleString()} token（上一轮，含系统提示/节点/图片）`
        : '\n（各分项为字符估算；生成一轮后这里显示 API 实报值）');
  }

  // 面板尺寸：舞台 CSS 变量始终跟随实际窗口（拖拽调整大小/主进程改尺寸都会触发 resize）
  function syncStageSize() {
    document.documentElement.style.setProperty('--stage-w', window.innerWidth + 'px');
    document.documentElement.style.setProperty('--stage-h', window.innerHeight + 'px');
  }
  syncStageSize();
  window.addEventListener('resize', syncStageSize);

  // ── 边缘拖拽调整大小（透明窗口不支持原生边框 resize → 渲染层报边缘，主进程跟光标调）──
  document.querySelectorAll('#resize-handles .rs').forEach((el) => {
    el.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      try { el.setPointerCapture(e.pointerId); } catch { /* 忽略 */ }
      window.planner.beginResize(el.dataset.edge || 'se');
    });
    const end = () => window.planner.endResize();
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  });
  window.addEventListener('blur', () => window.planner.endResize());

  // @ 文件引用补全（对齐 Codex / opencode 的 @ file）
  const atList = $('#at-list');
  let atItems = [];
  let atIndex = -1;
  let atReqSeq = 0;

  function atTokenBeforeCursor() {
    const v = input.value;
    const pos = input.selectionStart || 0;
    const m = v.slice(0, pos).match(/@([^\s@]*)$/);
    return m ? { start: pos - m[0].length, q: m[1] } : null;
  }

  function hideAtList() {
    if (atList) atList.classList.add('hidden');
    atItems = [];
    atIndex = -1;
  }

  function renderAtList() {
    if (!atList) return;
    atList.innerHTML = '';
    atItems.forEach((f, i) => {
      const btn = document.createElement('button');
      btn.className = 'at-item' + (i === atIndex ? ' active' : '');
      btn.type = 'button';
      btn.textContent = f;
      btn.addEventListener('mousedown', (e) => {   // mousedown：抢在 blur 之前
        e.preventDefault();
        applyAtItem(i);
      });
      atList.appendChild(btn);
    });
    atList.classList.remove('hidden');
  }

  async function updateAtList() {
    const tok = atTokenBeforeCursor();
    if (!tok || !wsState.workspace) {
      hideAtList();
      return;
    }
    const seq = ++atReqSeq;
    let files = [];
    try {
      const r = await window.planner.apiFetch('/workspace/files?q=' + encodeURIComponent(tok.q));
      const d = JSON.parse(r.text);
      files = (d.files || []).slice(0, 10);
    } catch { files = []; }
    if (seq !== atReqSeq) return;   // 过期响应（期间又敲了字）
    atItems = files;
    if (!atItems.length) { hideAtList(); return; }
    atIndex = 0;
    renderAtList();
  }

  function applyAtItem(i) {
    const f = atItems[i];
    const tok = atTokenBeforeCursor();
    if (!f || !tok) { hideAtList(); return; }
    const v = input.value;
    const after = v.slice(input.selectionStart || 0);
    const next = v.slice(0, tok.start) + '@' + f + ' ' + after;
    input.value = next;
    const caret = tok.start + f.length + 2;
    input.setSelectionRange(caret, caret);
    hideAtList();
    autoResizeInput();
    input.focus();
  }

  function handleAtKey(e) {
    if (!atList || atList.classList.contains('hidden') || !atItems.length) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      atIndex = (atIndex + (e.key === 'ArrowDown' ? 1 : atItems.length - 1)) % atItems.length;
      renderAtList();
      return true;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      applyAtItem(atIndex);
      return true;
    }
    if (e.key === 'Escape') {
      hideAtList();
      return true;
    }
    return false;
  }
  input.addEventListener('input', () => updateAtList());
  input.addEventListener('blur', () => hideAtList());

  // ── 变更历史 tab：文件变更留底 + 任意回退 ──────────────────
  function fmtChgTime(ts) {
    try {
      const dt = new Date((Number(ts) || 0) * 1000);
      const p2 = (n) => String(n).padStart(2, '0');
      return `${p2(dt.getMonth() + 1)}-${p2(dt.getDate())} `
        + `${p2(dt.getHours())}:${p2(dt.getMinutes())}:${p2(dt.getSeconds())}`;
    } catch { return ''; }
  }

  function renderChanges(items) {
    const list = $('#changes-list');
    if (!list) return;
    list.innerHTML = '';
    if (!items.length) {
      const div = document.createElement('div');
      div.className = 'chg-item dim';
      div.textContent = '还没有变更记录（小助写/改/删文件后会出现这里）。';
      list.appendChild(div);
      return;
    }
    items.forEach((c) => {
      const row = document.createElement('div');
      row.className = 'chg-item' + (c.reverted ? ' reverted' : '');
      const info = document.createElement('div');
      info.className = 'chg-info';
      const title = document.createElement('div');
      title.className = 'chg-title';
      title.textContent = `${c.tool} · ${c.rel}`;
      const meta = document.createElement('div');
      meta.className = 'chg-meta';
      const flags = [];
      if (c.reverted) flags.push('已回退');
      if (c.before_missing) flags.push('新建');
      if (c.after_missing) flags.push('删除');
      if (c.no_snapshot) flags.push('未留底');
      meta.textContent = `${fmtChgTime(c.ts)}  ·  内容 ${c.after_size}/原 ${c.before_size} 字符`
        + (flags.length ? '  ·  ' + flags.join(' · ') : '');
      info.appendChild(title);
      info.appendChild(meta);
      row.appendChild(info);
      if (!c.reverted && !c.no_snapshot) {
        const btn = document.createElement('button');
        btn.className = 'chg-undo';
        btn.type = 'button';
        btn.textContent = '回退到此条之前';
        btn.addEventListener('click', () => rollbackChanges(c));
        row.appendChild(btn);
      }
      list.appendChild(row);
    });
  }

  async function refreshChanges() {
    try {
      const r = await window.planner.apiFetch('/changes?limit=200');
      const d = JSON.parse(r.text);
      if (!d.ok) throw new Error(d.error || 'bad_response');
      renderChanges(d.changes || []);
    } catch (e) {
      const list = $('#changes-list');
      if (list) {
        list.innerHTML = '';
        const div = document.createElement('div');
        div.className = 'chg-item dim';
        div.textContent = '（变更历史加载失败，点「⟳ 刷新」重试）';
        list.appendChild(div);
      }
      console.warn('[changes] 加载失败', e);
    }
  }

  async function rollbackChanges(c) {
    const okGo = await uiConfirm(`回退到「${c.tool} · ${c.rel}」之前？\n`
      + '该条及之后的所有变更都会被撤销（恢复文件内容）。', { title: '回退变更' });
    if (!okGo) return;
    const res = await postJson('/changes/rollback', { id: c.id });
    if (!res.ok) {
      addError('回退失败。', '该条可能没有留底（超大/二进制文件），或路径已不在当前工作区。');
    } else {
      const failed = res.failed || [];
      addMessage(`（已回退 ${(res.reverted || []).length} 条变更`
        + (failed.length ? `；${failed.length} 条失败：${failed.map((f) => f.reason).join('；')}`
                         : '') + '）', 'log');
    }
    refreshChanges();
  }

  if ($('#changes-refresh')) {
    $('#changes-refresh').addEventListener('click', () => refreshChanges());
    $('#changes-clear').addEventListener('click', async () => {
      if (!(await uiConfirm('清空变更历史？（不会改动任何文件）', { title: '清空变更历史' }))) return;
      await postJson('/changes/clear', {});
      refreshChanges();
    });
  }

  function handleEvent(ev) {
    if (ev.type === 'user_message') {
      // 其他窗口（悬浮球语音 / 快速输入浮层）或直接 HTTP 发的用户消息：
      // 面板本地没有自己的 addMessage —— 用事件补气泡（按 msg id 去重）
      addUserMessage(ev.text || '', ev.id || '', false, ev.attachments || []);   // 不打断用户翻历史
    } else if (ev.type === 'text_stream') {
      let el = streamMsgs[ev.msg_id];
      if (!el) {
        collapseThinking();   // 这一轮开始说正文了 → 思考过程收起来
        el = addMessage('', 'assistant');
        streamMsgs[ev.msg_id] = el;
      }
      const bubble = el.querySelector('.bubble');
      if (bubble) bubble.textContent += ev.content;
      else el.textContent += ev.content;
      scrollMessagesToBottom();
    } else if (ev.type === 'thinking_stream') {
      thinkingStream(ev);
    } else if (ev.type === 'text') {
      // 流式收束（完整文本事件）：对最新一条 assistant 消息整段渲染 markdown
      collapseThinking();
      const last = [...messages.querySelectorAll('.msg.assistant')].pop();
      if (last) {
        const b = last.querySelector('.bubble');
        if (b && !b.querySelector('strong, code, pre')) {
          b.innerHTML = window.md.render(b.textContent);
          enhanceHtmlPreviews(b);   // ```html → 沙箱预览卡
        }
      }
    } else if (ev.type === 'tool_call') {
      addToolLine(ev.name, ev.args, ev.id, false);
    } else if (ev.type === 'tool_result') {
      setToolResult(ev.id, ev.content, ev.diff);
    } else if (ev.type === 'log') {
      if (ev.content) addMessage(ev.content, 'log');
    } else if (ev.type === 'thinking') {
      $('#thinking').classList.toggle('hidden', !ev.value);
      $('.dot').classList.toggle('thinking', !!ev.value);
      syncMainAgentStatus(!!ev.value);
    } else if (ev.type === 'compress_state') {
      // 后台压缩开始/结束（指示灯；完成后的历史重载走 memory_update）
      $('#compressing').classList.toggle('hidden', !ev.value);
      $('.dot').classList.toggle('compressing', !!ev.value);
    } else if (ev.type === 'agenda_update') {
      if ($('#calendar').classList.contains('active')) refreshCalendar();
    } else if (ev.type === 'memory_update') {
      pendingMemoryReload = true;   // 记忆树有变化，生成结束后重载历史
    } else if (ev.type === 'error') {
      addError(ev.text, ev.hint);
    } else if (ev.type === 'changes_update') {
      if ($('#changes').classList.contains('active')) refreshChanges();
    } else if (ev.type === 'stats_update') {
      if (ev.summary) llmState.stats = ev.summary;
      refreshStats();
    } else if (ev.type === 'approval_request') {
      showApproval(ev);
    } else if (ev.type === 'approval_resolved') {
      if (!ev.id || ev.id === approvalReqId) hideApproval();
    } else if (ev.type === 'question_request') {
      showQuestion(ev);
    } else if (ev.type === 'question_resolved') {
      const card = questionCard(ev.id);
      if (card) {
        const ans = ev.answer || ev.selected;
        settleQuestionCard(card, ans ? `（已回答：${ans}）` : '（已跳过）');
      } else if (!ev.id) {
        document.querySelectorAll('.question-inline:not(.resolved)').forEach((el) => {
          settleQuestionCard(el, '（已结束）');
        });
      }
    } else if (ev.type === 'image') {
      addImage(ev.url, ev.caption);
    } else if (ev.type === 'changed_files') {
      addChangedFiles(ev.files);
    } else if (ev.type === 'permissions_update') {
      if ($('#permissions').classList.contains('active')) refreshDirPermissions();
    } else if (ev.type === 'workspace_changed') {
      const desc = ev.workspace
        ? `（代码工作区：${ev.cwd || ev.workspace} · 权限模式 ${ev.permission_mode || 'ask'}）`
        : '（代码工作区已关闭）';
      addMessage(desc, 'log');
      refreshWs();
    }
  }

  // ── 事件与状态 ─────────────────────────────────────────
  // /dequeue 与 /state 都由主进程独占轮询后经 IPC 推送（events / state），
  // 面板不再各自轮询，减少 HTTP 请求。
  // 压缩发生（memory_update）时标记待重载：生成结束（thinking false）后
  // 自动重载历史，对话框与 buffer 同步（不打断流式）。
  let pendingMemoryReload = false;
  // 当前是否在生成（applyState 按 state.thinking 同步）：onPanelShown 重建历史前要看它，
  // 生成中绝不重建（在途思考/工具行还没进 buffer，重建=抹掉——用户实测 bug）。
  let isGenerating = false;

  // ── 子代理 White（后台异步；用户只和主助手对话）──────────
  const agentBar = $('#agent-bar');
  const whiteView = $('#white-view');
  const whiteMessages = $('#white-messages');
  const whiteTaskEl = $('#white-task');
  const whiteStateEl = $('#white-state');
  const whiteStopBtn = $('#white-stop');
  const whiteJumpBtn = $('#white-jump');
  state.agents = [
    { id: 'main', name: '小助', status: 'idle', pending_approvals: 0 },
    { id: 'white', name: 'White', status: 'idle', placeholder: true,
      task: '', started_at: 0, finished_at: 0, result: null, pending_approvals: 0 },
  ];
  state.activeAgent = 'main';
  state.whiteEnabled = true;
  state.whiteUnread = false;
  state.whiteTask = '';
  let whiteToolLines = {};
  let whiteStreamEl = null;
  let whiteThinkEl = null;
  let whiteHistoryToken = 0;
  // White 自己的贴底策略：只有用户当前在底部时才跟随新消息；翻上去看历史时不动。
  let whiteStick = true;

  function whiteIsActive() { return state.activeAgent === 'white'; }
  function whiteNearBottom() {
    return whiteMessages.scrollHeight - whiteMessages.scrollTop - whiteMessages.clientHeight < 100;
  }
  function updateWhiteJump() {
    if (whiteJumpBtn) whiteJumpBtn.classList.toggle('hidden', whiteNearBottom());
  }
  function whiteScroll(force = false) {
    if (force) whiteStick = true;
    if (whiteStick) {
      try { whiteMessages.scrollTop = whiteMessages.scrollHeight; } catch { /* 忽略 */ }
    }
    updateWhiteJump();
  }
  whiteMessages.addEventListener('scroll', () => {
    whiteStick = whiteNearBottom();
    updateWhiteJump();
  });
  if (whiteJumpBtn) {
    whiteJumpBtn.addEventListener('click', () => {
      whiteStick = true;
      whiteMessages.scrollTo({ top: whiteMessages.scrollHeight, behavior: 'smooth' });
    });
  }

  async function refreshAgents() {
    try {
      const d = await api('/agents');
      state.agents = d.agents || [];
      state.whiteEnabled = d.white_enabled !== false;
      // /agents 是请求瞬间的快照：如果响应晚到，主助手可能已经答完。
      // 本地 isGenerating 更新，覆盖服务端快照，避免主标签一直转圈。
      const main = state.agents.find((a) => a.id === 'main');
      if (main) main.status = isGenerating ? 'running' : 'idle';
      renderAgentBar();
    } catch { /* 后端不可用 */ }
  }

  function agentInfo(id) {
    return (state.agents || []).find((a) => a.id === id) || null;
  }

  // 主助手标签的运行状态以实时 thinking 为准；/agents 只在启动/展开时拉一次，
  // 不随后续生成结束更新（否则标签会一直停在转圈）。
  function syncMainAgentStatus(running) {
    const a = agentInfo('main');
    if (!a) return;
    const next = running ? 'running' : 'idle';
    if (a.status === next) return;
    a.status = next;
    renderAgentBar();
  }

  function renderAgentBar() {
    const list = state.agents || [];
    if (list.length <= 1 || !state.whiteEnabled) {
      agentBar.classList.add('hidden');
      return;
    }
    agentBar.classList.remove('hidden');
    agentBar.textContent = '';
    list.forEach((a) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agent-tab' + (a.id === 'white' ? ' white' : '')
        + (state.activeAgent === a.id ? ' active' : '');
      const name = document.createElement('span');
      name.textContent = a.id === 'main' ? '主助手' : (a.name || a.id);
      btn.appendChild(name);
      if (a.status === 'running') {
        const sp = document.createElement('span');
        sp.className = 'agent-spinner';
        btn.appendChild(sp);
      } else {
        const dot = document.createElement('span');
        // White 空闲/未派任务 → 灰色呼吸灯；主助手绿色
        dot.className = 'agent-dot' + (a.id === 'white' ? ' gray' : '');
        btn.appendChild(dot);
      }
      if ((a.pending_approvals || 0) > 0) {
        const b = document.createElement('span');
        b.className = 'agent-bang';
        b.textContent = a.pending_approvals > 9 ? '9+' : String(a.pending_approvals);
        btn.appendChild(b);
      }
      if (a.id === 'white' && state.whiteUnread) {
        const u = document.createElement('span');
        u.className = 'agent-unread';
        btn.appendChild(u);
      }
      btn.addEventListener('click', () => switchAgent(a.id));
      agentBar.appendChild(btn);
    });
  }

  function switchAgent(id) {
    if (id === 'white' && !agentInfo('white')) return;
    state.activeAgent = id;
    const white = id === 'white';
    $('#messages').classList.toggle('hidden', white);
    $('#jump-bottom').classList.add('hidden');
    if (white) {
      $('#mounted-bar').classList.add('hidden');
    } else {
      // 回到主视图：挂载条按实际文件恢复（不要无条件显示空条）
      window.planner.getMounted().then(renderMountedBar).catch(() => {});
    }
    $('#input-bar').classList.toggle('hidden', white);
    whiteView.classList.toggle('hidden', !white);
    if (white) {
      state.whiteUnread = false;
      // DOM 已有实时内容就保留（含滚动位置）；只有首次进入/关闭后空态才拉 history
      if (!whiteMessages.childElementCount) loadWhiteHistory();
      // 隐藏时 clientHeight=0，jump 状态不可靠；显示后按真实滚动位置校正
      setTimeout(updateWhiteJump, 0);
    }
    renderAgentBar();
  }

  // White 视图的渲染（与主对话同款气泡/工具行/思考行）
  function whiteAddUser(text) {
    const el = document.createElement('div');
    el.className = 'msg user';
    const body = document.createElement('div');
    body.className = 'msg-body';
    const bubble = document.createElement('div');
    bubble.className = 'bubble user';
    bubble.textContent = String(text || '');
    body.appendChild(bubble);
    el.appendChild(body);
    whiteMessages.appendChild(el);
    whiteScroll();
  }

  function whiteAddAssistant(text) {
    const el = document.createElement('div');
    el.className = 'msg assistant';
    const body = document.createElement('div');
    body.className = 'msg-body';
    const bubble = document.createElement('div');
    bubble.className = 'bubble assistant';
    bubble.innerHTML = window.md.render(String(text || ''));
    body.appendChild(bubble);
    el.appendChild(body);
    whiteMessages.appendChild(el);
    whiteScroll();
    return bubble;
  }

  function whiteStream(text) {
    if (!whiteStreamEl) whiteStreamEl = whiteAddAssistant('');
    whiteStreamEl.textContent += String(text || '');
    whiteScroll();
  }

  function whiteFinishStream(full) {
    if (whiteStreamEl) {
      whiteStreamEl.innerHTML = window.md.render(String(full || whiteStreamEl.textContent));
      whiteStreamEl = null;
    } else if (full) {
      whiteAddAssistant(full);
    }
    whiteScroll();
  }

  function whiteThinking(text) {
    if (!whiteThinkEl) {
      const d = document.createElement('details');
      d.className = 'white-thinking';
      const s = document.createElement('summary');
      s.textContent = '思考过程';
      const pre = document.createElement('pre');
      d.appendChild(s);
      d.appendChild(pre);
      whiteMessages.appendChild(d);
      whiteThinkEl = pre;
    }
    whiteThinkEl.textContent += String(text || '');
    whiteScroll();
  }

  function whiteToolCall(id, name, args, done) {
    if (whiteToolLines[id]) return;
    const el = document.createElement('div');
    el.className = 'tool-line' + (done ? ' done' : ' running');
    el.dataset.tool = name || '?';
    const argsText = args ? JSON.stringify(args, null, 1) : '';
    el.innerHTML = '<div class="tl-line" title="点击展开参数与结果">'
      + '<span class="tl-name">' + escapeHtml(name || '?') + '</span>'
      + '<span class="tl-target"></span>'
      + '<span class="tl-status">' + (done ? '✓' : '<span class="tl-spinner"></span>执行中…') + '</span>'
      + '<span class="tl-preview"></span></div>'
      + '<div class="tl-body hidden"></div>';
    el.querySelector('.tl-line').addEventListener('click', () => {
      el.querySelector('.tl-body').classList.toggle('hidden');
    });
    if (argsText) {
      const pre = document.createElement('pre');
      pre.className = 'tool-pre';
      pre.textContent = argsText;
      el.querySelector('.tl-body').appendChild(pre);
    }
    whiteMessages.appendChild(el);
    whiteToolLines[id] = el;
    whiteScroll();
  }

  function whiteToolResult(id, content, diff) {
    let el = whiteToolLines[id];
    if (!el) { whiteToolCall(id, 'tool', {}, false); el = whiteToolLines[id]; }
    if (!el) return;
    el.classList.remove('running');
    el.classList.add('done');
    const status = el.querySelector('.tl-status');
    if (status) status.textContent = '✓';
    const body = el.querySelector('.tl-body');
    if (body && content) {
      const pre = document.createElement('pre');
      pre.className = 'tool-result';
      pre.textContent = String(content).slice(0, 4000);
      body.appendChild(pre);
    }
    if (body && diff) {
      const pre = document.createElement('pre');
      pre.className = 'tool-pre';
      pre.textContent = String(diff).slice(0, 6000);
      body.appendChild(pre);
    }
    const preview = el.querySelector('.tl-preview');
    if (preview) preview.textContent = String(content || '').slice(0, 80).replace(/\s+/g, ' ');
    whiteScroll();
  }

  function whiteResultCard(result) {
    if (!result) return;
    const el = document.createElement('div');
    el.className = 'white-result';
    const stateText = { done: '完成', blocked: '被阻塞', failed: '失败' }[result.status] || result.status;
    el.innerHTML = '<div class="wr-head">White 任务结果'
      + '<span class="wr-state ' + escapeHtml(result.status || '') + '">' + escapeHtml(stateText || '') + '</span>'
      + '<span class="hint" style="font-weight:400">' + (result.elapsed_seconds || 0) + 's</span></div>'
      + '<div class="wr-summary"></div>';
    el.querySelector('.wr-summary').textContent = String(result.summary || '');
    const extra = [];
    if ((result.findings || []).length) extra.push('发现：' + result.findings.join('；'));
    if ((result.assumptions || []).length) extra.push('假设：' + result.assumptions.join('；'));
    if ((result.next_steps || []).length) extra.push('建议：' + result.next_steps.join('；'));
    if (extra.length) {
      const div = document.createElement('div');
      div.className = 'wr-files';
      div.textContent = extra.join('\n');
      el.appendChild(div);
    }
    const files = result.files || [];
    if (files.length) {
      const div = document.createElement('div');
      div.className = 'wr-files';
      div.textContent = '改动文件：';
      files.forEach((f) => {
        const code = document.createElement('code');
        code.textContent = f.rel || f.path || '';
        code.title = f.path || '';
        div.appendChild(code);
      });
      el.appendChild(div);
    }
    whiteMessages.appendChild(el);
    whiteScroll();
  }

  function whiteNote(text) {
    const el = document.createElement('div');
    el.className = 'msg log';
    el.textContent = '（White：' + text + '）';
    whiteMessages.appendChild(el);
    whiteScroll();
  }

  function updateWhiteHead(st) {
    if (!st) return;
    whiteTaskEl.textContent = st.task || '还没有任务——由小助派发独立任务后会显示执行过程';
    const labels = { idle: '空闲', running: '运行中…', closed: '已关闭' };
    whiteStateEl.textContent = labels[st.status] || st.status || '';
    whiteStopBtn.classList.toggle('hidden', st.status !== 'running');
  }

  function applyWhiteStatus(ev) {
    let a = agentInfo('white');
    if (!a) {
      a = { id: 'white', name: 'White' };
      state.agents = [...(state.agents || []), a];
    }
    Object.assign(a, ev);
    if (ev.status === 'closed') {
      // 关闭后标签页保留（灰色呼吸灯），只是回到还没有任务
      const placeholder = { id: 'white', name: 'White', status: 'idle',
                            placeholder: true, task: '', started_at: 0,
                            finished_at: 0, result: null, pending_approvals: 0 };
      state.agents = (state.agents || []).map((x) => (x.id === 'white' ? placeholder : x));
      if (!state.agents.some((x) => x.id === 'white')) state.agents.push(placeholder);
      whiteMessages.textContent = '';
      whiteToolLines = {};
      whiteStreamEl = null;
      whiteThinkEl = null;
      whiteStick = true;
      if (whiteJumpBtn) whiteJumpBtn.classList.add('hidden');
      state.whiteTask = '';
      updateWhiteHead(placeholder);
      if (whiteIsActive()) loadWhiteHistory();
      renderAgentBar();
      return;
    }
    if (ev.status === 'running' && ev.task && ev.task !== state.whiteTask) {
      state.whiteTask = ev.task;
      // 新任务：把主助手交给 White 的说明也显示成一条消息
      if (!whiteMessages.textContent.includes(ev.task)) whiteAddUser(ev.task);
    }
    updateWhiteHead(ev);
    renderAgentBar();
    if (ev.status === 'running') state.whiteUnread = false;
  }

  function handleWhiteEvent(ev) {
    if (ev.type === 'white_status') { applyWhiteStatus(ev); return; }
    if (ev.type === 'white_result') {
      whiteResultCard(ev.result);
      if (!whiteIsActive()) { state.whiteUnread = true; renderAgentBar(); }
      return;
    }
    if (ev.type === 'white_note') {
      whiteNote(ev.text);
      return;
    }
    // 实时事件始终写进 White（隐藏时也累积）——用户中途点开就能看到完整流程，
    // 不依赖 /history 晚到的响应。
    whiteHistoryToken += 1;
    if (ev.type === 'text_stream') whiteStream(ev.content);
    else if (ev.type === 'text') whiteFinishStream(ev.content);
    else if (ev.type === 'thinking_stream') whiteThinking(ev.content);
    else if (ev.type === 'tool_call') whiteToolCall(ev.id, ev.name, ev.args, false);
    else if (ev.type === 'tool_result') whiteToolResult(ev.id, ev.content, ev.diff);
  }

  async function loadWhiteHistory() {
    const token = ++whiteHistoryToken;
    const wasNear = whiteNearBottom();
    const fromBottom = whiteMessages.scrollHeight - whiteMessages.scrollTop;
    const hadContent = whiteMessages.childElementCount > 0;
    try {
      const d = await api('/history?agent=white');
      if (token !== whiteHistoryToken) return;   // 期间来了实时事件，别用旧历史覆盖
      whiteMessages.textContent = '';
      whiteToolLines = {};
      whiteStreamEl = null;
      whiteThinkEl = null;
      state.whiteTask = (d.status && d.status.task) || '';
      (d.messages || []).forEach((m) => {
        if (m.role === 'user') whiteAddUser(m.content);
        else if (m.role === 'assistant') whiteAddAssistant(m.content);
        else if (m.role === 'thinking') whiteThinking(m.content);
        else if (m.role === 'tool_call') whiteToolCall(m.id, m.name, m.args, false);
        else if (m.role === 'tool_result') whiteToolResult(m.id, m.content, m.diff);
        else if (m.role === 'result') whiteResultCard(m.result);
      });
      if (d.status) updateWhiteHead(d.status);
      if (!(d.messages || []).length) {
        const hint = document.createElement('div');
        hint.className = 'msg log';
        hint.textContent = 'White 还没有任务——由小助派发独立任务后会显示执行过程。';
        whiteMessages.appendChild(hint);
      }
      // 清空/重渲染会改变 scrollHeight：按加载前的阅读位置恢复，不能无条件拉底。
      if (!hadContent || wasNear) {
        whiteStick = true;
        whiteMessages.scrollTop = whiteMessages.scrollHeight;
      } else {
        whiteMessages.scrollTop = Math.max(0, whiteMessages.scrollHeight - fromBottom);
        whiteStick = whiteNearBottom();
      }
      updateWhiteJump();
      whiteScroll(whiteStick);
    } catch (e) { console.error("[white] load history failed", e); }
  }

  whiteStopBtn.addEventListener('click', async () => {
    whiteStopBtn.disabled = true;
    try {
      await post('/white/stop', {});
      whiteStateEl.textContent = '停止中…';
    } catch { /* 忽略 */ }
    setTimeout(() => { whiteStopBtn.disabled = false; }, 1500);
  });

  refreshAgents();

  window.planner.onEvents((events) => {
    events.forEach((ev) => {
      // 审批弹窗仍是全局的（主助手/White 共用一张卡），其余 White 事件走子代理视图
      const globalApproval = ev.type === 'approval_request' || ev.type === 'approval_resolved';
      if (ev.agent === 'white' && !globalApproval) {
        handleWhiteEvent(ev);
        return;
      }
      handleEvent(ev);
      if (globalApproval) refreshAgents();
    });
  });

  window.planner.onState(applyState);

  function applyState(s) {
    if (!s) return;
    if (s.offline) {
      setOffline(true);
      return;
    }
    setOffline(false);
    state.heartbeat = s.heartbeat;
    // 思考状态：以推送的 state 为准兜底纠正（thinking 事件可能在面板隐藏时被悬浮球消费）
    const thinking = !!s.thinking;
    isGenerating = thinking;
    syncMainAgentStatus(thinking);
    $('#thinking').classList.toggle('hidden', !thinking);
    $('.dot').classList.toggle('thinking', thinking);
    // 后台压缩指示（标题栏呼吸灯）：压缩记忆是分钟级任务，给个可见反馈
    const compressing = !!s.compressing;
    $('#compressing').classList.toggle('hidden', !compressing);
    $('.dot').classList.toggle('compressing', compressing);
    // 发送/停止合并按钮：生成中变"停止"（点击打断），否则"发送"
    sendBtn.classList.toggle('stopping', thinking);
    sendBtn.textContent = thinking ? '停止' : '发送';
    // 上下文统计：存下来供状态条 chip（"原始消息"分项）与合并面板使用
    lastCtx = s.context || null;
    renderStatsChip();   // chip 含原始消息长度 → 上下文变化也要刷新
    const hb = s.heartbeat;
    if (hb && hb.in_seconds > 0) {
      const label = hb.in_seconds < 60
        ? `${hb.in_seconds} 秒后醒来`
        : `${hb.in_minutes} 分钟后醒来`;
      $('#heartbeat').textContent = hb.note ? `${label}：${hb.note}` : label;
    } else {
      $('#heartbeat').textContent = '';
    }
    // 今日计划徽标（逾期数）
    const overdue = (s.agenda && s.agenda.overdue_count) || 0;
    const badge = $('#agenda-badge');
    badge.classList.toggle('hidden', !overdue);
    badge.textContent = overdue;
    // 跨天 → 日历翻到今天（日期变了，月份/选中日都可能要更新）
    const agenda = s.agenda;
    if (agenda && agenda.today && agenda.today !== state.lastAgendaDate) {
      state.lastAgendaDate = agenda.today;
      if ($('#calendar').classList.contains('active')) refreshCalendar();
    }
    // 生成结束且压缩过 → 重载历史（对话框与 buffer 同步，新节点出现）
    if (!s.thinking && pendingMemoryReload) {
      pendingMemoryReload = false;
      loadHistory(true);
    }
    // 生成结束 → 收起的思考块保持折叠（流式中没收完的在这里兜底）
    if (!thinking) collapseThinking();
  }

  // 后端离线/恢复：离线期间心跳位显示提示；**恢复瞬间补一次历史与工作区状态**——
  // 启动时面板先于后端就绪（loadHistory 静默失败），没有这条就得手动收/展面板才能看到历史。
  let backendWasOffline = false;
  function setOffline(off) {
    if (off) {
      $('#heartbeat').textContent = '后端未连接';
      backendWasOffline = true;
      return;
    }
    if (backendWasOffline) {
      backendWasOffline = false;
      loadHistory(true);
      refreshWs();
      refreshLlm();
      refreshStats();   // 启动竞态：面板先于后端加载时 /llm/stats 首次拉取失败——
                        // 不补这一下，合并 chip 的命中率/TPS 会一直显示「—」，
                        // 直到点开面板或下一次生成（用户反馈过）
    }
  }

  // ── 目录读取授权页 ─────────────────────────────────────
  function dirPermSourceLabel(source) {
    return ({ user: '手动添加', assistant: '助手申请', approval: '审批时记住' })[source]
      || source || '未知';
  }

  function renderDirPermissions(data) {
    const list = $('#perm-page-list');
    const note = $('#perm-page-note');
    if (!list || !note) return;
    const ws = (data && data.workspace) || '';
    note.textContent = ws
      ? `当前工作区：${ws}（写权限仅限这里）`
      : '尚未设置代码工作区；写权限不可用。';
    list.innerHTML = '';
    const grants = (data && data.grants) || [];
    if (!grants.length) {
      const hint = document.createElement('div');
      hint.className = 'empty-tip';
      hint.textContent = '还没有目录读授权——点「＋ 添加目录…」选择要允许小助读取的目录。';
      list.appendChild(hint);
      return;
    }
    grants.forEach((g) => {
      const row = document.createElement('div');
      row.className = 'perm-item';
      const main = document.createElement('div');
      main.className = 'perm-item-main';
      const path = document.createElement('div');
      path.className = 'perm-item-path';
      path.textContent = g.path || '';
      const meta = document.createElement('div');
      meta.className = 'perm-item-meta';
      let text = `只读 · 递归包含子目录 · 来源：${dirPermSourceLabel(g.source)}`;
      if (Number(g.covered_count) > 0) {
        text += ` · 已合并 ${g.covered_count} 个子目录授权`;
      }
      meta.textContent = text;
      main.appendChild(path);
      main.appendChild(meta);
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'perm-item-del';
      del.textContent = '移除';
      del.title = '移除这条目录读授权（其子目录的显式授权会重新出现）';
      del.addEventListener('click', async () => {
        if (!(await uiConfirm(`移除只读授权？\n${g.path || ''}`, { title: '移除授权' }))) return;
        try {
          const r = await post('/dir-permissions/remove', { id: g.id });
          if (!r.ok) addMessage(`（移除失败：${r.error || '未知错误'}）`, 'log');
        } catch {
          addMessage('（移除失败：后端不可用）', 'log');
        }
        await refreshDirPermissions();
      });
      row.appendChild(main);
      row.appendChild(del);
      list.appendChild(row);
    });
  }

  async function refreshDirPermissions() {
    const list = $('#perm-page-list');
    try {
      const d = await api('/dir-permissions');
      renderDirPermissions(d);
    } catch {
      if (list) {
        list.innerHTML = '<div class="empty-tip">后端未连接</div>';
      }
    }
  }

  if ($('#perm-page-add')) {
    $('#perm-page-add').addEventListener('click', async () => {
      try {
        const p = await window.planner.pickDirectory();
        if (!p) return;
        const r = await post('/dir-permissions/add', { path: p });
        if (!r.ok) {
          addMessage(`（添加授权失败：${r.message || r.error || '未知错误'}）`, 'log');
        } else if (r.merged) {
          addMessage(`（该目录已被现有授权覆盖：${p}）`, 'log');
        } else {
          addMessage(`（已添加只读授权：${p}）`, 'log');
        }
      } catch {
        addMessage('（添加授权失败：后端不可用）', 'log');
      }
      await refreshDirPermissions();
    });
  }
  if ($('#perm-page-refresh')) {
    $('#perm-page-refresh').addEventListener('click', refreshDirPermissions);
  }

  // ── Tab 切换 ─────────────────────────────────────────
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
      btn.classList.add('active');
      $('#' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'calendar') refreshCalendar();
      if (btn.dataset.tab === 'changes') refreshChanges();
      if (btn.dataset.tab === 'permissions') refreshDirPermissions();
    });
  });

  function localDateStr(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  // ── 日历（月视图 + 当天明细）──────────────────────────────
  // 数据来自 /calendar（calendar_items：date + 可选 time）。
  // 带时刻的项由后端到点提醒；这里只负责呈现、增删与改期。
  const calState = { year: 0, month: 0, selected: '' };

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  async function refreshCalendar() {
    const now = new Date();
    if (!calState.year) {
      calState.year = now.getFullYear();
      calState.month = now.getMonth();
      calState.selected = localDateStr(now);
    }
    renderMonth();
    await renderDay();
  }

  function renderMonth() {
    $('#cal-title').textContent = `${calState.year} 年 ${calState.month + 1} 月`;
    const from = `${calState.year}-${pad2(calState.month + 1)}-01`;
    const last = new Date(calState.year, calState.month + 1, 0);
    const to = localDateStr(last);
    const grid = $('#cal-grid');
    grid.innerHTML = '<div class="empty-tip">加载中…</div>';
    api(`/calendar?from=${from}&to=${to}`)
      .then((data) => {
        const items = (data && data.items) || [];
        const byDate = {};
        items.forEach((it) => {
          if (it.status === 'cancelled') return;   // 取消项不占格子
          (byDate[it.date] = byDate[it.date] || []).push(it);
        });
        const daysInMonth = last.getDate();
        const lead = new Date(calState.year, calState.month, 1).getDay();
        const today = localDateStr(new Date());
        grid.innerHTML = '';
        for (let i = 0; i < lead; i++) {
          const blank = document.createElement('div');
          blank.className = 'cal-cell cal-blank';
          grid.appendChild(blank);
        }
        for (let d = 1; d <= daysInMonth; d++) {
          const ds = `${calState.year}-${pad2(calState.month + 1)}-${pad2(d)}`;
          const list = byDate[ds] || [];
          const undone = list.filter((x) => x.status === 'todo').length;
          grid.appendChild(dayCell(ds, d, undone, ds === today));
        }
      })
      .catch(() => {
        grid.innerHTML = '<div class="empty-tip">后端未连接</div>';
      });
  }

  function dayCell(dateStr, dayNum, undone, isToday) {
    const cell = document.createElement('div');
    cell.className = 'cal-cell';
    if (isToday) cell.classList.add('is-today');
    if (dateStr === calState.selected) cell.classList.add('is-selected');
    if (undone > 0) cell.classList.add('has-items');
    cell.innerHTML = `
      <span class="cal-dnum">${dayNum}</span>
      ${undone > 0 ? `<span class="cal-dot">${undone}</span>` : ''}`;
    cell.addEventListener('click', () => {
      calState.selected = dateStr;
      renderMonth();
      renderDay();
    });
    return cell;
  }

  async function renderDay() {
    const ds = calState.selected;
    const d = new Date(`${ds}T00:00:00`);
    const week = '日一二三四五六'[d.getDay()];
    $('#cal-day-title').textContent = `${d.getMonth() + 1} 月 ${d.getDate()} 日 · 周${week}`;
    const list = $('#cal-day-list');
    list.innerHTML = '<div class="empty-tip">加载中…</div>';
    try {
      const data = await api(`/calendar?from=${ds}&to=${ds}`);
      const items = (data && data.items) || [];
      if (!items.length) {
        list.innerHTML = '<div class="empty-tip">这天还没有安排</div>';
        return;
      }
      list.innerHTML = '';
      items.forEach((it) => {
        const row = document.createElement('div');
        row.className = `cal-item${it.status === 'done' ? ' done' : ''}`;
        const kind = it.kind === 'task' ? '任务' : '日程';
        const repLabel = it.series_id
          ? ({ daily: '每天', weekly: '每周', weekdays: '工作日' }[it.repeat] || '重复')
          : '';
        row.innerHTML = `
          <input type="checkbox" ${it.status === 'done' ? 'checked' : ''} title="做完了勾选" />
          <span class="cal-time">${escapeHtml(it.time || '全天')}</span>
          <span class="cal-kind">${kind}</span>
          <span class="cal-text">${escapeHtml(it.content)}</span>
          ${repLabel ? `<button type="button" class="cal-rep" title="重复项（${repLabel}）">↻${repLabel}</button>` : ''}
          <button type="button" class="cal-del" title="取消这一次">✕</button>`;
        row.querySelector('input').addEventListener('change', () => {
          post('/calendar/update', {
            id: it.id,
            status: it.status === 'done' ? 'todo' : 'done',
          }).then(() => { renderMonth(); renderDay(); }).catch(() => {});
        });
        row.querySelector('.cal-del').addEventListener('click', () => {
          post('/calendar/cancel', { id: it.id })
            .then(() => { renderMonth(); renderDay(); }).catch(() => {});
        });
        // 重复项：点 ↻ 展开一个内联确认条（不用 window.confirm，避免弹窗被拦）
        const repBtn = row.querySelector('.cal-rep');
        if (repBtn) {
          repBtn.addEventListener('click', () => {
            if (row.nextElementSibling && row.nextElementSibling.classList.contains('cal-confirm')) {
              row.nextElementSibling.remove();
              return;
            }
            const bar = document.createElement('div');
            bar.className = 'cal-confirm';
            bar.innerHTML = `<span>取消该系列自 ${ds} 起、今后所有未完成的期？</span>
              <button type="button" class="ok">确定</button>
              <button type="button" class="no">算了</button>`;
            bar.querySelector('.ok').addEventListener('click', () => {
              post('/calendar/series/cancel', { id: it.id })
                .then(() => { renderMonth(); renderDay(); }).catch(() => {});
            });
            bar.querySelector('.no').addEventListener('click', () => bar.remove());
            row.after(bar);
          });
        }
        list.appendChild(row);
      });
    } catch {
      list.innerHTML = '<div class="empty-tip">后端未连接</div>';
    }
  }

  // 翻月 / 回今天
  $('#cal-prev').addEventListener('click', () => {
    calState.month -= 1;
    if (calState.month < 0) { calState.month = 11; calState.year -= 1; }
    renderMonth();
  });
  $('#cal-next').addEventListener('click', () => {
    calState.month += 1;
    if (calState.month > 11) { calState.month = 0; calState.year += 1; }
    renderMonth();
  });
  $('#cal-today').addEventListener('click', () => {
    const now = new Date();
    calState.year = now.getFullYear();
    calState.month = now.getMonth();
    calState.selected = localDateStr(now);
    refreshCalendar();
  });

  // 添加日程的入口（表单）已移除：日程/任务由小助通过日历工具管理
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── 收起（回到悬浮球）──────────────────────────────────────
  $('#btn-fold').addEventListener('click', () => {
    window.planner.hidePanel(); // 走 IPC 变形收回（不依赖 window.close 链路）
  });
  // 设置（右上角 ⚙）
  $('#btn-settings').addEventListener('click', () => {
    window.planner.openSettings();
  });

  // ── 历史消息（重启恢复 / 面板重新可见时补渲染）────────────
  // 面板隐藏期间小助说的话只冒了气泡，展开面板时要补进对话框；
  // 历史工具调用渲染为"已完成"卡片（静态，不转圈）。
  async function loadHistory(clear = true) {
    try {
      // 重建前记下用户的阅读位置（距底距离）：正在翻历史时重建后大致回到原地，
      // 而不是被拽到底部（压缩后台重载/面板重显都可能触发重建）
      const wasNear = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
      const fromBottom = messages.scrollHeight - messages.scrollTop;
      const data = await api('/history');
      if (clear) {
        messages.innerHTML = '';
        closeToolGroup();
        Object.keys(toolLines).forEach((k) => delete toolLines[k]);
        Object.keys(streamMsgs).forEach((k) => delete streamMsgs[k]);
        Object.keys(thinkBlocks).forEach((k) => delete thinkBlocks[k]);
      }
      (data.messages || []).forEach((m) => {
        if (m.role === 'tool_call') {
          addToolLine(m.name, m.args, m.id, true);
        } else if (m.role === 'tool_result') {
          setToolResult(m.id, m.content, m.diff);
        } else if (m.role === 'thinking') {
          appendThinking(createThinkingBlock(), m.content || '');
        } else if (m.role === 'image') {
          addImage(m.url, m.caption);
        } else if (m.role === 'changed_files') {
          addChangedFiles(m.files);
        } else {
          const cls = m.role === 'assistant' ? 'assistant' : m.role === 'memory' ? 'memory' : 'user';
          if (cls === 'user') {
            addUserMessage(m.content, m.id || null, false, m.attachments || []);
          } else {
            addMessage(m.content, cls, m.id || null);
          }
        }
      });
      // 渲染过程会把列表顺带滚到底（贴底跟随）；按用户原来的位置收尾：
      // - 原来贴底 → 强制到底（首次加载/正常重载）
      // - 原来在翻历史 → 恢复距底距离，不打断阅读
      if (clear) {
        if (!wasNear && fromBottom > 0) {
          messages.scrollTop = Math.max(0, messages.scrollHeight - fromBottom);
          jumpBtn.classList.remove('hidden');
        } else {
          scrollMessagesToBottom(true);
          jumpBtn.classList.add('hidden');
        }
      }
    } catch { /* 后端未连接则跳过 */ }
  }

  // 主进程通知：面板变形展开完成 → 空闲时才重建历史（含隐藏期间的气泡消息）
  window.planner.onPanelShown(() => {
    // ⚠️ 生成中**不得**重建：在途的思考流/工具调用/流式正文还没写进 buffer，
    // /history 里没有，重建会把它们从对话区抹掉（用户实测：生成中收/展面板，
    // "用了哪些工具、中间的 thinking"全没了）。隐藏期间事件现在也实时进 DOM
    // （main.js 投递），展开后 DOM 本身就是完整现场；空闲时再以 /history 全量对齐。
    if (!isGenerating) loadHistory(true);
  refreshAgents();
  // 首屏先渲染占位：不等 /agents 回来，打开面板就能看到「主助手 / White」
  renderAgentBar();
    if (whiteIsActive()) loadWhiteHistory();
    // 主动同步挂载文件（悬浮球形态挂载时本窗口未创建，收不到广播）
    window.planner.getMounted().then(renderMountedBar).catch(() => {});
    // 展开完成：聚焦输入框，可直接打字
    // 展开完成：先按空值/草稿校准高度，再聚焦输入框
    setTimeout(() => {
      try { autoResizeInput(); $('#input').focus(); } catch { /* 忽略 */ }
    }, 80);
  });

  // ── 语音播报 ─────────────────────────────────────────
  // file:// 页面无法直连 http 媒体/接口（CORS/PNA），音频经主进程下载为
  // 本地 file:// 后播放（audio 元素加载 file:// 无限制），新播放打断旧播放。
  const ttsAudio = document.getElementById('tts-audio');
  async function playAudioUrl(url) {
    if (!url) return false;
    try {
      const t0 = Date.now();
      const fileUrl = await window.planner.audioFile(url);
      if (!fileUrl) {
        window.planner.reportLog('tts: audioFile null for ' + url);
        window.planner.reportPlaybackResult(false, 'panel: audioFile null');
        return false;
      }
      clearSpeakPlaying();   // 新播放顶替旧的：把仍显示"播放中"的喇叭按钮复位
      ttsAudio.src = fileUrl;
      ttsAudio.onended = () => ttsAudio.removeAttribute('src');
      await ttsAudio.play();
      window.planner.reportLog('tts: play OK ' + fileUrl + ' (' + (Date.now() - t0) + 'ms)');
      window.planner.reportPlaybackResult(true, 'panel');
      return true;
    } catch (e) {
      window.planner.reportLog('tts: play FAIL ' + e.name + ' ' + e.message + ' src=' + ttsAudio.src);
      window.planner.reportPlaybackResult(false, 'panel: ' + e.name + ' ' + e.message);
      console.error('[tts] 播放失败:', e);
      return false;
    }
  }

  // 自动朗读（audio 事件）——语音连续对话模式下抑制（流式 TTS 接管）
  window.planner.onAudio((url) => {
    if (window.voiceMode && window.voiceMode.shouldSuppressAudio()) return;
    playAudioUrl(url);
  });

  // ── 面板语音输入：🎤 点击开始录音，再点一下结束 → 识别 → 填进输入框 ──
  //（免按连续对话模式仍在悬浮球右键菜单里开关；两者抢麦克风，开始手动录音会先关掉它）
  const voiceBtn = $('#voice-btn');
  const VOICE_BTN_TITLE = '点击开始录音，再点一下结束并转成文字';
  let voiceRec = 'idle';                 // idle | starting | recording
  let voiceRecStop = null;
  async function togglePanelVoice() {
    if (voiceRec === 'starting') return;
    if (voiceRec === 'recording') {
      voiceRec = 'idle';
      window.planner.setPanelRecording(false);
      voiceBtn.classList.remove('recording');
      if (!(window.voiceMode && window.voiceMode.isEnabled())) voiceBtn.title = VOICE_BTN_TITLE;
      const stop = voiceRecStop;
      voiceRecStop = null;
      let wav = null;
      try { wav = await stop(false); } catch { wav = null; }
      if (!wav) { addMessage('（没有录到声音）', 'log'); return; }
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
          addMessage('语音识别失败（模型下载中？）', 'log');
        }
      } catch {
        addMessage('语音识别失败（后端不可用）', 'log');
      }
      return;
    }
    if (window.voiceMode && window.voiceMode.isEnabled()) {
      window.voiceMode.requestToggle();      // 先关掉连续对话（避免两路抢麦）
    }
    if (window.voiceMode) window.voiceMode.interrupt();   // 打断正在播放的语音
    voiceRec = 'starting';
    voiceBtn.classList.add('recording');
    voiceBtn.title = '正在录音——再点一下结束并转成文字';
    try {
      voiceRecStop = await window.mic.begin();
      voiceRec = 'recording';
      window.planner.setPanelRecording(true);   // 悬浮球同步显示录音特效
    } catch {
      voiceRec = 'idle';
      window.planner.setPanelRecording(false);
      voiceRecStop = null;
      voiceBtn.classList.remove('recording');
      voiceBtn.title = VOICE_BTN_TITLE;
      addMessage('（无法使用麦克风：检查系统权限/设备）', 'log');
    }
  }
  voiceBtn.addEventListener('click', () => { togglePanelVoice(); });
  // active 判定：面板形态（本窗口 = 面板）时负责语音收发；
  // 面板窗口在 morphing_out（收起动画中）仍视为面板形态
  let voicePanelShown = true;
  window.planner.onPanelState((s) => {
    const shown = s === 'shown' || s === 'morphing_in' || s === 'morphing_out';
    if (shown !== voicePanelShown) {
      voicePanelShown = shown;
      if (window.voiceMode) window.voiceMode.notifyVisibility();
    }
  });
  window.voiceMode.attach({
    audioEl: ttsAudio,
    isActive: () => voicePanelShown,
    onUi: {
      setState: (state, enabled) => {
        voiceBtn.classList.toggle('active', enabled);
        voiceBtn.title = enabled
          ? '语音对话中（' + (state === 'recording' ? '正在听你说话…' : '待命中，直接说话')
            + '）——点击关闭'
          : '语音对话模式（免按连续对话，说话可打断小助）';
      },
      interruptSpeech: () => { /* voiceMode 已 pause ttsAudio */ },
    },
  });

  // ── 发送/停止合并按钮：生成中点击打断（长按录音已取消，见输入区注释）──
  sendBtn.addEventListener('click', (e) => {
    if (!sendBtn.classList.contains('stopping')) return;   // 平时走表单提交
    e.preventDefault();
    if (sendBtn.disabled) return;
    sendBtn.disabled = true;
    post('/stop').catch(() => { /* 忽略 */ }).finally(() => {
      setTimeout(() => { sendBtn.disabled = false; }, 300);
    });
  });

  // ── 右键复制选中文字（气泡内容已可选中）──────────────
  function showCopiedTip() {
    const tip = document.createElement('div');
    tip.className = 'copied-tip';
    tip.textContent = '已复制';
    document.body.appendChild(tip);
    setTimeout(() => tip.remove(), 1200);
  }

  document.addEventListener('contextmenu', (e) => {
    const sel = window.getSelection();
    const text = sel ? sel.toString().trim() : '';
    if (!text) return;                       // 无选中文本 → 不拦截（输入框等默认行为）
    e.preventDefault();
    // 先试 execCommand（同步可靠），失败降级 clipboard API
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch { /* 降级 */ }
    if (!copied) {
      navigator.clipboard.writeText(text).catch(() => { /* 忽略 */ });
    }
    showCopiedTip();
  });

  document.addEventListener('keydown', (e) => {
    // 全局 Ctrl+C 兜底（选中文本时原生已处理，此处确保无焦点时也可用）
    if (e.ctrlKey && (e.key === 'c' || e.key === 'C')) {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : '';
      if (text && document.activeElement
          && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        e.preventDefault();
        try {
          document.execCommand('copy');
          showCopiedTip();
        } catch { /* 忽略 */ }
      }
    }
  });

  // ── 启动 ─────────────────────────────────────────────
  (async () => {
    try {
      const data = await api('/state');
      applyState(data.state);
      state.lastAgendaDate = (data.state.agenda || {}).today || '';
      refreshCalendar();
    } catch {
      setOffline(true);
    }
    loadHistory();
    refreshWs();
    refreshLlm();
    refreshStats();
  })();
})();
