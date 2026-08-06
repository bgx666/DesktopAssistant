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
from .llm import MockChatModel, build_chat_model
from .memory.sqlite_memory_tree import SQLiteMemoryTree
from .store.tasks_db import TasksDb

_logger = logging.getLogger("planner.session")

CHARACTER_ID = "assistant"
DISPLAY_NAME = "小助"
PLAYER_NAME = "用户"
BACKEND_TAG = "planner/1"

_TZ = timezone(timedelta(hours=8))
FALLBACK_HEARTBEAT_MINUTES = _config.PLANNER_FALLBACK_MINUTES
MAX_WORKER_ROUNDS = 3            # 生成期间到达的新消息最多再补 N 轮
DEFAULT_WAKE_MINUTES = 30        # 首次启动/未调度时的默认唤醒间隔

# 心跳节奏自适应：
# - 对话中（用户刚说话/在聊）→ 短心跳，随时跟进
# - 用户沉默（自主唤醒多次没人理）→ 每次心跳逐渐加长，避免烦人
DIALOG_HEARTBEAT_MINUTES = 10    # 对话默认心跳（用户刚说话后）
SILENT_ESCALATE_STEP = 10        # 沉默时每次心跳加长的分钟数
SILENT_ESCALATE_MAX = 120        # 沉默加长上限


def _now() -> datetime:
    return datetime.now(_TZ)


class PlannerSession:
    """小助的会话状态：任务库、对话 buffer、事件队列、调度线程、免打扰、记忆树。"""

    def __init__(self, data_root: Path | None = None, mock: bool | None = None) -> None:
        self.data_root = Path(data_root) if data_root else _config.data_root()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.mock = _config.PLANNER_MOCK_LLM if mock is None else mock
        self.mode: str = "mock" if self.mock else "llm"

        # 存储
        self.db = TasksDb(self.data_root / "planner.db")
        self.memory_tree: SQLiteMemoryTree | None = None

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
        self.current_trigger: str = "player"   # player | heartbeat | scheduled

        # 对外状态
        self.thinking: bool = False

        # 事件队列（/dequeue drain）
        self._events: list[dict] = []
        self._events_lock = threading.Lock()

        # 心跳调度（分钟级）
        self._next_heartbeat_at: float = 0.0
        self._heartbeat_minutes: int = 0
        self._heartbeat_note: str = ""
        self._heartbeat_silent_count: int = 0   # 连续自主唤醒用户没说话的次数（沉默递进）
        self._activity: str = ""
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

        # 最后活动时间（epoch 秒，持久化）：程序关闭期间的离线时长据此补回归问候
        self._last_activity_at: float = 0.0
        # 已压缩进记忆树的消息累计条数（持久化）：压缩节点 round_range 用
        # 全局序号（小B _span 机制的对齐），多次压缩范围连续不重叠
        self._compressed_total: int = 0
        # 停止请求（用户点"停止"打断当前生成）：after_model/before_model
        # 中间件检查后跳转 end；每次生成开始时重置
        self._stop_requested: bool = False

        # 免打扰
        self.dnd_enabled: bool = True
        self.dnd_until: datetime | None = None      # 一次性免打扰截止时间

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
        self._maybe_welcome_back()

    # ── 懒加载 ────────────────────────────────────────────────

    def _get_llm(self):
        if self._llm is None:
            if self.mock:
                self._llm = MockChatModel(session=self)
            else:
                self._llm = build_chat_model()
        return self._llm

    def _get_summary_model(self):
        """压缩用独立模型（与主对话同配置，服务端 prompt caching 命中）。"""
        if self._summary_model is None:
            if self.mock:
                self._summary_model = MockChatModel()
            else:
                self._summary_model = build_chat_model()
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
        """优雅关闭：停调度 → 等生成中的 worker 结束 → 落盘 → 关库。"""
        self.stop_heartbeat()
        try:
            with self.chat_lock:  # 等待在途生成完成（worker 持锁期间）
                pass
        except Exception:
            pass
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
        with self._events_lock:
            self._events.append(ev)

    def push_text(self, content: str) -> None:
        self.push_event({"type": "text", "content": content, "from": CHARACTER_ID})

    def push_log(self, text: str) -> None:
        self.push_event({"type": "log", "text": text})

    def push_plan_update(self) -> None:
        self.push_event({"type": "plan_update", "date": _now().strftime("%Y-%m-%d")})

    def drain_events(self) -> list[dict]:
        with self._events_lock:
            events = self._events
            self._events = []
            return events

    def _set_thinking(self, value: bool) -> None:
        self.thinking = value
        self.push_event({"type": "thinking", "value": value})

    # ── 免打扰 ────────────────────────────────────────────────

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

    def set_heartbeat_state(self, minutes: int, note: str = "") -> None:
        minutes = max(_config.PLANNER_HEARTBEAT_MIN_MINUTES,
                      min(_config.PLANNER_HEARTBEAT_MAX_MINUTES, int(minutes)))
        with self.buffer_lock:
            self._heartbeat_minutes = minutes
            self._heartbeat_note = note
            self._next_heartbeat_at = time.time() + minutes * 60
        self._wake_event.set()
        _logger.info("[heartbeat] 调度: %d 分钟后（%s）", minutes, note)

    def schedule_heartbeat(self, minutes: int, note: str = "") -> None:
        self.set_heartbeat_state(minutes, note)

    def _cancel_heartbeat(self) -> None:
        """取消挂起的心跳（玩家说话时重置旧的长时间心跳用）。"""
        with self.buffer_lock:
            self._next_heartbeat_at = 0.0
        self._wake_event.set()

    def _next_silent_minutes(self) -> int:
        """按沉默次数计算下次心跳分钟数：对话 10 → 沉默逐步加长 → 上限 120。"""
        if self._heartbeat_silent_count <= 0:
            return DIALOG_HEARTBEAT_MINUTES
        return min(SILENT_ESCALATE_MAX,
                   DIALOG_HEARTBEAT_MINUTES + self._heartbeat_silent_count * SILENT_ESCALATE_STEP)

    def heartbeat_dict(self) -> dict:
        with self.buffer_lock:
            if self._next_heartbeat_at <= 0:
                return {"in_minutes": 0, "note": ""}
            in_minutes = max(0, int((self._next_heartbeat_at - time.time()) / 60) + 1)
            return {"in_minutes": in_minutes, "note": self._heartbeat_note}

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
        """
        msg = HumanMessage(content=content, id=uuid.uuid4().hex)
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

    def _save_buffer_state(self) -> None:
        """buffer 持久化（messages_to_dict → json，存进 memory_tree.db）。"""
        try:
            from langchain_core.messages import messages_to_dict
            self.get_memory_tree().save_buffer_state(
                messages_to_dict(self.recent_buffer), self._msg_counter, self.round,
                last_activity_at=self._last_activity_at or None,
                reminded_overdue=list(getattr(self, "_reminded_overdue", set())),
                compressed_total=self._compressed_total,
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
                return False
            msgs = messages_from_dict(state["recent_buffer"])
            if msgs:
                self.recent_buffer = msgs
                self._msg_counter = state.get("_msg_counter", len(msgs))
                self.round = state.get("round", 0)
                self._last_activity_at = state.get("last_activity_at", 0.0) or 0.0
                reminded = state.get("reminded_overdue", [])
                if reminded:
                    self._reminded_overdue = set(reminded)
                self._compressed_total = int(state.get("compressed_total", 0) or 0)
                _logger.info("[session] 从 buffer_state 恢复上下文: %d 条消息", len(msgs))
                return True
            return False
        except Exception as exc:
            _logger.warning("[session] 加载 buffer 状态失败: %s", exc)
            return False

    def _maybe_welcome_back(self) -> None:
        """重启回归问候：程序关闭期间离线超过阈值，启动后补一次自主生成。

        任何生成结束都会刷新 _last_activity_at——程序开着时由玩家消息/心跳
        持续刷新；关闭期间不刷新，重启时差值即真实离线时长。DND 时跳过，
        交给之后的正常心跳接管。
        """
        if self._last_activity_at <= 0:
            return
        gap_minutes = (time.time() - self._last_activity_at) / 60
        if gap_minutes < _config.PLANNER_WELCOME_BACK_MINUTES:
            return
        if self.in_dnd():
            _logger.info("[welcome_back] 免打扰时段，跳过回归问候")
            return
        hours = round(gap_minutes / 60, 1)
        _logger.info("[welcome_back] 距上次活动 %.1f 分钟，补一次回归问候", gap_minutes)
        text = (f"（距离上次你见到我已经过去了 {hours} 小时。你醒了过来，"
                f"先看看任务进度和计划，提醒用户最重要的事，说点关心的、有用的话。）")
        self._receive(text, trigger=True)
        self._spawn_worker("welcome_back")

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
        """调度线程主循环：heartbeat 到点 + 定时触发点（早晨/晚间/逾期）。"""
        last_morning = ""
        last_evening = ""
        while not self._stop_event.is_set():
            now = _now()
            # 定时触发点（每天一次，跨天重置）
            today = now.strftime("%Y-%m-%d")
            if now.hour == _config.PLANNER_MORNING_HOUR and last_morning != today:
                last_morning = today
                self._fire_scheduled(f"[早晨] 早上好。现在是 {now.strftime('%H:%M')}，新的一天开始了。看看待办队列，安排一下接下来做什么。")
            if now.hour == _config.PLANNER_EVENING_HOUR and last_evening != today:
                last_evening = today
                self._fire_scheduled(f"[晚间] 现在是 {now.strftime('%H:%M')}，回顾一下今天做了什么，没做的提醒用户，调整接下来的安排。")
            # 逾期检查（每 10 分钟一次，避免重复轰炸）
            if now.minute % 10 == 0:
                self._check_overdue()
            # 心跳到点
            with self.buffer_lock:
                next_at = self._next_heartbeat_at
                phase_ok = True
            if next_at > 0 and time.time() >= next_at:
                self._fire_heartbeat()
            self._wake_event.wait(timeout=5)
            self._wake_event.clear()

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
                self.schedule_heartbeat(FALLBACK_HEARTBEAT_MINUTES, note)
                _logger.info("[heartbeat] 免打扰时段，顺延")
                return
            # 自主唤醒 = 用户沉默一次 → 心跳逐步加长
            self._heartbeat_silent_count += 1
            _logger.info("[heartbeat] 触发自主生成（距上次 %d 分钟：%s）", minutes, note)
            text = (f"（{minutes} 分钟过去了。你醒了过来。{note + '。' if note else ''}"
                    f"可以看看用户的任务进度，决定要不要提醒或调整安排。）")
            self._receive(text, trigger=True)
            self._spawn_worker("heartbeat")

    def _fire_scheduled(self, text: str) -> None:
        if self.in_dnd():
            return
        with self.chat_lock:
            with self.buffer_lock:
                if self._generating:
                    return
            _logger.info("[scheduled] 定时触发: %s", text[:40])
            self._receive(text, trigger=True)
            self._spawn_worker("scheduled")

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
            text = ("[提醒] 你注意到有几个任务已经逾期还没完成："
                    + "；".join(f"#{t['id']}「{t['title']}」截止 {t['due_date']}" for t in pending)
                    + "。")
            self._receive(text, trigger=True)
            self._spawn_worker("scheduled")

    # ── 玩家消息 ──────────────────────────────────────────────

    def enqueue_player_message(self, message: str) -> str:
        """注入玩家消息并立即触发回复生成。返回该消息的 id（撤销按钮用）。

        用户说话 = 活跃状态：重置挂起的旧心跳（避免"1 小时后才醒来"的残留），
        设对话默认短心跳并清零沉默计数；生成中 LLM 会再调 heartbeat 覆盖。
        """
        now = _now()
        self._cancel_heartbeat()
        self._heartbeat_silent_count = 0
        self.schedule_heartbeat(DIALOG_HEARTBEAT_MINUTES)
        msg = self._receive(
            f"[{now.strftime('%H:%M')}] {PLAYER_NAME}对你说：{message}", trigger=True)
        self._save_log("user", message)
        self._wake_event.set()
        threading.Thread(target=self._player_worker, name="planner-player", daemon=True).start()
        return getattr(msg, "id", None) or ""

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
        # 撤销 = 用户介入 = 活跃：重置心跳，保存状态，通知前端
        self._cancel_heartbeat()
        self._heartbeat_silent_count = 0
        self.schedule_heartbeat(DIALOG_HEARTBEAT_MINUTES)
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
        - values：完整 state（removes 已生效）→ 最终 messages 回写 buffer、
          set_heartbeat_called 兜底判断；每轮模型文本收束时 push 完整 text（气泡 toast 用）
        """
        from langchain_core.messages import AIMessage, AIMessageChunk

        self._repair_buffer()
        with self.buffer_lock:
            input_msgs = list(self.recent_buffer)
        final_messages = None
        heartbeat_called = False
        current_msg_id = None
        current_chunks = []
        # 已见消息 id（而非 seen_count=len(msgs)）：压缩中间件会 RemoveMessage
        # 收缩消息列表，按长度增量会漏掉压缩后新增的 ToolMessage → 工具卡片
        # 永远停在"正在执行"转圈。按消息 id 去重，与列表收缩无关。
        seen_msg_ids = {getattr(m, "id", None) or id(m) for m in input_msgs}
        try:
            stream = self._agent.stream(
                {"messages": input_msgs, "model_call_count": 0, "set_heartbeat_called": False},
                stream_mode=["messages", "values"],
            )
            for item in stream:
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
                    if data.get("set_heartbeat_called"):
                        heartbeat_called = True
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
                        self.push_text(full_text)
                        self._save_log("assistant", full_text)
                        current_chunks = []
                        current_msg_id = None
        except Exception as exc:
            _logger.exception("[agent] stream 异常")
            self.push_log(f"小助走神了一下（生成出错：{exc}）")
            self.schedule_heartbeat(self._next_silent_minutes())
            return
        if final_messages:
            with self.buffer_lock:
                self.recent_buffer = list(final_messages)
            self._reorder_node_messages()
        # 兜底心跳：LLM 没调 heartbeat → 按对话/沉默节奏自适应
        if not heartbeat_called:
            minutes = self._next_silent_minutes()
            _logger.info("[agent] 未调用 heartbeat，兜底 %d 分钟", minutes)
            self.schedule_heartbeat(minutes)
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
                if name == "heartbeat":
                    continue   # 心跳收尾不算工具动作，不展示
                try:
                    args = tc.get("args", {})
                except Exception:
                    args = {}
                self.push_event({"type": "tool_call", "id": tc.get("id", ""),
                                 "name": name, "args": args})
        elif mtype == "tool":
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
