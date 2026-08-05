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
        self._activity: str = ""
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

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

    def heartbeat_dict(self) -> dict:
        with self.buffer_lock:
            if self._next_heartbeat_at <= 0:
                return {"in_minutes": 0, "note": ""}
            in_minutes = max(0, int((self._next_heartbeat_at - time.time()) / 60) + 1)
            return {"in_minutes": in_minutes, "note": self._heartbeat_note}

    # ── 计划快照 ──────────────────────────────────────────────

    def _plan_snapshot_text(self) -> str | None:
        """计划快照指纹变化时生成注入文本（None = 无变化）。"""
        try:
            s = self.db.summary()
            fingerprint = json.dumps({
                "t": s["tasks"], "d": s["today_plan_done"], "n": s["today_plan_total"],
                "overdue": [p["id"] for p in s["overdue"]],
            }, ensure_ascii=False, sort_keys=True)
            if fingerprint == self._last_plan_fingerprint:
                return None
            self._last_plan_fingerprint = fingerprint
            lines = [f"[当前计划]（{s['today']}，{_now().strftime('%H:%M')}）"]
            tasks = [t for t in self.db.list_tasks() if t["status"] in ("todo", "in_progress")]
            if tasks:
                for t in tasks[:5]:
                    lines.append(f"- #{t['id']}「{t['title']}」[{t['status']}] 截止 {t['due_date'] or '未定'}（{t['plan_done']}/{t['plan_total']}）")
            if s["today_plan_undone"]:
                lines.append(f"今日计划（{s['today_plan_done']}/{s['today_plan_total']}）待做：")
                for p in s["today_plan_undone"][:8]:
                    lines.append(f"  · #{p['id']} {p['content']}")
            elif s["today_plan_total"]:
                lines.append("今日计划已全部完成。")
            if s["overdue"]:
                lines.append("逾期未做：" + "；".join(f"#{p['id']} {p['content']}" for p in s["overdue"][:5]))
            if not tasks and not s["today_plan_total"]:
                lines.append("（目前没有待办任务，可以问问用户最近想做什么。）")
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

    def _receive(self, content: str, *, trigger: bool = True) -> None:
        """接收一条外部消息。_generating 期间入队 _inbox，结束后再写入。"""
        msg = HumanMessage(content=content)
        with self.buffer_lock:
            if self._generating:
                self._inbox.append(msg)
                if trigger:
                    self.pending_response = True
                return
            self.recent_buffer.append(msg)
            self._msg_counter += 1
            if trigger:
                self.pending_response = True

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
                messages_to_dict(self.recent_buffer), self._msg_counter, self.round)
        except Exception as exc:
            _logger.warning("[session] 保存 buffer 状态失败: %s", exc)

    def _load_buffer_state(self) -> bool:
        """从 memory_tree.db 恢复消息列表。"""
        try:
            from langchain_core.messages import messages_from_dict
            state = self.get_memory_tree().load_buffer_state()
            if not state or not state["recent_buffer"]:
                return False
            msgs = messages_from_dict(state["recent_buffer"])
            if msgs:
                self.recent_buffer = msgs
                self._msg_counter = state.get("_msg_counter", len(msgs))
                self.round = state.get("round", 0)
                _logger.info("[session] 从 buffer_state 恢复上下文: %d 条消息", len(msgs))
                return True
            return False
        except Exception as exc:
            _logger.warning("[session] 加载 buffer 状态失败: %s", exc)
            return False

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
                self._fire_scheduled(f"[早晨] 早上好。现在是 {now.strftime('%H:%M')}，新的一天开始了。")
            if now.hour == _config.PLANNER_EVENING_HOUR and last_evening != today:
                last_evening = today
                self._fire_scheduled(f"[晚间] 现在是 {now.strftime('%H:%M')}，今天快结束了。")
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
                self.schedule_heartbeat(FALLBACK_HEARTBEAT_MINUTES, note)
                _logger.info("[heartbeat] 免打扰时段，顺延")
                return
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
        """计划到期未完成超过 1 小时的逾期检查（每天至多提醒一次，去重用集合）。"""
        if self.in_dnd():
            return
        try:
            overdue = self.db.list_pending_before(_now().strftime("%Y-%m-%d"))
        except Exception:
            return
        pending = [p for p in overdue if p["id"] not in getattr(self, "_reminded_overdue", set())]
        if not pending:
            return
        if not hasattr(self, "_reminded_overdue"):
            self._reminded_overdue = set()
        if len(pending) > 3:
            pending = pending[:3]
        for p in pending:
            self._reminded_overdue.add(p["id"])
        with self.chat_lock:
            with self.buffer_lock:
                if self._generating:
                    return
            text = ("[提醒] 你注意到有几条计划已经逾期还没做："
                    + "；".join(f"#{p['id']} {p['date']}「{p['content']}」" for p in pending)
                    + "。")
            self._receive(text, trigger=True)
            self._spawn_worker("scheduled")

    # ── 玩家消息 ──────────────────────────────────────────────

    def enqueue_player_message(self, message: str) -> bool:
        """注入玩家消息并立即触发回复生成。"""
        now = _now()
        self._receive(f"[{now.strftime('%H:%M')}] {PLAYER_NAME}对你说：{message}", trigger=True)
        self._save_log("user", message)
        self._wake_event.set()
        threading.Thread(target=self._player_worker, name="planner-player", daemon=True).start()
        return True

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
            self.current_trigger = trigger
        self._set_thinking(True)
        try:
            self._run_agent()
        finally:
            self._set_thinking(False)
            with self.buffer_lock:
                self._generating = False
                self.current_trigger = "player"
                while self._inbox:
                    m = self._inbox.pop(0)
                    self.recent_buffer.append(m)
                    self._msg_counter += 1
                    self.pending_response = True
            self._save_buffer_state()

    def _run_agent(self) -> None:
        """agent.invoke：model ↔ tools 循环直到停止（无工具调用 / heartbeat / 玩家让位）。"""
        self._repair_buffer()
        with self.buffer_lock:
            input_msgs = list(self.recent_buffer)
        try:
            result = self._agent.invoke(
                {"messages": input_msgs, "model_call_count": 0, "set_heartbeat_called": False},
            )
        except Exception as exc:
            _logger.exception("[agent] invoke 异常")
            self.push_log(f"小助走神了一下（生成出错：{exc}）")
            self.schedule_heartbeat(FALLBACK_HEARTBEAT_MINUTES)
            return
        with self.buffer_lock:
            new_msgs = list(result.get("messages") or [])
            self.recent_buffer = new_msgs
        # 兜底心跳：LLM 没调 heartbeat
        if not result.get("set_heartbeat_called"):
            _logger.info("[agent] 未调用 heartbeat，兜底 %d 分钟", FALLBACK_HEARTBEAT_MINUTES)
            self.schedule_heartbeat(FALLBACK_HEARTBEAT_MINUTES)
        self.push_plan_update()

    # ── 对外状态 ──────────────────────────────────────────────

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
                    "today_plan_total": s["today_plan_total"],
                    "today_plan_done": s["today_plan_done"],
                    "overdue_count": len(s["overdue"]),
                },
                "activity": self._activity,
            }
