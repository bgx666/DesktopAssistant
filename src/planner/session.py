"""PlannerSession —— 学习工作助手会话（并发/调度/事件/heartbeat/记忆树）。

移植自 yaya backend session.py：
- buffer + chat_lock / buffer_lock(RLock) / _inbox 并发模式；
- 后端自建调度线程（daemon）：LLM 自主 heartbeat 唤醒 + 定时触发点（早晨/晚间/逾期）；
- 事件队列（/dequeue drain）+ 流式文本推送（StreamTextMiddleware）；
- 记忆树 + buffer 持久化（SQLiteMemoryTree.save_buffer_state，重启恢复）。

planner 特有：
- 任务库（TasksDb）与计划快照注入（PlanSnapshotMiddleware）；
- 免打扰窗口（DND）：默认 22:00-08:00，可一次性 override；
- 心跳单位是分钟（比 xiaob 的秒级长得多），clamp 10~720 分钟。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from . import config as _config
from .asr import AsrClient
from .llm import MockChatModel, build_chat_model
from .memory.sqlite_memory_tree import SQLiteMemoryTree
from .middleware import SUMMARIZE_TRIGGER_MESSAGES as _SUMMARIZE_TRIGGER
from .store.tasks_db import TasksDb
from .tts import TtsClient

_logger = logging.getLogger("planner.session")

CHARACTER_ID = "assistant"
DISPLAY_NAME = "小助"
PLAYER_NAME = "用户"
BACKEND_TAG = "planner/1"

_TZ = timezone(timedelta(hours=8))
FALLBACK_HEARTBEAT_MINUTES = _config.PLANNER_FALLBACK_MINUTES
MAX_WORKER_ROUNDS = 3            # 生成期间到达的新消息最多再补 N 轮
DEFAULT_WAKE_MINUTES = 30        # 首次启动/未调度时的默认唤醒间隔
STARTUP_GRACE_SECONDS = 120      # 启动宽限期：打开后 2 分钟内不自动说话（心跳/逾期都不触发）

# 心跳节奏：分钟级定时任务（一人一句，无秒级短心跳）
# - 用户说话后不重置心跳（保持原定时，AI 不主动插话）
# - 沉默时每次心跳逐步加长，避免烦人
SILENT_ESCALATE_STEP = 10        # 沉默时每次心跳加长的分钟数
SILENT_ESCALATE_MAX = 120        # 沉默加长上限


def _now() -> datetime:
    return datetime.now(_TZ)


def _close_stream_bg(gen) -> None:
    """后台关闭 langgraph 同步流。

    langgraph 的 sync stream 被 close 时会等底层模型流自然结束
    （BackgroundExecutor.__exit__ 里 concurrent.futures.wait(pending)），
    若在主线程 close 会阻塞整轮生成——放到 daemon 线程做，主线程立即返回，
    chat_lock / _generating 尽快释放给下一条消息。
    """
    try:
        gen.close()
    except Exception:
        pass


class PlannerSession:
    """小助的会话状态：任务库、对话 buffer、事件队列、调度线程、免打扰、记忆树。"""

    def __init__(self, data_root: Path | None = None, mock: bool | None = None) -> None:
        self.data_root = Path(data_root) if data_root else _config.data_root()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.mock = _config.PLANNER_MOCK_LLM if mock is None else mock
        self.mode: str = "mock" if self.mock else "llm"

        # 用户设置（data/settings.json；压缩参数/长按时间/LLM 配置）
        from .settings import load_settings
        self.settings: dict = load_settings(self.data_root)

        # 压缩摘要语言（评测用）：None/zh = 中文指令（生产默认），"en" = 英文
        self.summary_language: str | None = None
        # 压缩用 system prompt 覆盖（评测用英文摘要系统提示，否则中文角色卡
        # 会压过英文压缩指令导致摘要语言不受控）；None = 主会话角色卡
        self.summary_system_prompt: str | None = None

        # 存储
        self.db = TasksDb(self.data_root / "planner.db")
        self.memory_tree: SQLiteMemoryTree | None = None

        # 语音合成（本地 Kokoro 默认；cloud=DashScope；mimo=小米 MiMo）
        if _config.PLANNER_TTS_ENGINE == "mimo":
            self.tts = TtsClient(
                self.data_root,
                engine=_config.PLANNER_TTS_ENGINE,
                api_key=_config.PLANNER_MIMO_API_KEY,
                model=_config.PLANNER_MIMO_MODEL,
                voice=_config.PLANNER_MIMO_VOICE,
                base_url=_config.PLANNER_MIMO_BASE_URL,
            )
        else:
            self.tts = TtsClient(
                self.data_root,
                engine=_config.PLANNER_TTS_ENGINE,
                api_key=_config.PLANNER_TTS_API_KEY,
                model=_config.PLANNER_TTS_MODEL,
                voice=_config.PLANNER_TTS_VOICE if _config.PLANNER_TTS_ENGINE == "cloud" else "zf_001",
            )
        # 应用 settings.json 持久化的 TTS 配置（启动即生效，与保存时一致——
        # 否则重启后音色/开关回到默认，设置形同虚设）
        if self.settings.get("tts_voice"):
            if self.settings["tts_voice"] in {v["id"] for v in self.tts.list_voices()}:
                self.tts.voice = self.settings["tts_voice"]
        if "tts_enabled" in self.settings and self.tts._engine_ok:
            # 引擎不可用（如云引擎无 key）时保持禁用，不因默认设置开启
            self.tts._enabled = bool(self.settings["tts_enabled"])

        # 语音输入（SenseVoiceSmall-onnx 本地识别；依赖缺失时静默关闭）
        self.asr = AsrClient()

        # 对话状态
        self.recent_buffer: list[BaseMessage] = []
        self.round: int = 0
        self._msg_counter: int = 0

        # 并发（与 xiaob/yaya 同构）
        self.chat_lock = threading.Lock()
        self.buffer_lock = threading.RLock()
        self._generating: bool = False
        self._inbox: list[BaseMessage] = []
        self.pending_response: bool = False
        self._last_text_len: int = 0   # 最近一轮模型文本长度（continue 分段暂停用）
        self._continue_pause_event = threading.Event()   # continue 分段暂停（可被用户打断唤醒）
        self.current_trigger: str = "player"   # player | heartbeat | scheduled | nudge

        # 对外状态
        self.thinking: bool = False

        # 事件队列（/dequeue drain）：Condition 支持长轮询（无事件时 wait 到有事件/超时）
        self._events: list[dict] = []
        self._events_cond = threading.Condition()

        # 心跳调度（分钟级）
        self._next_heartbeat_at: float = 0.0
        self._heartbeat_minutes: float = 0.0
        self._heartbeat_note: str = ""
        self._heartbeat_silent_count: int = 0   # 连续自主唤醒用户没说话的次数（沉默递进）
        self._activity: str = ""
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at = time.time()   # 启动宽限期起点（打开后 2 分钟不自动说话）
        self._wake_event = threading.Event()

        # 最后活动时间（epoch 秒，持久化）：程序关闭期间的离线时长据此补回归问候
        self._last_activity_at: float = 0.0
        # 上次玩家消息时间（内存）：玩家消息注入「距上次说话 X」间隔提示
        self._last_player_message_at: datetime | None = None
        # 已压缩进记忆树的消息累计条数（持久化）：压缩节点 round_range 用
        # 全局序号（小B _span 机制的对齐），多次压缩范围连续不重叠
        self._compressed_total: int = 0
        # 停止请求（用户点"停止"打断当前生成）：after_model/before_model
        # 中间件检查后跳转 end；每次生成开始时重置
        self._stop_requested: bool = False
        # 异步压缩：达到阈值后由后台线程压缩，空闲时原子替换 buffer
        self._compressing: bool = False
        self._closing: bool = False   # close 信号：压缩线程尽快应用退出

        # 免打扰
        self.dnd_enabled: bool = True
        self.dnd_until: datetime | None = None      # 一次性免打扰截止时间

        # 用户正在输入（瞬态，不持久化；由前端输入框状态驱动）
        self.typing: bool = False

        # 计划快照注入
        self._last_plan_fingerprint: str = ""

        # 懒加载
        self._llm = None
        self._summary_model = None
        self._agent_obj = None
        self._system_prompt: str = ""

        self._system_prompt = self._build_system_prompt()
        self.get_memory_tree()          # 确保 db 文件落盘
        self._load_buffer_state()
        self._check_startup_heartbeat()

    # ── 懒加载 ────────────────────────────────────────────────

    def _get_llm(self):
        if self._llm is None:
            if self.mock:
                self._llm = MockChatModel(session=self)
            else:
                from .settings import DEFAULT_SETTINGS
                s = self.settings
                self._llm = build_chat_model(
                    api_key=s.get("llm_api_key") or None,
                    base_url=s.get("llm_base_url") or None,
                    model_name=s.get("llm_model") or None,
                )
        return self._llm

    def _get_summary_model(self):
        """压缩用独立模型（与主对话同配置，服务端 prompt caching 命中）。"""
        if self._summary_model is None:
            if self.mock:
                self._summary_model = MockChatModel()
            else:
                from .settings import DEFAULT_SETTINGS
                s = self.settings
                self._summary_model = build_chat_model(
                    api_key=s.get("llm_api_key") or None,
                    base_url=s.get("llm_base_url") or None,
                    model_name=s.get("llm_model") or None,
                )
        return self._summary_model

    @property
    def _agent(self):
        if self._agent_obj is None:
            from .agent import build_planner_agent
            self._agent_obj = build_planner_agent(self)
        return self._agent_obj

    def set_chat_model(self, model) -> None:
        """测试桩注入：替换 chat model 并重建 agent。"""
        self._llm = model
        self._agent_obj = None

    def update_settings(self, updates: dict) -> dict:
        """保存并应用设置（应用即生效）：
        - 压缩参数：下次压缩时读取新值（运行时读 settings）
        - LLM 配置：重建模型实例（下次生成生效）
        校验失败抛 ValueError。
        """
        from .settings import save_settings
        old_llm = (self.settings.get("llm_api_key"), self.settings.get("llm_base_url"),
                   self.settings.get("llm_model"))
        self.settings = save_settings(self.data_root, updates)
        new_llm = (self.settings.get("llm_api_key"), self.settings.get("llm_base_url"),
                   self.settings.get("llm_model"))
        if old_llm != new_llm:
            self._llm = None
            self._summary_model = None
            self._agent_obj = None
            _logger.info("[settings] LLM 配置变更，已重建模型")
        # TTS 设置即时生效：音色校验 + 启用开关
        tts_voice = self.settings.get("tts_voice")
        if tts_voice and tts_voice != getattr(self.tts, "voice", None):
            if tts_voice in {v["id"] for v in self.tts.list_voices()}:
                self.tts.voice = tts_voice
                _logger.info("[settings] 音色切换为 %s", tts_voice)
            else:
                _logger.warning("[settings] 未知音色 %s，忽略", tts_voice)
        if "tts_enabled" in self.settings:
            self.tts._enabled = bool(self.settings["tts_enabled"])
            _logger.info("[settings] 语音播报 %s", "开启" if self.tts._enabled else "关闭")
        _logger.info("[settings] 设置已保存并应用: %s", {k: self.settings[k] for k in updates if k in self.settings})
        return dict(self.settings)

    def toggle_mock(self) -> str:
        """运行时切换 Mock/真实 LLM。返回切换后的模式。"""
        self.mock = not self.mock
        self.mode = "mock" if self.mock else "llm"
        self._llm = None
        self._summary_model = None
        self._agent_obj = None
        _logger.info("[session] 切换为 %s 模式", self.mode)
        return self.mode

    def get_memory_tree(self) -> SQLiteMemoryTree:
        if self.memory_tree is None:
            self.memory_tree = SQLiteMemoryTree(CHARACTER_ID, self.data_root / "memory_tree.db")
        return self.memory_tree

    def close(self) -> None:
        """优雅关闭：停调度 → 等生成中的 worker 结束 → 等后台压缩 → 落盘 → 关库。"""
        self.stop_heartbeat()
        try:
            with self.chat_lock:  # 等待在途生成完成（worker 持锁期间）
                pass
        except Exception:
            pass
        # 等待后台压缩线程结束：否则它可能并发关闭 sqlite 连接 → 原生崩溃
        self._closing = True
        deadline = time.time() + 15
        while self._compressing and time.time() < deadline:
            time.sleep(0.05)
        self._save_buffer_state()
        if self.memory_tree:
            self.memory_tree.close()
        self.db.close()

    # ── system prompt ─────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        return (_config.PROMPTS_DIR / "system.md").read_text(encoding="utf-8")

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    # ── 事件队列 ──────────────────────────────────────────────

    def push_event(self, ev: dict) -> None:
        with self._events_cond:
            self._events.append(ev)
            self._events_cond.notify_all()

    def push_text(self, content: str) -> None:
        self.push_event({"type": "text", "content": content, "from": CHARACTER_ID})

    def _maybe_speak(self, text: str) -> None:
        """完整文本收束后后台合成语音（气泡朗读）。

        只发 audio 事件，播放与否由主进程按面板状态决定（悬浮球形态才播）；
        失败/未配置 key 静默忽略，不影响生成。mock 模式不合成（测试/演示）。
        """
        if self.mock:
            return
        if not getattr(self, "tts", None) or not self.tts.enabled:
            return
        if not text or not str(text).strip():
            return
        self.tts.synthesize_async(
            str(text),
            lambda url: url and self.push_event({"type": "audio", "url": url}),
        )

    def pause_before_continue(self) -> None:
        """continue_speaking 分段：按上段文本长度暂停，让回复一句一句出现。

        公式：基础 2 秒 + 每 10 字 +1 秒（上限 10 秒，防呆）。
        用 Event.wait 实现：用户打断（说话/停止）时 interrupt_continue_pause()
        立即唤醒，暂停即刻结束让位。
        """
        secs = min(10.0, 2.0 + (self._last_text_len or 0) / 10.0)
        _logger.info("[continue] 暂停 %.1f 秒（上段 %d 字）", secs, self._last_text_len or 0)
        self._continue_pause_event.clear()
        self._continue_pause_event.wait(timeout=secs)

    def interrupt_continue_pause(self) -> None:
        """立即唤醒正在进行的 continue 分段暂停（用户说话 / 停止时调用）。"""
        self._continue_pause_event.set()

    def push_log(self, text: str) -> None:
        self.push_event({"type": "log", "text": text})

    def push_plan_update(self) -> None:
        self.push_event({"type": "plan_update", "date": _now().strftime("%Y-%m-%d")})

    def drain_events(self, timeout: float | None = None) -> list[dict]:
        """一次性 drain 事件队列。

        timeout 为 None/0 → 立即返回（现有行为，测试兼容）；
        timeout > 0 → 无事件时等待至多 timeout 秒（长轮询，供 /dequeue?wait=N 用），
        有事件或超时即返回。
        """
        with self._events_cond:
            if timeout:
                deadline = time.time() + timeout
                while not self._events:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self._events_cond.wait(timeout=remaining)
            events = self._events
            self._events = []
            return events

    def _set_thinking(self, value: bool) -> None:
        self.thinking = value
        self.push_event({"type": "thinking", "value": value})

    # ── 免打扰 ────────────────────────────────────────────────

    def set_typing(self, typing: bool) -> None:
        """前端输入框状态：非空 = 正在输入（瞬态，不持久化）。"""
        self.typing = bool(typing)

    def set_dnd(self, enabled: bool, until_hour: int | None = None) -> None:
        with self.buffer_lock:
            self.dnd_enabled = enabled
            if enabled and until_hour is not None:
                now = _now()
                until = now.replace(hour=until_hour, minute=0, second=0, microsecond=0)
                if until <= now:
                    until += timedelta(days=1)
                self.dnd_until = until
            elif not enabled:
                self.dnd_until = None
        self.push_event({"type": "dnd", "enabled": self.dnd_enabled})
        self._wake_event.set()

    def in_dnd(self, at: datetime | None = None) -> bool:
        at = at or _now()
        with self.buffer_lock:
            if self.dnd_until is not None:
                return at < self.dnd_until
            if not self.dnd_enabled:
                return False
            start = _config.PLANNER_DND_START_HOUR
            end = _config.PLANNER_DND_END_HOUR
            h = at.hour
            if start <= end:
                return start <= h < end
            return h >= start or h < end  # 跨天窗口（如 22-8）

    # ── 心跳状态 ──────────────────────────────────────────────

    def set_heartbeat_state(self, minutes: float, note: str = "") -> None:
        minutes = max(_config.PLANNER_HEARTBEAT_MIN_MINUTES,
                      min(_config.PLANNER_HEARTBEAT_MAX_MINUTES, float(minutes)))
        with self.buffer_lock:
            self._heartbeat_minutes = minutes
            self._heartbeat_note = note
            self._next_heartbeat_at = time.time() + minutes * 60
        self._wake_event.set()
        _logger.info("[heartbeat] 调度: %s后（%s）", self._fmt_duration(minutes), note)

    def schedule_heartbeat(self, minutes: float, note: str = "") -> None:
        self.set_heartbeat_state(minutes, note)

    @staticmethod
    def _fmt_duration(minutes: float) -> str:
        """分钟数 → 可读时长（秒级显示）。"""
        if minutes < 1:
            return f"{max(1, int(round(minutes * 60)))} 秒"
        if minutes == int(minutes):
            return f"{int(minutes)} 分钟"
        return f"{minutes:.1f} 分钟"

    def _cancel_heartbeat(self) -> None:
        """取消挂起的心跳（玩家说话时重置旧的长时间心跳用）。"""
        with self.buffer_lock:
            self._next_heartbeat_at = 0.0
        self._wake_event.set()

    def _next_silent_minutes(self) -> float:
        """按沉默次数计算下次心跳分钟数：10 起步 → 沉默逐步加长 → 上限 120。"""
        base = float(_config.PLANNER_HEARTBEAT_MIN_MINUTES)   # 10 分钟
        if self._heartbeat_silent_count <= 0:
            return base
        return min(SILENT_ESCALATE_MAX,
                   base + self._heartbeat_silent_count * SILENT_ESCALATE_STEP)

    def heartbeat_dict(self) -> dict:
        with self.buffer_lock:
            if self._next_heartbeat_at <= 0:
                return {"in_minutes": 0, "in_seconds": 0, "note": ""}
            in_seconds = max(0, int(self._next_heartbeat_at - time.time()))
            return {"in_seconds": in_seconds,
                    "in_minutes": max(0, int(in_seconds / 60)),
                    "note": self._heartbeat_note}

    # ── 待办队列快照 ──────────────────────────────────────────

    def _plan_snapshot_text(self) -> str | None:
        """动态待办队列指纹变化时生成注入文本（None = 无变化）。"""
        try:
            s = self.db.summary()
            fingerprint = json.dumps({
                "t": s["tasks"],
                "q": [(p["id"], p["status"], p["priority"]) for p in s["queue"]],
                "overdue": [t["id"] for t in s["overdue_tasks"]],
            }, ensure_ascii=False, sort_keys=True)
            if fingerprint == self._last_plan_fingerprint:
                return None
            self._last_plan_fingerprint = fingerprint
            lines = [f"[当前待办]（{s['today']}，{_now().strftime('%H:%M')}）"]
            queue = s["queue"]
            if queue:
                lines.append(f"待办队列（{len(queue)} 项未完成，按紧急度排序）：")
                for i, p in enumerate(queue[:8], 1):
                    due = p.get("task_due") or ""
                    due_txt = f"，截止 {due}" if due else ""
                    lines.append(f"  {i}. #{p['id']} {p['content']}（{p['task_title']}{due_txt}）")
            else:
                lines.append("（目前没有待办条目。）")
            # 未拆解的任务（提醒 agent 可以拆解或直接建议）
            raw_tasks = [t for t in self.db.list_tasks()
                         if t["status"] in ("todo", "in_progress") and t["plan_total"] == 0]
            if raw_tasks:
                lines.append("尚未拆解的任务：" + "；".join(
                    f"#{t['id']}「{t['title']}」" for t in raw_tasks[:5]))
            if not queue and not raw_tasks:
                lines.append("（目前没有待办任务，可以问问用户最近想做什么。）")
            if s["overdue_tasks"]:
                lines.append("已逾期：" + "；".join(
                    f"#{t['id']}「{t['title']}」" for t in s["overdue_tasks"][:5]))
            if s["tasks"]["in_progress"] or s["tasks"]["todo"]:
                lines.append(f"进行中任务 {s['tasks']['in_progress']} 个，待开始 {s['tasks']['todo']} 个。")
            return "\n".join(lines)
        except Exception:
            _logger.exception("[session] 计划快照生成失败")
            return None

    # ── buffer 维护 ───────────────────────────────────────────

    def _append_to_buffer(self, msg) -> None:
        with self.buffer_lock:
            if isinstance(msg, dict):
                msg = HumanMessage(content=msg.get("content", ""))
            self.recent_buffer.append(msg)
            self._msg_counter += 1

    def _receive(self, content: str, *, trigger: bool = True) -> BaseMessage:
        """接收一条外部消息。_generating 期间入队 _inbox，结束后再写入。返回消息对象。

        显式分配 id（langchain 默认 None，langgraph 在模型调用时才补）——
        撤销按钮依赖消息 id 在入队时就稳定存在。
        消息带 metadata.ts 时间戳（秒级，UTC+8）——压缩节点起止时间的数据来源。
        """
        msg = HumanMessage(content=content, id=uuid.uuid4().hex,
                           metadata={"ts": self._ts_now()})
        # 玩家消息 = 立即打断 continue 分段暂停（让位给用户）
        self.interrupt_continue_pause()
        with self.buffer_lock:
            if self._generating:
                self._inbox.append(msg)
                if trigger:
                    self.pending_response = True
                return msg
            self.recent_buffer.append(msg)
            self._msg_counter += 1
            if trigger:
                self.pending_response = True
        return msg

    @staticmethod
    def _ts_now() -> str:
        """当前时间戳（秒级 ISO，UTC+8）。"""
        return _now().strftime("%Y-%m-%d %H:%M:%S")

    def _stamp_missing_ts(self) -> None:
        """给 buffer 中缺少 ts 的消息补时间戳（agent 生成的 ai/tool 消息、
        旧 buffer 恢复的消息）——保证压缩时每条消息都有时间。"""
        ts = self._ts_now()
        with self.buffer_lock:
            for m in self.recent_buffer:
                meta = getattr(m, "metadata", None)
                if not meta:
                    try:
                        m.metadata = {"ts": ts}      # metadata 为 None（旧数据）→ 重建
                    except Exception:
                        continue
                elif not meta.get("ts"):
                    meta["ts"] = ts

    def _repair_buffer(self) -> None:
        """检查 buffer 末尾是否有孤儿 tool_call，自动补 tool 消息（防御用）。"""
        with self.buffer_lock:
            buf = self.recent_buffer
            for i in range(len(buf) - 1, -1, -1):
                m = buf[i]
                if m.type == "tool":
                    break
                tcs = getattr(m, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        tid = tc.get("id") or f"repair_{uuid.uuid4().hex[:12]}"
                        buf.append(ToolMessage(content="（工具调用被中断）", tool_call_id=tid))
                    break

    def _strip_orphan_tool_messages(self) -> None:
        """删除无配对 tool_calls 的孤儿 tool 消息（防御 DeepSeek 400）。

        压缩 batch 若切开 ai(tool_calls) 与其工具结果，可能留下孤儿
        ToolMessage；任何来源的孤儿都在生成输入前清掉。
        """
        with self.buffer_lock:
            call_ids = set()
            for m in self.recent_buffer:
                for tc in (getattr(m, "tool_calls", None) or []):
                    call_ids.add(tc.get("id"))
            kept = [m for m in self.recent_buffer
                    if not (getattr(m, "type", None) == "tool"
                            and (getattr(m, "tool_call_id", None) or "") not in call_ids)]
            if len(kept) != len(self.recent_buffer):
                _logger.info("[repair] 清理 %d 条孤儿 tool 消息",
                             len(self.recent_buffer) - len(kept))
                self.recent_buffer = kept

    def _save_buffer_state(self) -> None:
        """buffer 持久化（messages_to_dict → json，存进 memory_tree.db）。"""
        try:
            from langchain_core.messages import messages_to_dict
            last_msg_at = None
            if self._last_player_message_at is not None:
                last_msg_at = self._last_player_message_at.timestamp()
            self.get_memory_tree().save_buffer_state(
                messages_to_dict(self.recent_buffer), self._msg_counter, self.round,
                last_activity_at=self._last_activity_at or None,
                reminded_overdue=list(getattr(self, "_reminded_overdue", set())),
                compressed_total=self._compressed_total,
                next_heartbeat_at=self._next_heartbeat_at or None,
                heartbeat_minutes=self._heartbeat_minutes or None,
                heartbeat_note=self._heartbeat_note or None,
                last_player_message_at=last_msg_at,
            )
        except Exception as exc:
            _logger.warning("[session] 保存 buffer 状态失败: %s", exc)

    def _load_buffer_state(self) -> bool:
        """从 memory_tree.db 恢复消息列表。"""
        try:
            from langchain_core.messages import messages_from_dict
            state = self.get_memory_tree().load_buffer_state()
            if not state or not state["recent_buffer"]:
                self._last_activity_at = (state or {}).get("last_activity_at", 0.0) or 0.0
                reminded = (state or {}).get("reminded_overdue", [])
                if reminded:
                    self._reminded_overdue = set(reminded)
                self._compressed_total = int((state or {}).get("compressed_total", 0) or 0)
                self._restore_heartbeat_state(state or {})
                self._restore_last_player_at(state or {})
                return False
            msgs = messages_from_dict(state["recent_buffer"])
            if msgs:
                self.recent_buffer = msgs
                self._stamp_missing_ts()   # 旧数据/缺时间戳的消息补打（压缩时间字段的数据来源）
                self._msg_counter = state.get("_msg_counter", len(msgs))
                self.round = state.get("round", 0)
                self._last_activity_at = state.get("last_activity_at", 0.0) or 0.0
                reminded = state.get("reminded_overdue", [])
                if reminded:
                    self._reminded_overdue = set(reminded)
                self._compressed_total = int(state.get("compressed_total", 0) or 0)
                self._restore_heartbeat_state(state)
                self._restore_last_player_at(state)
                _logger.info("[session] 从 buffer_state 恢复上下文: %d 条消息", len(msgs))
                return True
            return False
        except Exception as exc:
            _logger.warning("[session] 加载 buffer 状态失败: %s", exc)
            return False

    def _restore_heartbeat_state(self, state: dict) -> None:
        """恢复心跳调度（跨重启剩余时间扣减离线时长）。"""
        self._next_heartbeat_at = float(state.get("next_heartbeat_at", 0.0) or 0.0)
        self._heartbeat_minutes = float(state.get("heartbeat_minutes", 0.0) or 0.0)
        self._heartbeat_note = str(state.get("heartbeat_note", "") or "")
        if self._next_heartbeat_at > 0:
            remain = self._next_heartbeat_at - time.time()
            _logger.info("[heartbeat] 恢复调度：剩余 %.0f 秒", max(0, remain))

    def _restore_last_player_at(self, state: dict) -> None:
        """恢复上次玩家消息时间（跨重启间隔提示累积）。"""
        ts = float(state.get("last_player_message_at", 0.0) or 0.0)
        if ts > 0:
            self._last_player_message_at = datetime.fromtimestamp(ts, tz=_TZ)

    def _check_startup_heartbeat(self) -> None:
        """启动时检查心跳：未到期保留剩余时间；已到期立即补触发一次。

        需求：跨重启继承心跳进度，而不是完全重置。
        - 离线时长未超过剩余时间 → 保留原到期时刻（剩余时间自动扣减离线时长）。
        - 离线时长已超过剩余时间 → 启动时立即触发一次心跳，并落保底调度。
        """
        if self._next_heartbeat_at <= 0:
            return
        if time.time() < self._next_heartbeat_at:
            remain = self._next_heartbeat_at - time.time()
            _logger.info("[heartbeat] 恢复调度：剩余 %.0f 秒", max(0, remain))
            return   # 还没到期：保留原到期时刻（剩余时间已扣减离线时长）
        _logger.info("[heartbeat] 启动时心跳已到期，立即补触发")
        self._fire_heartbeat()

    # ── 异步压缩（后台线程，不阻塞生成）──────────────────────

    def _maybe_compress_async(self) -> None:
        """阈值检查：raw ≥ 60 且未在压缩 → spawn 后台压缩线程。

        压缩线程对 buffer 快照压缩（不触碰原 buffer），压缩完成后等待
        空闲窗口（无生成/无待处理消息），原子替换"新消息以外"的旧部分。
        """
        if self._compressing:
            return
        with self.buffer_lock:
            raw = [m for m in self.recent_buffer
                   if "node_id" not in (getattr(m, "metadata", None) or {})]
            if len(raw) < _SUMMARIZE_TRIGGER:
                return
            snapshot = list(self.recent_buffer)
        self._compressing = True
        _logger.info("[compress] 触发异步压缩（raw=%d）", len(raw))
        threading.Thread(target=self._run_async_compression, args=(snapshot,),
                         name="planner-compress", daemon=True).start()

    def _run_async_compression(self, snapshot: list) -> None:
        """后台压缩线程：快照压缩 → 等空闲 → 原子替换 buffer。

        替换 = 按消息 id 删除被压缩消息 + 插入节点消息 + 节点排序——
        压缩期间新到的消息（用户输入/模型生成）id 不在删除集，自然保留，
        与压缩结果合并；_compressed_total 已在压缩时累加。
        """
        try:
            from .middleware import SummarizationMiddleware
            comp = SummarizationMiddleware(self)
            removes, adds = comp.compress_snapshot(snapshot)
            if not removes and not adds:
                return
            remove_ids = {(getattr(m, "id", None) or id(m)) for m in removes}

            # 等空闲：用户打字/生成间隙必然出现，替换是毫秒级原子操作；
            # close 信号（_closing）→ 立即应用退出（close 已在等 chat_lock，无并发生成）
            while True:
                with self.buffer_lock:
                    idle = ((not self._generating and not self.pending_response
                             and not self._inbox) or self._closing)
                    if idle:
                        break
                time.sleep(0.2)

            with self.buffer_lock:
                self.recent_buffer = [
                    m for m in self.recent_buffer
                    if (getattr(m, "id", None) or id(m)) not in remove_ids]
                self.recent_buffer.extend(adds)
                self._strip_orphan_tool_messages()   # 防御：压缩切段可能留孤儿 tool
                self._reorder_node_messages()
            self._save_buffer_state()
            self.push_event({"type": "memory_update"})
            _logger.info("[compress] 异步压缩完成：删除 %d 条，插入 %d 节点消息",
                         len(remove_ids), len(adds))
        except Exception:
            _logger.exception("[compress] 异步压缩异常")
        finally:
            self._compressing = False

    def _log_dir(self) -> Path:
        d = self.data_root / CHARACTER_ID
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_log(self, role: str, content: str) -> None:
        try:
            today = _now().strftime("%Y-%m-%d")
            line = json.dumps({
                "timestamp": _now().isoformat(),
                "role": role, "content": content, "round": self.round,
            }, ensure_ascii=False) + "\n"
            with open(self._log_dir() / f"{today}.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as exc:
            _logger.warning("[session] 日志写入失败: %s", exc)

    # ── 调度线程 ──────────────────────────────────────────────

    def start_heartbeat(self) -> None:
        """启动调度 daemon 线程（幂等）。"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._schedule_loop, name="planner-scheduler", daemon=True)
        self._heartbeat_thread.start()
        if self._next_heartbeat_at <= 0:
            self.schedule_heartbeat(DEFAULT_WAKE_MINUTES)

    def stop_heartbeat(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)

    def _schedule_loop(self) -> None:
        """调度线程主循环：heartbeat 到点 + 逾期检查（启动宽限期内全部静默）。

        启动宽限期（STARTUP_GRACE_SECONDS）内不触发任何自主行为——
        定时唤醒/逾期提醒都不做，避免"一打开小助就说话"的突兀感。
        """
        while not self._stop_event.is_set():
            if not self._in_startup_grace():
                now = _now()
                # 逾期检查（每 10 分钟一次，避免重复轰炸）
                if now.minute % 10 == 0:
                    self._check_overdue()
                # 心跳到点
                with self.buffer_lock:
                    next_at = self._next_heartbeat_at
                if next_at > 0 and time.time() >= next_at:
                    self._fire_heartbeat()
            self._maybe_compress_async()   # 达到阈值 → 后台压缩（不阻塞）
            self._wake_event.wait(timeout=5)
            self._wake_event.clear()

    def _in_startup_grace(self) -> bool:
        """启动宽限期：启动后 STARTUP_GRACE_SECONDS 秒内不触发自主行为。"""
        return time.time() - self._started_at < STARTUP_GRACE_SECONDS

    def _fire_heartbeat(self) -> None:
        with self.chat_lock:
            with self.buffer_lock:
                if self._generating or self.pending_response:
                    return  # 玩家消息优先
                if self._next_heartbeat_at <= 0 or time.time() < self._next_heartbeat_at:
                    return
                self._next_heartbeat_at = 0.0
                minutes = self._heartbeat_minutes or FALLBACK_HEARTBEAT_MINUTES
                note = self._heartbeat_note
                self._heartbeat_note = ""
            if self.in_dnd():
                # 免打扰：不打扰，顺延一个正常间隔
                self._heartbeat_silent_count += 1
                self.schedule_heartbeat(minutes, note)
                _logger.info("[heartbeat] 免打扰时段，顺延")
                return
            # 自主唤醒 = 用户沉默一次 → 心跳逐步加长
            self._heartbeat_silent_count += 1
            _logger.info("[heartbeat] 触发自主生成（距上次 %s：%s）",
                         self._fmt_duration(minutes), note)
            # 先落一个保底调度（生成中 LLM 会重新设置覆盖）：
            # 防止触发后进程退出/中断导致 next=0 落盘 → 重启后心跳被重置；
            # 保底沿用原心跳间隔，避免"重置感"
            self.schedule_heartbeat(minutes, note)
            self._save_buffer_state()   # 立即落盘：退出/中断后重开恢复新计时
            text = (f"（系统：定时任务到点，主动和用户说说话。{note + '。' if note else ''}"
                    f"可以看看用户的任务进度，提醒用户该做的事，说说你的想法。）")
            self._receive(text, trigger=True)
            self._spawn_worker("heartbeat")

    def _check_overdue(self) -> None:
        """逾期任务检查（每天至多提醒一次，去重用集合）。"""
        if self.in_dnd():
            return
        try:
            overdue = self.db.list_overdue_tasks(_now().strftime("%Y-%m-%d"))
        except Exception:
            return
        pending = [t for t in overdue if t["id"] not in getattr(self, "_reminded_overdue", set())]
        if not pending:
            return
        if not hasattr(self, "_reminded_overdue"):
            self._reminded_overdue = set()
        if len(pending) > 3:
            pending = pending[:3]
        for t in pending:
            self._reminded_overdue.add(t["id"])
        self._save_buffer_state()   # 去重持久化，避免重启后重复提醒
        with self.chat_lock:
            with self.buffer_lock:
                if self._generating:
                    return
            text = ("（系统：有几个任务已经逾期还没完成："
                    + "；".join(f"#{t['id']}「{t['title']}」截止 {t['due_date']}" for t in pending)
                    + "。提醒用户。）")
            self._receive(text, trigger=True)
            self._spawn_worker("scheduled")

    # ── 玩家消息 ──────────────────────────────────────────────

    @staticmethod
    def _fmt_gap(seconds: float) -> str:
        """秒数 → 可读间隔（秒/分秒/小时分）。"""
        s = max(0, int(seconds))
        if s < 60:
            return f"{s} 秒"
        if s < 3600:
            return f"{s // 60} 分 {s % 60} 秒"
        return f"{s // 3600} 小时 {(s % 3600) // 60} 分钟"

    def enqueue_player_message(self, message: str, files: list | None = None) -> str:
        """注入玩家消息并立即触发回复生成。返回该消息的 id（撤销按钮用）。

        用户说话 = 活跃状态：清零沉默计数（有回应）。
        **不重置心跳**：一人一句——AI 回答后保持原有定时，不主动插话。
        消息附带「距上次说话 X」——让 AI 感知对话节奏（间隔信息放在
        "对你说："之前，/history 切分时丢弃，对话框不显示）。

        files：拖入挂载的文件 [{name, path, kind, content?}]，按 kind 分流注入
        （text 内容直注 / doc 解析落盘+预览 / image OCR 提取 / 其他仅路径）。
        """
        now = _now()
        self._heartbeat_silent_count = 0
        gap_hint = ""
        if self._last_player_message_at is not None:
            gap_hint = f"（距上次说话 {self._fmt_gap((now - self._last_player_message_at).total_seconds())}）"
        self._last_player_message_at = now
        text = message or "请结合我给你的文件回答。"
        attachments = self._render_attachments(files) if files else ""
        msg = self._receive(
            f"[{now.strftime('%H:%M')}]{gap_hint} {PLAYER_NAME}对你说：{text}{attachments}",
            trigger=True)
        self._save_log("user", message or "（拖入文件）")
        self._wake_event.set()
        threading.Thread(target=self._player_worker, name="planner-player", daemon=True).start()
        return getattr(msg, "id", None) or ""

    def _render_attachments(self, files: list) -> str:
        """按 kind 分流注入挂载文件（标注来源；失败降级为仅路径）。"""
        from .fileparse import parse_file, save_attachment_text

        parts = []
        for f in files:
            name = str(f.get("name") or "未命名")
            path = str(f.get("path") or "")
            kind = str(f.get("kind") or "other")
            content = f.get("content")
            loc = f"「{path}」" if path else ""
            if kind == "text" and content:
                parts.append(f"- 文本文件 {name}{loc}：\n{content}")
            elif kind == "doc":
                parsed = parse_file(path)
                if parsed:
                    txt = save_attachment_text(self.data_root, path, parsed)
                    preview = parsed[: 8000]
                    parts.append(
                        f"- 文档 {name}{loc}：已解析，文本存于 {txt}。"
                        f"预览：\n{preview}\n（完整内容请用 read_file 工具读取该 txt）")
                else:
                    parts.append(f"- 文档 {name}{loc}：（解析失败或不支持此格式，仅提供文件名）")
            elif kind == "image":
                from .ocr import _global_client as _ocr_global
                text = _ocr_global().recognize_path(path)
                if text:
                    parts.append(f"- 图片 {name}{loc}：【图片 OCR 识别文字】\n{text}")
                else:
                    parts.append(f"- 图片 {name}{loc}：（OCR 未能识别出文字）")
            else:
                parts.append(f"- 文件 {name}{loc}")
        if not parts:
            return ""
        return "\n\n【拖入的文件】\n" + "\n\n".join(parts)

    def undo_message(self, msg_id: str) -> dict:
        """撤销：从 buffer 删除该玩家消息及其后的所有对话。

        原始消息已不在 buffer（被压缩进记忆树）→ 无法撤销；
        生成中撤销会被 _run_agent 的 final_messages 回写覆盖 → 拒绝。
        """
        with self.buffer_lock:
            if self._generating:
                return {"ok": False, "reason": "generating",
                        "error": "小助正在回复中，请先点「停止」再撤销"}
            idx = None
            for i, m in enumerate(self.recent_buffer):
                if (getattr(m, "type", None) == "human"
                        and (getattr(m, "id", None) or id(m)) == msg_id):
                    idx = i
                    break
            if idx is None:
                return {"ok": False, "reason": "compressed",
                        "error": "该消息已被压缩进记忆树，无法撤销"}
            removed = self.recent_buffer[idx:]
            self.recent_buffer = self.recent_buffer[:idx]
            self._msg_counter = max(0, self._msg_counter - len(removed))
            self._repair_buffer()
        _logger.info("[undo] 撤销 %d 条对话（从消息 %s 起）", len(removed), msg_id)
        # 撤销 = 用户介入 = 活跃：清零沉默计数，不重置心跳（一人一句），保存状态
        self._heartbeat_silent_count = 0
        self._save_buffer_state()
        self.push_event({"type": "log", "text": f"已撤销 {len(removed)} 条对话"})
        self.push_plan_update()
        return {"ok": True, "removed": len(removed)}

    def request_stop(self) -> None:
        """请求停止当前生成（用户点「停止」）。

        生成会在下一个安全点收尾（当前模型调用结束、工具执行完后），
        已生成的消息保留在 buffer；停止后无需心跳兜底之外的额外处理。
        """
        self._stop_requested = True
        _logger.info("[stop] 收到停止请求")

    def _player_worker(self) -> None:
        for _ in range(MAX_WORKER_ROUNDS):
            with self.chat_lock:
                with self.buffer_lock:
                    if not self.pending_response:
                        return
                    self.pending_response = False
                try:
                    self._generate_response("player")
                except Exception:
                    _logger.exception("[chat] 玩家消息生成异常")
                    self.push_log("小助走神了一下（生成出错）")
                    return
            with self.buffer_lock:
                if not self.pending_response:
                    return

    def _spawn_worker(self, trigger: str) -> None:
        def _run():
            with self.chat_lock:
                with self.buffer_lock:
                    if not self.pending_response:
                        return
                    self.pending_response = False
                try:
                    self._generate_response(trigger)
                except Exception:
                    _logger.exception("[scheduler] 自主生成异常")
                    self.push_log("小助走神了一下（生成出错）")
        threading.Thread(target=_run, name=f"planner-{trigger}", daemon=True).start()

    # ── agent 单回合 ──────────────────────────────────────────

    def _generate_response(self, trigger: str) -> None:
        with self.buffer_lock:
            self._generating = True
            self._stop_requested = False   # 每次生成重置停止标志
            self.current_trigger = trigger
        self._set_thinking(True)
        try:
            self._run_agent()
        finally:
            self._set_thinking(False)
            with self.buffer_lock:
                self._generating = False
                self._last_activity_at = time.time()   # 任何生成都算一次活动
                self.current_trigger = "player"
                while self._inbox:
                    m = self._inbox.pop(0)
                    self.recent_buffer.append(m)
                    self._msg_counter += 1
                    self.pending_response = True
            self._save_buffer_state()

    def _run_agent(self) -> None:
        """agent.stream：model ↔ tools 循环直到停止（无工具调用 / heartbeat / 玩家让位）。

        流式输出：stream_mode=["messages", "values"]——
        - messages：AIMessageChunk 逐 token → push text_stream（面板逐字渲染）；
          非流式模型（mock）yield 完整 AIMessage → 整段作为一条 chunk
        - values：完整 state（removes 已生效）→ 最终 messages 回写 buffer；
          每轮模型文本收束时 push 完整 text（气泡 toast 用）
        """
        from langchain_core.messages import AIMessage, AIMessageChunk

        self._repair_buffer()
        self._strip_orphan_tool_messages()
        with self.buffer_lock:
            input_msgs = list(self.recent_buffer)
        final_messages = None
        current_msg_id = None
        current_chunks = []
        # 已见消息 id（而非 seen_count=len(msgs)）：压缩中间件会 RemoveMessage
        # 收缩消息列表，按长度增量会漏掉压缩后新增的 ToolMessage → 工具卡片
        # 永远停在"正在执行"转圈。按消息 id 去重，与列表收缩无关。
        seen_msg_ids = {getattr(m, "id", None) or id(m) for m in input_msgs}
        interrupted = False
        try:
            stream = self._agent.stream(
                {"messages": input_msgs, "model_call_count": 0, "set_heartbeat_called": False},
                stream_mode=["messages", "values"],
            )
            for item in stream:
                # 真打断：用户说话/点停止 → 立即停止消费流。已流式的半句留在面板
                # （text_stream），不落 buffer（被打断的回复不入上下文，下一轮从
                # 干净状态续接）。
                if self._stop_requested:
                    interrupted = True
                    break
                kind = item[0]
                data = item[1]
                if kind == "messages":
                    chunk, metadata = data
                    if (metadata or {}).get("langgraph_node") != "model":
                        continue
                    # 真实流式模型 → AIMessageChunk（逐 token）；非流式（mock）→ 完整 AIMessage
                    if not isinstance(chunk, (AIMessage, AIMessageChunk)):
                        continue
                    content = chunk.content or ""
                    if not content:
                        continue
                    if current_msg_id is None:
                        current_msg_id = uuid.uuid4().hex[:12]
                    current_chunks.append(content)
                    self.push_event({
                        "type": "text_stream",
                        "content": content,
                        "msg_id": current_msg_id,
                    })
                elif kind == "values":
                    # 完整 state：最终消息列表（removes 已生效）+ heartbeat 标志
                    msgs = data.get("messages")
                    if msgs is not None:
                        final_messages = msgs
                    # 增量检测工具调用与结果（工具卡片事件）：按消息 id 去重，
                    # 不受压缩 RemoveMessage 收缩列表影响
                    if msgs is not None:
                        for m in msgs:
                            mid = getattr(m, "id", None) or id(m)
                            if mid not in seen_msg_ids:
                                seen_msg_ids.add(mid)
                                self._emit_tool_events(m)
                    # 每轮模型文本收束 → 完整文本事件（气泡 toast 用）
                    if current_chunks:
                        full_text = "".join(current_chunks)
                        self._last_text_len = len(full_text)   # continue 分段暂停按此计算
                        self.push_text(full_text)
                        self._save_log("assistant", full_text)
                        self._maybe_speak(full_text)
                        current_chunks = []
                        current_msg_id = None
        except Exception as exc:
            _logger.exception("[agent] stream 异常")
            self.push_log(f"小助走神了一下（生成出错：{exc}）")
            self.schedule_heartbeat(self._next_silent_minutes())
            return
        if interrupted:
            _logger.info("[agent] 用户打断，立即停止当前生成")
            # 后台关闭流：langgraph sync stream 的 close() 会阻塞等待底层模型流
            # 自然结束，放 daemon 线程做——主线程立即返回，chat_lock/_generating
            # 尽快释放给下一条消息。del stream 移除本地引用，返回时不再二次 close。
            threading.Thread(target=_close_stream_bg, args=(stream,),
                             name="planner-stream-close", daemon=True).start()
            del stream
        if final_messages:
            with self.buffer_lock:
                # 过滤"无话可说"的空回复（无文本且无工具调用）：
                # 心跳允许留空，空 AI 消息不落 buffer（避免污染后续上下文）
                filtered = [
                    m for m in final_messages
                    if not (getattr(m, "type", None) == "ai"
                            and not str(getattr(m, "content", "") or "").strip()
                            and not getattr(m, "tool_calls", None))
                ]
                # 生成期间新追加的消息不能被 final_messages 回写覆盖：
                # 输入是 buffer 快照，期间到达的玩家消息（语音/打字）必须保留，
                # 交由下一轮生成处理（pending_response 已由 enqueue 置位）
                in_ids = {getattr(m, "id", None) or id(m) for m in input_msgs}
                extra = [m for m in self.recent_buffer
                         if (getattr(m, "id", None) or id(m)) not in in_ids
                         and getattr(m, "type", None) == "human"]
                self.recent_buffer = filtered + extra
            self._stamp_missing_ts()   # agent 生成的 ai/tool 消息补时间戳
            self._reorder_node_messages()
        self.push_plan_update()

    def _reorder_node_messages(self) -> None:
        """压缩节点移到 buffer 开头（按 node_start 排序）。

        压缩中间件经 langgraph add_messages 把节点消息追加到列表末尾；
        多次压缩后节点会卡在对话中间（早期摘要插在晚期对话之间，时间顺序
        错乱）。压缩的总是最早的消息，节点代表被压缩的早期对话，应排最前。
        """
        with self.buffer_lock:
            nodes = [m for m in self.recent_buffer
                     if "node_id" in (getattr(m, "metadata", None) or {})]
            if not nodes:
                return
            rest = [m for m in self.recent_buffer
                    if "node_id" not in (getattr(m, "metadata", None) or {})]
            nodes.sort(key=lambda m: (getattr(m, "metadata", None) or {}).get("node_start", 0))
            self.recent_buffer = nodes + rest

    def _emit_tool_events(self, m) -> None:
        """从新增消息中提取工具调用/结果，推送前端工具卡片事件。"""
        mtype = getattr(m, "type", None)
        if mtype == "ai" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                name = tc.get("name", "?")
                if name == "continue_speaking":
                    continue   # 分段工具不展示卡片（视觉上就是连续说话）
                try:
                    args = tc.get("args", {})
                except Exception:
                    args = {}
                self.push_event({"type": "tool_call", "id": tc.get("id", ""),
                                 "name": name, "args": args})
        elif mtype == "tool":
            if getattr(m, "name", "") == "continue_speaking":
                return   # 对应卡片不展示
            self.push_event({"type": "tool_result", "id": getattr(m, "tool_call_id", ""),
                             "content": str(getattr(m, "content", "") or "")})

    # ── 对外状态 ──────────────────────────────────────────────

    # ── 上下文统计（token 估算）────────────────────────────────

    def context_stats(self) -> dict:
        """当前上下文的长度估算（无 tokenizer，按中文字符/token 比例粗估）。

        范围：system prompt + 对话 buffer（含压缩节点消息）。
        比例：对话以中文为主，1 个汉字 ≈ 1~1.5 token（含标点/英文混合取 1.2），
        tokens = chars × 1.2（字符数 ≤ token 数，估算只会偏保守）。
        """
        chars = len(self.system_prompt or "")
        with self.buffer_lock:
            for m in self.recent_buffer:
                content = getattr(m, "content", "") or ""
                if isinstance(content, str):
                    chars += len(content)
            msgs = len(self.recent_buffer)
        return {"messages": msgs, "chars": chars, "tokens": int(chars * 1.2)}

    def state_dict(self) -> dict:
        with self.buffer_lock:
            s = self.db.summary()
            return {
                "mode": self.mode,
                "thinking": self.thinking,
                "heartbeat": self.heartbeat_dict(),
                "dnd": {"enabled": self.dnd_enabled, "in_dnd": self.in_dnd(),
                        "until": self.dnd_until.isoformat() if self.dnd_until else None},
                "plan": {
                    "today": s["today"],
                    "tasks": s["tasks"],
                    "pending_total": s["pending_total"],
                    "pending_done": s["pending_done"],
                    "overdue_count": len(s["overdue_tasks"]),
                },
                "context": self.context_stats(),
                "activity": self._activity,
            }
