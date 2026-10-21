"""PlannerSession —— 学习工作助手会话（并发/调度/事件/heartbeat/记忆树）。

移植自 yaya backend session.py：
- buffer + chat_lock / buffer_lock(RLock) / _inbox 并发模式；
- 后端自建调度线程（daemon）：LLM 自主 heartbeat 唤醒 + 定时触发点（早晨/晚间/逾期）；
- 事件队列（/dequeue drain）+ 流式文本推送（StreamTextMiddleware）；
- 记忆树 + buffer 持久化（SQLiteMemoryTree.save_buffer_state，重启恢复）。

planner 特有：
- 日历是唯一的待办概念（模型需要时用 list_schedule 查，不再每轮注入议程快照）；
- 心跳 = 「下一次主动唤醒」的定时器，clamp 到 [1 分钟, 720 分钟]。
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
from .llm import (
    MockChatModel,
    build_chat_model,
    is_vision_model,
    resolve_llm_config,
    resolve_model_name,
)
from .memory.sqlite_memory_tree import SQLiteMemoryTree
from .middleware import (
    SUMMARIZE_KEEP_MESSAGES as _SUMMARIZE_KEEP,
    SUMMARIZE_TOKEN_BUDGET as _SUMMARIZE_TOKEN_BUDGET,
)
from .store.calendar_db import CalendarDb
from .tts import TtsClient

_logger = logging.getLogger("planner.session")

CHARACTER_ID = "assistant"
DISPLAY_NAME = "小助"
PLAYER_NAME = "用户"
BACKEND_TAG = "planner/1"

_TZ = timezone(timedelta(hours=8))
FALLBACK_HEARTBEAT_MINUTES = _config.PLANNER_FALLBACK_MINUTES
MAX_WORKER_ROUNDS = 3            # 生成期间到达的新消息最多再补 N 轮
STARTUP_GRACE_SECONDS = 120      # 启动宽限期：打开后 2 分钟内不触发定时提醒

# ── 心跳（用户授权的定时器，2026-10-19 起）────────────────────
# 心跳**不再是自主搭话/自学习的节律**——只有用户明确要求"某个时间/间隔后做某事、
# 盯住某个任务"时才由模型通过 heartbeat 工具设置；普通回合结束不排心跳，
# 系统也**不做任何自动排程**（启动不排、醒来不保底重复、出错不重试）。
#   · 巡检循环：醒来 → 按用户嘱托检查 → 完成则收尾（不设下一次）→ 未完成则
#     再设下一次（note 写明原因）；
#   · 定时提醒（钟点）仍走日历（add_schedule），到点系统自己叫，不占心跳槽位；
#   · 一轮内的「调工具 → 观察 → 再调工具」由原生 ReAct 循环完成，与心跳无关。
# 醒来提示里给出「现在几点 + 距用户上次说话多久」，供模型判断任务是否该做。

# 调度线程轮询间隔：按待触发心跳的剩余时间自适应（见 _schedule_tick_seconds）。
SCHEDULER_IDLE_TICK_SECONDS = 5.0      # 无待触发心跳时
SCHEDULER_BUSY_TICK_SECONDS = 0.2      # 心跳已到期但被生成阻塞（防空转）


def _now() -> datetime:
    return datetime.now(_TZ)


def image_url(path: str) -> str:
    """图片展示 URL（`GET /file?path=`；路径整体 urlencode，防特殊字符/注入）。"""
    from urllib.parse import quote
    return "/file?path=" + quote(str(path), safe="")


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
    """小助的会话状态：任务库、对话 buffer、事件队列、调度线程、记忆树。"""

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
        self.db = CalendarDb(self.data_root / "planner.db")
        self.memory_tree: SQLiteMemoryTree | None = None

        # 代码工作区（settings.workspace_root；空 = 代码能力关闭）与审批挂起
        self.workspace_root: str = str(self.settings.get("workspace_root") or "").strip()
        self.pending_approvals: dict[str, dict] = {}
        self.pending_questions: dict[str, dict] = {}   # ask_user 结构化提问挂起

        # LLM 用量/缓存命中统计（最近 50 轮生成；设置页/状态条展示）
        from .llm_stats import LlmStats
        self.llm_stats = LlmStats(self.data_root)
        self._last_prompt_tokens: int = 0   # 上一轮 API 实报输入 token（标题栏显示用）

        # 写操作留底的待关联队列（tools._record_change 入队，_emit_tool_events 消费）：
        # 把 change id 附到对应 tool_result（前端显示修改前后 diff、/history 重建）
        self._pending_changes: list[dict] = []
        self._turn_change_ids: list[int] = []   # 本轮回复改动过的文件（快捷块用）
        # show_image 的待关联队列（tools.show_image 入队）：图片事件 + 历史重建
        self._pending_images: list[dict] = []

        # 语音合成（本地 Kokoro / 小米 MiMo / DashScope，按 settings + env 创建）
        self.tts = self._create_tts()

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
        # 正在执行的 run_command 子进程（停止/关闭时杀整棵进程树，防留孤儿）
        self._run_proc = None
        # 待注入的屏幕截图（capture_screen 工具写入 data URL，ScreenShotInjectMiddleware
        # 在下次模型调用前作为 user 消息注入并清空；生成开始前清残留防陈旧注入）
        self._pending_screenshots: list[dict] = []   # 待注入图片（截屏/读图：{url, text, viewed?}）
        # 异步压缩：达到阈值后由后台线程压缩，空闲时原子替换 buffer
        self._compressing: bool = False
        self._closing: bool = False   # close 信号：压缩线程尽快应用退出

        # 懒加载
        self._llm = None
        self._summary_model = None
        self._agent_obj = None
        self._system_prompt: str = ""
        self._changes_db = None   # 文件变更历史（代码工作区写操作留底）

        self._system_prompt = self._build_system_prompt()
        self.get_memory_tree()          # 确保 db 文件落盘
        self._load_buffer_state()
        # 断点续聊：恢复的排队消息（上次生成中被强杀时排队的）立即续跑
        # （用循环 worker：它会依次处理队列里的每一条）
        if self._inbox:
            _logger.info("[session] 续跑恢复的排队消息: %d 条", len(self._inbox))
            threading.Thread(target=self._player_worker,
                             name="planner-player", daemon=True).start()
        self._check_startup_heartbeat()

    # ── 懒加载 ────────────────────────────────────────────────

    def _create_tts(self) -> TtsClient:
        """按当前 settings + 环境变量创建 TTS 客户端。

        settings.tts_engine 留空时跟随 PLANNER_TTS_ENGINE；
        保存设置切换引擎后调用本方法重建，立即生效。
        """
        engine = (self.settings.get("tts_engine") or _config.PLANNER_TTS_ENGINE).strip().lower()
        if engine == "mimo":
            tts = TtsClient(
                self.data_root,
                engine="mimo",
                api_key=_config.PLANNER_MIMO_API_KEY,
                model=_config.PLANNER_MIMO_MODEL,
                voice=_config.PLANNER_MIMO_VOICE,
                base_url=_config.PLANNER_MIMO_BASE_URL,
            )
        else:
            tts = TtsClient(
                self.data_root,
                engine=engine,
                api_key=_config.PLANNER_TTS_API_KEY,
                model=_config.PLANNER_TTS_MODEL,
                voice=_config.PLANNER_TTS_VOICE if engine == "cloud" else "zf_001",
            )
        # 应用持久化音色/开关（与保存时一致）
        voice = self.settings.get("tts_voice")
        if voice and voice in {v["id"] for v in tts.list_voices()}:
            tts.voice = voice
        if "tts_enabled" in self.settings and tts._engine_ok:
            tts._enabled = bool(self.settings["tts_enabled"])
        return tts

    def llm_session_id(self) -> str:
        """LLM 会话稳定 ID（OpenCode Go 的 `x-opencode-session` 头；首次使用生成并持久化）。"""
        sid = str(self.settings.get("llm_session_id") or "").strip()
        if sid:
            return sid
        sid = uuid.uuid4().hex
        try:
            from .settings import save_settings
            self.settings = save_settings(self.data_root, {"llm_session_id": sid})
            _logger.info("[llm] 已生成会话 ID（OpenCode Go 路由/缓存用）")
        except ValueError:
            pass
        return sid

    def _get_llm(self):
        if self._llm is None:
            if self.mock:
                self._llm = MockChatModel(session=self)
            else:
                s = self.settings
                cfg = resolve_llm_config(s)
                self._llm = build_chat_model(
                    api_key=cfg["api_key"] or None,
                    base_url=cfg["base_url"] or None,
                    model_name=cfg["model"] or None,
                    # 思考开关与强度（设置页可改，见 settings.py 的 _ENUMS）
                    thinking_mode=s.get("thinking_mode"),
                    reasoning_effort=s.get("reasoning_effort"),
                    provider=cfg["provider"],
                    session_id=self.llm_session_id(),
                )
        return self._llm

    def _get_summary_model(self):
        """压缩用独立模型（与主会话同一份配置）；max_tokens 按节点预算留余量。

        节点预算 node_token_budget（默认 1000，整节点目标）→ max_tokens 设 3 倍，
        避免 JSON 被硬截断（超出预算的软裁剪由 middleware._apply_budget 兜底；
        预算本身也会写进压缩指令作为总字数上限，模型不是蒙着写）。
        """
        if self._summary_model is None:
            if self.mock:
                self._summary_model = MockChatModel()
            else:
                s = self.settings
                cfg = resolve_llm_config(s)
                budget = int(s.get("node_token_budget", 1000) or 1000)
                self._summary_model = build_chat_model(
                    api_key=cfg["api_key"] or None,
                    base_url=cfg["base_url"] or None,
                    model_name=cfg["model"] or None,
                    # 压缩是后台提取任务：**显式关思考**——开思考时推理 token 会吃掉
                    # max_tokens 预算、返回空内容 → 摘要为空 → 压缩空转
                    # （2026-10-18 实测：thinking 开启时每次触发、每次空转）
                    thinking_mode="disabled",
                    reasoning_effort=s.get("reasoning_effort"),
                    # 3× 余量：任务型 JSON（6 字段）比纯文本长得多，1.5× 会被
                    # max_tokens 截断 → JSON 解析失败（2026-10-18 实测）
                    max_tokens=int(budget * 3),
                    provider=cfg["provider"],
                    session_id=self.llm_session_id(),
                )
        return self._summary_model

    @property
    def _agent(self):
        if self._agent_obj is None:
            from .agent import build_planner_agent
            self._agent_obj = build_planner_agent(self)
        return self._agent_obj

    @property
    def vision_capable(self) -> bool:
        """当前模型是否具备图像输入能力（mock 或纯文本模型 → False）。

        解析链与 build_chat_model 一致（resolve_llm_config：分服务商模型 >
        旧字段 > env > 预设默认）。视觉注入只在视觉模型下启用。
        """
        if self.mock:
            return False
        model = resolve_llm_config(self.settings)["model"]
        return is_vision_model(model)

    def set_chat_model(self, model) -> None:
        """测试桩注入：替换 chat model 并重建 agent。"""
        self._llm = model
        self._agent_obj = None

    def update_settings(self, updates: dict) -> dict:
        """保存并应用设置（应用即生效）：
        - 压缩参数：下次压缩时读取新值（运行时读 settings）
        - LLM 配置：重建模型实例（下次生成生效）
        - 工作区 / 当前目录 / 权限模式：重建提示词与 agent（切换副作用见下）
        校验失败抛 ValueError。
        """
        from .settings import save_settings
        from .workspace import PermissionStore, latest_recent
        updates = dict(updates or {})
        old_llm = resolve_llm_config(self.settings)   # 分服务商：provider/base_url/key/model
        old_workspace = str(self.settings.get("workspace_root") or "").strip()
        old_cwd = str(self.settings.get("workspace_cwd") or "").strip()
        old_mode = str(self.settings.get("permission_mode") or "ask").strip().lower()
        old_budget = int(self.settings.get("node_token_budget", 1000) or 1000)
        old_call_limit = int(self.settings.get("model_call_limit", 100) or 100)
        # 工作区**实际变化**时 → 当前目录归零（调用方想保留就显式带上 workspace_cwd）
        if ("workspace_root" in updates and "workspace_cwd" not in updates
                and str(updates.get("workspace_root") or "").strip() != old_workspace):
            updates["workspace_cwd"] = ""
        # 最近目录：切换工作区 / 工作目录时置顶记录（≤8 去重）
        touched = []
        if "workspace_root" in updates:
            touched.append(str(updates.get("workspace_root") or "").strip())
        if "workspace_cwd" in updates:
            touched.append(str(updates.get("workspace_cwd") or "").strip())
        if any(touched):
            recent = list(self.settings.get("workspace_recent") or [])
            for t in touched:
                if t:
                    recent = latest_recent(recent, t)
            updates["workspace_recent"] = recent
        self.settings = save_settings(self.data_root, updates)
        new_llm = resolve_llm_config(self.settings)
        if old_llm != new_llm:
            self._llm = None
            self._summary_model = None
            self._agent_obj = None
            _logger.info("[settings] LLM 配置变更（provider=%s model=%s），已重建模型",
                         new_llm.get("provider"), new_llm.get("model"))
        if int(self.settings.get("node_token_budget", 1000) or 1000) != old_budget:
            self._summary_model = None      # 节点预算变了 → 重建压缩模型（max_tokens 变）
        # 单轮模型调用上限：ModelCallLimitMiddleware 在 agent 构建时读设置 → 重建
        if int(self.settings.get("model_call_limit", 100) or 100) != old_call_limit:
            self._agent_obj = None
            _logger.info("[settings] 单轮调用上限 → %s，已重建 agent",
                         self.settings.get("model_call_limit"))
        # 工作区 / 当前目录 / 权限模式变更：规则、提示词、agent 都要跟上
        new_workspace = str(self.settings.get("workspace_root") or "").strip()
        new_cwd = str(self.settings.get("workspace_cwd") or "").strip()
        new_mode = str(self.settings.get("permission_mode") or "ask").strip().lower()
        if new_workspace != old_workspace:
            PermissionStore(self.data_root).clear()
            self.workspace_root = new_workspace
            _logger.info("[settings] 工作区变更 → %s（已清空授权规则）",
                         new_workspace or "(关闭)")
        if (new_workspace != old_workspace or new_cwd != old_cwd
                or new_mode != old_mode):
            self._agent_obj = None
            self._system_prompt = self._build_system_prompt()
            self.push_event({"type": "workspace_changed", "workspace": new_workspace,
                             "cwd": new_cwd, "permission_mode": new_mode})
            _logger.info("[settings] 工作区/目录/权限模式变更 → ws=%s cwd=%s mode=%s",
                         new_workspace or "(关)", new_cwd or "(根)", new_mode)
        # TTS 设置即时生效：引擎切换则重建客户端，否则只应用音色/开关
        new_engine = (self.settings.get("tts_engine") or _config.PLANNER_TTS_ENGINE).strip().lower()
        if new_engine != self.tts.engine:
            self.tts = self._create_tts()
            _logger.info("[settings] TTS 引擎切换为 %s，已重建客户端", new_engine)
        else:
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

    def get_changes_db(self):
        """文件变更历史库（代码工作区写操作留底；懒加载）。"""
        if self._changes_db is None:
            from .changes import ChangesDb
            self._changes_db = ChangesDb(self.data_root)
        return self._changes_db

    def close(self) -> None:
        """优雅关闭：停调度 → 杀执行中的命令 → 等生成中的 worker 结束 → 等后台压缩 → 落盘 → 关库。"""
        self.stop_heartbeat()
        self.kill_run_proc()
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
        if self._changes_db is not None:
            self._changes_db.close()
        self.db.close()

    # ── system prompt ─────────────────────────────────────────

    def effective_cwd(self) -> Path | None:
        """当前工作目录（Path）；未启用工作区时 None。

        settings.workspace_cwd 无效（不存在/越界）时回退工作区根。
        """
        from .workspace import effective_cwd as _eff
        ws_raw = str(self.settings.get("workspace_root") or "").strip()
        if not ws_raw:
            return None
        try:
            ws = Path(ws_raw).expanduser().resolve()
        except OSError:
            return None
        return _eff(ws, str(self.settings.get("workspace_cwd") or ""))

    def _build_system_prompt(self) -> str:
        """角色卡 +（启用代码工作区时）工作区段 + 仓库 AGENTS.md + 当前目录内容。

        工作区段占位符：{workspace} / {cwd} / {permission_mode}；
        顶层文件树注入的是**当前工作目录**（cwd）的内容，不是工作区根。
        """
        text = (_config.PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
        ws = str(self.settings.get("workspace_root") or "").strip()
        if not ws:
            return text
        cwd = self.effective_cwd()
        cwd_str = str(cwd) if cwd is not None else ws
        mode = str(self.settings.get("permission_mode") or "ask").strip().lower()
        parts = [text.rstrip()]
        ws_file = _config.PROMPTS_DIR / "workspace.md"
        if ws_file.exists():
            section = (ws_file.read_text(encoding="utf-8")
                       .replace("{workspace}", ws)
                       .replace("{cwd}", cwd_str)
                       .replace("{permission_mode}", mode))
            parts.append(section.strip())
        # 仓库约定：工作区根 + 当前目录（不同则都读；各限 8KB）
        agents_bases: list[Path] = []
        for base in (Path(ws), cwd):
            if base is not None and base not in agents_bases:
                agents_bases.append(base)
        for base in agents_bases:
            try:
                agents_md = (base / "AGENTS.md").read_text(encoding="utf-8")
                if agents_md.strip():
                    parts.append(f"## 仓库约定（来自 {base}\\AGENTS.md）\n\n"
                                 + agents_md.strip()[:8000])
            except OSError:
                pass
        tree_base = cwd if cwd is not None else Path(ws)
        try:   # 当前目录内容（轻量：最多 40 项；细节用 glob/grep 查）
            entries = sorted((p.name + "/" if p.is_dir() else p.name)
                             for p in tree_base.iterdir())
            if entries:
                shown = entries[:40]
                more = (f"，共 {len(entries)} 项，显示前 40）"
                        if len(entries) > len(shown) else "）")
                parts.append(f"## 当前目录内容（{tree_base}{more}\n\n"
                             + "\n".join(shown))
        except OSError:
            pass
        return "\n\n".join(parts) + "\n"

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

    def push_error(self, text: str, hint: str = "") -> None:
        """把错误**明确显示到界面**（红色错误气泡 + 落日志）——静默失败是最烦人的。

        text：一两句说清发生了什么（带服务商/模型等上下文）；
        hint：怎么解决（去哪改什么）。
        """
        text = str(text or "").strip() or "出错了"
        hint = str(hint or "").strip()
        self.push_event({"type": "error", "text": text, "hint": hint})
        try:
            self._save_log("error", text + ((" | " + hint) if hint else ""))
        except Exception:
            pass

    @staticmethod
    def _short_error(exc: Exception) -> str:
        """异常 → 一行短消息（去掉多行堆栈）。"""
        raw = str(exc or "").strip()
        first = raw.splitlines()[0] if raw else exc.__class__.__name__
        return first[:220]

    @staticmethod
    def _llm_error_hint(exc: Exception) -> str:
        """按常见错误给出下一步动作。"""
        s = str(exc or "").lower()
        if "401" in s or "authentication" in s or "invalid api key" in s or "invalid_request_error" in s and "key" in s:
            return "API Key 无效或未配置：去 设置 → 模型 API 检查当前服务商的 Key。"
        if "missing x-opencode-session" in s or "missingsessionid" in s:
            return "OpenCode Go 需要会话头：请升级到最新版本的 planner（本版已自动带上）。"
        if "404" in s:
            return "模型名可能不存在：点输入框上方 🤖 从菜单里选一个可用模型。"
        if "429" in s:
            return "请求太频繁或额度用尽，稍后重试（OpenCode Go 的额度可在 Console 查看）。"
        if "timeout" in s or "timed out" in s:
            return "请求超时：检查网络/代理是否可达对应服务商。"
        if "400" in s:
            return "请求被服务商拒绝：确认服务商/模型选对（点 🤖 重选），再试一次。"
        return "可检查：服务商/模型是否选对、API Key 是否有效、网络是否可达。"

    def push_agenda_update(self) -> None:
        self.push_event({"type": "agenda_update", "date": _now().strftime("%Y-%m-%d")})

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

    # ── 审批（代码工作区：写操作 / 工作区外读）──────────────────

    def request_approval(self, tool: str, paths: list[str], preview: str = "",
                         scopes: list[dict] | None = None, cwd: str = "") -> str:
        """挂起等待用户审批；返回 allow_once / always* / deny / timeout / stop。

        等待期间每 0.5s 醒一次检查停止/关闭信号（超时默认拒绝）；
        事件 approval_request / approval_resolved 推给前端弹窗
        （preview 展示 diff/摘要，scopes 供「记住并允许」选范围，cwd 供展示）。
        """
        req_id = uuid.uuid4().hex[:12]
        ev = threading.Event()
        timeout = float(self.settings.get("approval_timeout_seconds", 120) or 120)
        entry = {"id": req_id, "tool": tool, "paths": list(paths),
                 "preview": str(preview or ""), "scopes": list(scopes or []),
                 "cwd": str(cwd or ""), "timeout": timeout,
                 "event": ev, "decision": ""}
        with self.buffer_lock:
            self.pending_approvals[req_id] = entry
        self.push_event({"type": "approval_request", "id": req_id,
                         "tool": tool, "paths": list(paths),
                         "preview": entry["preview"], "scopes": entry["scopes"],
                         "cwd": entry["cwd"], "timeout": timeout})
        deadline = time.time() + timeout
        decision = "timeout"
        while time.time() < deadline:
            if ev.wait(timeout=0.5):
                decision = str(entry.get("decision") or "deny")
                break
            if self._stop_requested or self._closing:
                decision = "stop"
                break
        with self.buffer_lock:
            self.pending_approvals.pop(req_id, None)
        self.push_event({"type": "approval_resolved", "id": req_id, "decision": decision})
        _logger.info("[approval] %s %s → %s", tool, paths, decision)
        return decision

    def resolve_approval(self, req_id: str, decision: str, scope: str = "") -> bool:
        """前端回填审批结果；返回是否命中一个挂起请求。

        decision：allow_once / always / deny；always 时 scope ∈ file/dir/workspace
        （缺省 file）；内部统一成 always_file / always_dir / always_workspace。
        """
        d = str(decision or "").strip().lower()
        if d not in ("allow_once", "always", "deny"):
            return False
        if d == "always":
            sc = str(scope or "").strip().lower()
            if sc not in ("file", "dir", "workspace", "cmd", "cmd_prefix"):
                sc = "file"
            d = f"always_{sc}"
        with self.buffer_lock:
            entry = self.pending_approvals.get(str(req_id or "").strip())
            if entry is None:
                return False
            entry["decision"] = d
            entry["event"].set()
        return True

    # ── 结构化提问（ask_user：弹窗选项 / 自由输入）────────────────

    def ask_user(self, question: str, options: list[str] | None = None) -> str:
        """挂起等待用户回答问题（弹窗）；返回给模型的工具结果文本。

        - 用户在弹窗里选一个选项（selected）或直接打字（answer），两者都记；
        - 超时（approval_timeout_seconds）→ 提示模型自行斟酌、不要反复追问；
        - 停止/关窗 → 提示按现状收尾。等待期间每 0.5s 醒一次检查停止信号。
        """
        q = str(question or "").strip()[:500]
        opts = [str(o).strip()[:80] for o in list(options or []) if str(o).strip()][:4]
        if not q:
            return "（提问失败：question 不能为空。）"
        req_id = uuid.uuid4().hex[:12]
        ev = threading.Event()
        timeout = float(self.settings.get("approval_timeout_seconds", 120) or 120)
        entry = {"id": req_id, "question": q, "options": opts,
                 "answer": "", "selected": "", "timeout": timeout,
                 "event": ev}
        with self.buffer_lock:
            self.pending_questions[req_id] = entry
        self.push_event({"type": "question_request", "id": req_id,
                         "question": q, "options": opts, "timeout": timeout})
        deadline = time.time() + timeout
        status = "timeout"
        while time.time() < deadline:
            if ev.wait(timeout=0.5):
                status = "answered"
                break
            if self._stop_requested or self._closing:
                status = "stop"
                break
        with self.buffer_lock:
            self.pending_questions.pop(req_id, None)
        answer = str(entry.get("answer") or "").strip()
        selected = str(entry.get("selected") or "").strip()
        self.push_event({"type": "question_resolved", "id": req_id,
                         "answer": answer, "selected": selected, "status": status})
        _logger.info("[ask_user] %s → %s (answer=%r)", q[:40], status, answer[:40])
        if status == "stop":
            return "（用户停止了这一轮，提问没有得到回答。就按现在的情况收尾。）"
        if status == "timeout":
            return "（用户超时没有回答。不要反复追问，按你的判断继续或先做别的。）"
        if selected and answer:
            if answer == selected:
                return f"用户选择了「{selected}」。"
            return f"用户选择了「{selected}」，补充说明：{answer}"
        if selected:
            return f"用户选择了「{selected}」。"
        return f"用户回答：{answer}"

    def resolve_question(self, req_id: str, answer: str = "",
                         selected: str = "") -> bool:
        """前端回填提问结果（选项或自由文本）；返回是否命中一个挂起请求。"""
        a = str(answer or "").strip()[:2000]
        sc = str(selected or "").strip()[:80]
        if not a and not sc:
            return False
        with self.buffer_lock:
            entry = self.pending_questions.get(str(req_id or "").strip())
            if entry is None:
                return False
            entry["answer"] = a
            entry["selected"] = sc
            entry["event"].set()
        return True

    def _set_thinking(self, value: bool) -> None:
        self.thinking = value
        self.push_event({"type": "thinking", "value": value})

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

    def cancel_heartbeat(self) -> bool:
        """取消已排定的下一次定时唤醒（用户改主意/任务提前完成）。

        返回是否确实取消了（当前没有排定 → False）。取消立即落盘（重启不会复活）。
        """
        with self.buffer_lock:
            had = self._next_heartbeat_at > 0
            self._next_heartbeat_at = 0.0
            self._heartbeat_note = ""
        if had:
            self._save_buffer_state()
            _logger.info("[heartbeat] 已取消下一次唤醒")
        self._wake_event.set()
        return had

    @staticmethod
    def _fmt_duration(minutes: float) -> str:
        """分钟数 → 可读时长（秒级显示；亚秒保留一位小数——短心跳就是 0.1 秒级）。"""
        secs = minutes * 60
        if secs < 1:
            return f"{secs:.1f} 秒"
        if secs < 60:
            return f"{int(round(secs))} 秒"
        if minutes == int(minutes):
            return f"{int(minutes)} 分钟"
        return f"{minutes:.1f} 分钟"

    def _cancel_heartbeat(self) -> None:
        """取消挂起的心跳（玩家说话时重置旧的长时间心跳用）。"""
        with self.buffer_lock:
            self._next_heartbeat_at = 0.0
        self._wake_event.set()

    def _now_desc(self) -> str:
        """当前时间的人类可读描述（带星期）——注入心跳提示，供模型判断用户状态。"""
        now = _now()
        weekday = "一二三四五六日"[now.weekday()]
        return f"{now.strftime('%Y-%m-%d %H:%M')}（周{weekday}）"

    def _silence_desc(self) -> str:
        """距上次玩家说话的描述——模型据此判断用户是在忙还是正想聊。

        没有任何计数/递进逻辑：只把时间事实交给模型，判断权在模型手里。
        """
        if self._last_player_message_at is None:
            return "用户还没有跟你说过话。"
        gap = (_now() - self._last_player_message_at).total_seconds()
        if gap < 90:
            return "用户刚刚还在跟你说话。"
        return f"距用户上次说话已经 {self._fmt_gap(gap)}。"

    def heartbeat_dict(self) -> dict:
        with self.buffer_lock:
            if self._next_heartbeat_at <= 0:
                return {"in_minutes": 0, "in_seconds": 0, "note": ""}
            in_seconds = max(0, int(self._next_heartbeat_at - time.time()))
            return {"in_seconds": in_seconds,
                    "in_minutes": max(0, int(in_seconds / 60)),
                    "note": self._heartbeat_note}

    # ── buffer 维护 ───────────────────────────────────────────

    def _append_to_buffer(self, msg) -> None:
        with self.buffer_lock:
            if isinstance(msg, dict):
                msg = HumanMessage(content=msg.get("content", ""))
            self.recent_buffer.append(msg)
            self._msg_counter += 1

    def _receive(self, content: str | list, *, trigger: bool = True) -> BaseMessage:
        """接收一条外部消息。_generating 期间入队 _inbox，结束后再写入。返回消息对象。

        content：纯文本，或内容块列表（视觉注入 [text, image_url]）。
        显式分配 id（langchain 默认 None，langgraph 在模型调用时才补）——
        撤销按钮依赖消息 id 在入队时就稳定存在。
        消息带 metadata.ts 时间戳（秒级，UTC+8）——压缩节点起止时间的数据来源。
        """
        # 新消息边界：上一段未出块的改动先统一落块（含自主回合累积的）——
        # 与 /history 在 human 边界 flush 的口径一致，避免旧改动并进新回复的块
        if self._turn_change_ids:
            files = self.changed_files_for_ids(self._turn_change_ids)
            self._turn_change_ids = []
            if files:
                self.push_event({"type": "changed_files", "files": files})
        msg = HumanMessage(content=content, id=uuid.uuid4().hex,
                           metadata={"ts": self._ts_now()})
        # 玩家消息 = 立即打断 continue 分段暂停（让位给用户）
        self.interrupt_continue_pause()
        with self.buffer_lock:
            if self._generating:
                self._inbox.append(msg)
                if trigger:
                    self.pending_response = True
            else:
                self.recent_buffer.append(msg)
                self._msg_counter += 1
                if trigger:
                    self.pending_response = True
        # 收到消息**立即落盘**（连同排队队列）：强杀/断电也不丢——2026-10-18
        # 事故教训：之前只在生成结束才保存，外部强杀把用户"最后一轮消息"带走了。
        self._save_buffer_state()
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

    def _heal_wrapped_node_summaries(self) -> None:
        """自愈：节点消息的摘要曾是 JSON 外壳（2026-10-18 降级路径事故批次）——
        原地剥壳，只动带 node_id 且 `[摘要]` 段以 `{` 开头的消息；剥不出来就保留。"""
        import re as _re

        from .middleware import clean_wrapped_summary
        pat = _re.compile(r"\[摘要\] ([\s\S]*?)(?=\n\[|$)")
        healed = 0
        with self.buffer_lock:
            for m in self.recent_buffer:
                if "node_id" not in (getattr(m, "metadata", None) or {}):
                    continue
                content = str(getattr(m, "content", "") or "")
                mm = pat.search(content)
                if not mm or not mm.group(1).lstrip().startswith("{"):
                    continue
                cleaned = clean_wrapped_summary(mm.group(1))
                if cleaned and cleaned != mm.group(1):
                    try:
                        m.content = content[:mm.start(1)] + cleaned + content[mm.end(1):]
                        healed += 1
                    except Exception:   # 消息不可变等异常：跳过
                        pass
        if healed:
            _logger.info("[repair] 已剥壳 %d 条节点消息的 JSON 摘要", healed)

    def _drop_superseded_node_messages(self) -> int:
        """清除 buffer 里"已被父节点取代"的旧节点消息（inactive 且父节点消息在场）。

        历史 bug（2026-10-19 修复）：层级合并的目标若是**本轮刚产生的分片叶子**
        （消息在 adds、不在快照里），其消息没被摘除 → inactive 的 L0 消息与父节点
        并存于 buffer（看起来范围重叠/不连续、L0 数超过阈值）。修复后不再新增；
        此方法清理旧数据。只删"父节点消息也在 buffer"的——父节点消息缺失时保留
        （宁可重复，不丢覆盖）。
        """
        with self.buffer_lock:
            ids = [(getattr(m, "metadata", None) or {}).get("node_id")
                   for m in self.recent_buffer]
            ids = [i for i in ids if i]
            if not ids:
                return 0
            try:
                status = self.get_memory_tree().get_node_status(ids)
            except Exception:
                return 0
            present = set(ids)
            drop = {nid for nid in ids
                    if not (status.get(nid) or {}).get("is_active", True)
                    and (status.get(nid) or {}).get("parent_id") in present}
            if not drop:
                return 0
            before = len(self.recent_buffer)
            self.recent_buffer = [
                m for m in self.recent_buffer
                if (getattr(m, "metadata", None) or {}).get("node_id") not in drop]
            removed = before - len(self.recent_buffer)
            if removed:
                _logger.info("[repair] 已清理 %d 条被父节点取代的旧节点消息", removed)
            return removed

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
        """buffer 持久化（messages_to_dict 存 json，落 memory_tree.db）。

        连同 **_inbox（生成中排队待处理的消息）** 一起落盘——断点续聊：
        强杀/断电也不丢用户刚发的消息（2026-10-18 事故：无差别杀进程把
        release 实例连同"最后一轮消息"一起带走，那轮对话没落盘）。
        """
        try:
            from langchain_core.messages import messages_to_dict
            last_msg_at = None
            if self._last_player_message_at is not None:
                last_msg_at = self._last_player_message_at.timestamp()
            with self.buffer_lock:
                inbox_snapshot = messages_to_dict(list(self._inbox))
            self.get_memory_tree().save_buffer_state(
                messages_to_dict(self.recent_buffer), self._msg_counter, self.round,
                last_activity_at=self._last_activity_at or None,
                reminded_overdue=list(getattr(self, "_reminded_overdue", set())),
                compressed_total=self._compressed_total,
                next_heartbeat_at=self._next_heartbeat_at or None,
                heartbeat_minutes=self._heartbeat_minutes or None,
                heartbeat_note=self._heartbeat_note or None,
                last_player_message_at=last_msg_at,
                inbox=inbox_snapshot,
            )
        except Exception as exc:
            _logger.warning("[session] 保存 buffer 状态失败: %s", exc)

    def _load_buffer_state(self) -> bool:
        """从 memory_tree.db 恢复消息列表。"""
        try:
            from langchain_core.messages import messages_from_dict
            state = self.get_memory_tree().load_buffer_state()
            # 恢复排队消息（断点续聊：生成进行中发的消息也已随 buffer 落盘；
            # 重启后先保留在队列，生成调度会自然接上）
            if state and state.get("inbox"):
                try:
                    self._inbox = messages_from_dict(state["inbox"])
                    if self._inbox:
                        self.pending_response = True   # 让调度线程继续处理它们
                    _logger.info("[session] 从 buffer_state 恢复排队消息: %d 条", len(self._inbox))
                except Exception:
                    self._inbox = []
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
                self._heal_wrapped_node_summaries()   # 事故批次：JSON 外壳摘要剥壳
                self._drop_superseded_node_messages()  # 事故批次：被父节点取代的旧节点消息清理
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
        - 离线时长已超过剩余时间 → 启动时立即触发一次心跳，并立即落盘（next=0，
          重启不会重复触发；要不要再醒由模型决定）。
        """
        if self._next_heartbeat_at <= 0:
            return
        if time.time() < self._next_heartbeat_at:
            remain = self._next_heartbeat_at - time.time()
            _logger.info("[heartbeat] 恢复调度：剩余 %.0f 秒", max(0, remain))
            return   # 还没到期：保留原到期时刻（剩余时间已扣减离线时长）
        _logger.info("[heartbeat] 启动时心跳已到期，立即补触发")
        self._fire_heartbeat()
        self._save_buffer_state()   # 落盘 next=0：崩溃重启不重复补触发

    # ── 异步压缩（后台线程，不阻塞生成）──────────────────────

    def _maybe_compress_async(self) -> None:
        """阈值检查：原始消息估算 token ≥「token 预算」且未在压缩 → spawn 后台压缩线程。

        叶子压缩**只看 token 预算**（`compress_token_budget`，用户设置，应用即生效）；
        条数不再参与判定（历史遗留的 `compress_trigger` 已移除）。
        压缩线程对 buffer 快照压缩（不触碰原 buffer），压缩完成后等待
        空闲窗口（无生成/无待处理消息），原子替换"新消息以外"的旧部分。
        """
        if self._compressing:
            return
        st = getattr(self, "settings", None) or {}
        keep = int(st.get("compress_keep", _SUMMARIZE_KEEP))
        token_budget = int(st.get("compress_token_budget", _SUMMARIZE_TOKEN_BUDGET))
        from .middleware import should_compress_raw
        with self.buffer_lock:
            raw = [m for m in self.recent_buffer
                   if "node_id" not in (getattr(m, "metadata", None) or {})]
            if not should_compress_raw(raw, keep, token_budget):
                return
            snapshot = list(self.recent_buffer)
        self._compressing = True
        _logger.info("[compress] 触发异步压缩（raw=%d 条，估算超 %d token 预算）",
                     len(raw), token_budget)
        # 前端指示灯（标题栏呼吸灯）：压缩是分钟级后台任务，给个可见反馈
        self.push_event({"type": "compress_state", "value": True})
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
                self._place_node_messages()
            self._drop_superseded_node_messages()   # 兜底：清理被本轮父节点取代的旧消息
            self._save_buffer_state()
            self.push_event({"type": "memory_update"})
            _logger.info("[compress] 异步压缩完成：删除 %d 条，插入 %d 节点消息",
                         len(remove_ids), len(adds))
        except Exception:
            _logger.exception("[compress] 异步压缩异常")
        finally:
            self._compressing = False
            self.push_event({"type": "compress_state", "value": False})

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
        """启动调度 daemon 线程（幂等）。不自动排心跳——心跳只由用户嘱托设置。"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._schedule_loop, name="planner-scheduler", daemon=True)
        self._heartbeat_thread.start()

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
                # 逾期日历项检查（每 10 分钟一次，避免重复轰炸）
                if now.minute % 10 == 0:
                    self._check_calendar_overdue()
                # 日历到点提醒（用户明确安排的，精度到分钟）
                self._check_calendar_due()
                # 重复系列滚动续期（每小时至多一次）
                self._extend_calendar_series()
                # 心跳到点
                with self.buffer_lock:
                    next_at = self._next_heartbeat_at
                if next_at > 0 and time.time() >= next_at:
                    self._fire_heartbeat()
            self._maybe_compress_async()   # 达到阈值 → 后台压缩（不阻塞）
            self._wake_event.wait(timeout=self._schedule_tick_seconds())
            self._wake_event.clear()

    def _schedule_tick_seconds(self) -> float:
        """下次轮询等待秒数：按待触发心跳的剩余时间自适应。

        心跳已承担"下一次循环入口"的职责（最短 0.1 秒），固定 5 秒轮询会让
        0.1 秒的心跳实际等 5 秒才触发——连续干活时每一轮都白卡 5 秒。
        """
        with self.buffer_lock:
            next_at = self._next_heartbeat_at
        if next_at <= 0:
            return SCHEDULER_IDLE_TICK_SECONDS          # 无待触发心跳：低频轮询
        remain = next_at - time.time()
        if remain <= 0:
            # 已到期但被生成阻塞（_fire_heartbeat 提前 return）：别空转
            return SCHEDULER_BUSY_TICK_SECONDS
        return min(SCHEDULER_IDLE_TICK_SECONDS, remain)

    def _in_startup_grace(self) -> bool:
        """启动宽限期：启动后 STARTUP_GRACE_SECONDS 秒内不触发自主行为。"""
        return time.time() - self._started_at < STARTUP_GRACE_SECONDS

    def _fire_heartbeat(self) -> None:
        """用户授权的定时唤醒到点：注入"该做嘱托的事了"并唤醒一次生成。

        新的心跳语义（2026-10-19）：只由模型在用户明确要求定时/巡检时设置；
        醒来后**不会自动重排**——模型不设下一次就停（完成即收尾），
        生成出错也不重试（这次提醒丢掉）。
        """
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
            self._save_buffer_state()   # 落盘 next=0：醒过就算消费（不自动重排/不重试）
            # 唤醒：只把时间事实 + 用户当时的嘱托交给模型，做不做/是否再设由它判断
            _logger.info("[heartbeat] 定时唤醒（距设置 %s：%s）",
                         self._fmt_duration(minutes), note)
            text = (
                f"（系统：你为用户设定的定时唤醒到点了。现在是 {self._now_desc()}，"
                f"{self._silence_desc()}"
                f"用户当时的嘱托/你设闹钟的原因：{note or '（没写原因）'}。"
                f"现在按嘱托去做：该检查的检查、该提醒的提醒。"
                f"**做完了就正常收尾，不要再设心跳**；"
                f"如果还没完成（比如在等用户、等外部状态），再调 heartbeat 设下一次"
                f"（note 写明「还没完成什么、下次醒来要看什么」）。）")
            self._receive(text, trigger=True)
            self._spawn_worker("heartbeat")

    def _check_calendar_due(self) -> None:
        """日历上到点的安排 → 注入并唤醒一次（这是用户明确要的提醒）。

        与 heartbeat 的区别：心跳是用户嘱托的"隔多久检查一次"，而这是**定点
        承诺**——所以它有自己独立的触发源，不占心跳那一个槽位，也不会被后续的
        心跳覆盖掉；两者都不受时段限制（用户自己安排的提醒必须到点执行）。

        判据：status='todo' 且带 time 且 (date,time) 已过 且 reminded_at 为空。
        """
        # 正在生成 / 有排队消息 → 让位，下一次轮询再投递（不会重复：未标记前不写库）
        with self.buffer_lock:
            if self._generating or self._inbox:
                return
        try:
            now = _now()
            due = self.db.list_calendar_due(now)
        except Exception:
            return
        if not due:
            return
        stamp = now.strftime("%Y-%m-%d %H:%M")
        parts = []
        for it in due:
            self.db.mark_calendar_reminded(it["id"], stamp)
            parts.append(f"「{it['content']}」（原定 {it['date']} {it['time']}）")
        self._receive(
            f"（系统：现在 {self._now_desc()}，日历上到点的是：{'；'.join(parts)}。"
            f"这是用户自己排的安排——你判断该提醒他、还是该先自己处理、"
            f"或者其实已经做过了（那就顺势问一句）。）",
            trigger=True)
        self._spawn_worker("reminder")
        _logger.info("[calendar] 到点提醒 %d 项：%s", len(due),
                     "；".join(it["content"] for it in due))

    def _extend_calendar_series(self) -> None:
        """重复系列滚动续期（每小时至多一次）。

        重复规则走的是"生成实体行"路线，只能生成到 horizon（默认 1 年），
        所以**必须有人来续**——否则一年后"每天吃药"会默默消失。
        放在调度线程里按小时检查，不再引入独立的定时任务。
        """
        stamp = _now().strftime("%Y-%m-%d %H")
        if getattr(self, "_series_extended_stamp", "") == stamp:
            return
        self._series_extended_stamp = stamp
        try:
            for sid in self.db.list_calendar_series_need_extend():
                n = self.db.extend_calendar_series(sid)
                if n:
                    _logger.info("[calendar] 重复系列 %s 续期 +%d 期", sid, n)
        except Exception:
            _logger.exception("[calendar] 续期失败")

    def _check_calendar_overdue(self) -> None:
        """逾期日历项检查（每天至多提醒一次，去重用持久化集合）。

        与 `_check_calendar_due` 的分工：
          · `_check_calendar_due`  管"**今天到点**"——精确到分钟，到点就投递；
          · 这里管"**日期已经过去、还没做**"的漏网项——每天扫一次，别让它悄悄沉底。
        去重集合 `_reminded_overdue` 随 buffer_state 持久化，避免重启后重复提醒。
        """
        try:
            overdue = self.db.list_overdue_calendar(_now(), skip_reminded=True)
        except Exception:
            return
        pending = [i for i in overdue
                   if i["id"] not in getattr(self, "_reminded_overdue", set())]
        if not pending:
            return
        if not hasattr(self, "_reminded_overdue"):
            self._reminded_overdue = set()
        if len(pending) > 3:
            pending = pending[:3]
        for i in pending:
            self._reminded_overdue.add(i["id"])
        self._save_buffer_state()   # 去重持久化，避免重启后重复提醒
        with self.chat_lock:
            with self.buffer_lock:
                if self._generating:
                    return
            text = ("（系统：日历上这几项已经过了时间还没做："
                    + "；".join(f"#{i['id']}「{i['content']}」"
                               f"原定 {i['date']} {i['time'] or '全天'}" for i in pending)
                    + "。你判断要不要提醒用户，或者问问他还要不要做。）")
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

        用户说话 = 活跃状态。
        **不重置心跳**：一人一句——AI 回答后保持原有定时，不主动插话。
        消息附带「距上次说话 X」——让 AI 感知对话节奏（间隔信息放在
        "对你说："之前，/history 切分时丢弃，对话框不显示）。

        files：拖入挂载的文件 [{name, path, kind, content?}]，按 kind 分流注入
        （text 内容直注 / doc 解析落盘+预览 / image OCR 提取 / 其他仅路径）。
        """
        now = _now()
        gap_hint = ""
        if self._last_player_message_at is not None:
            gap_hint = f"（距上次说话 {self._fmt_gap((now - self._last_player_message_at).total_seconds())}）"
        self._last_player_message_at = now
        text = message or "请结合我给你的文件回答。"
        prefix = f"[{now.strftime('%H:%M')}]{gap_hint} {PLAYER_NAME}对你说：{text}"
        if files:
            att = self._render_attachments(files)
            if isinstance(att, dict):
                # 视觉路径：文本块 + image_url 块（原图直接给模型）
                content: str | list = [{"type": "text", "text": prefix + "\n\n" + att["text"]}]
                content.extend(att["images"])
            else:
                content = prefix + att
        else:
            content = prefix
        msg = self._receive(content, trigger=True)
        self._save_log("user", message or "（拖入文件）")
        # 通知前端渲染这条用户消息：悬浮球语音输入 / 快速输入浮层发送时，面板
        # 没有走自己的本地 addMessage —— 之前展开面板看不到自己发的消息（用户实测）。
        msg_id = getattr(msg, "id", None) or ""
        self.push_event({"type": "user_message", "id": msg_id,
                         "text": message or "（拖入文件）"})
        self._wake_event.set()
        threading.Thread(target=self._player_worker, name="planner-player", daemon=True).start()
        return msg_id

    def _render_attachments(self, files: list) -> str | dict:
        """按 kind 分流注入挂载文件（标注来源；失败降级为仅路径）。

        返回：
        - str：纯文本注入（OCR 路径，模型不具备视觉能力时行为不变）
        - {"text": str, "images": [image_url 块, ...]}：视觉路径——文本说明
          + 原图直接给模型（替代 OCR）
        """
        from .fileparse import parse_file, save_attachment_text

        vision = self.vision_capable
        parts = []
        images: list[dict] = []
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
                if vision:
                    block = self._image_block(path)
                    if block:
                        images.append(block)
                        parts.append(f"- 图片 {name}{loc}（已发送原图给模型）")
                        continue   # 视觉注入成功：不叠加 OCR（用户选择只发图片）
                from .ocr import _global_client as _ocr_global
                text = _ocr_global().recognize_path(path)
                if text:
                    parts.append(f"- 图片 {name}{loc}：【图片 OCR 识别文字】\n{text}")
                else:
                    parts.append(f"- 图片 {name}{loc}：（OCR 未能识别出文字）")
            else:
                parts.append(f"- 文件 {name}{loc}")
        if not parts and not images:
            return ""
        text = "\n\n【拖入的文件】\n" + "\n\n".join(parts) if parts else ""
        if images:
            return {"text": text, "images": images}
        return text

    @staticmethod
    def _image_block(path) -> dict | None:
        """图片文件 → image_url 内容块（统一转 PNG data URL）；读取/转换失败返回 None。"""
        from .imageutil import image_to_data_url
        url = image_to_data_url(path)
        if not url:
            return None
        return {"type": "image_url", "image_url": {"url": url}}

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
        # 撤销 = 用户介入 = 活跃，不重置心跳（一人一句），保存状态
        self._save_buffer_state()
        self.push_event({"type": "log", "text": f"已撤销 {len(removed)} 条对话"})
        self.push_agenda_update()
        return {"ok": True, "removed": len(removed)}

    def kill_run_proc(self) -> None:
        """终止正在执行的 run_command 子进程（连整棵进程树）——停止/关闭时调用。"""
        proc = self._run_proc
        self._run_proc = None
        if proc is None:
            return
        try:
            from .tools import _kill_process_tree
            _kill_process_tree(proc)
            _logger.info("[stop] 已终止执行中的命令进程")
        except Exception:
            _logger.exception("[stop] 终止命令进程失败")

    def request_stop(self) -> None:
        """请求停止当前生成（用户点「停止」）。

        生成会在下一个安全点收尾（当前模型调用结束、工具执行完后），
        已生成的消息保留在 buffer；**正在执行的命令立即杀掉进程树**。
        """
        self._stop_requested = True
        self.kill_run_proc()
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
            self._pending_screenshots = []   # 防上一轮中断残留的陈旧截图注入
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
        """agent.stream：跑一轮**原生 ReAct 循环**（model → tools → model → …）。

        停止条件（任一）：模型不再返回工具调用（它认为够了）/ 调了 heartbeat /
        玩家消息让位 / 用户点停止 / 触到 run_limit 保险。
        一轮对话里模型可以连续调工具多次，工具结果由框架自动回喂给模型。

        流式输出：stream_mode=["messages", "values"]——
        - messages：AIMessageChunk 逐 token → push text_stream（面板逐字渲染）；
          非流式模型（mock）yield 完整 AIMessage → 整段作为一条 chunk
        - values：完整 state（removes 已生效）→ 最终 messages 回写 buffer；
          每轮模型文本收束时 push 完整 text（气泡 toast 用）
        """
        from langchain_core.messages import AIMessage, AIMessageChunk

        # 预检：没配 API Key 就直接说清楚（别等 provider 回 401 再让人猜）
        if not self.mock:
            cfg = resolve_llm_config(self.settings)
            if not cfg["api_key"]:
                self.push_error(
                    f"没有配置 {cfg['provider']} 的 API Key，无法调用模型。",
                    "点输入框上方 🤖 切换服务商，或去 设置 → 模型 API 填当前服务商的 Key。")
                return

        self._repair_buffer()
        self._strip_orphan_tool_messages()
        with self.buffer_lock:
            input_msgs = list(self.recent_buffer)
        final_messages = None
        current_msg_id = None
        current_chunks = []
        current_think_id = None
        # 本轮用量累计（跨 ReAct 多次模型调用求和；duration 为整轮生成耗时）
        gen_in = gen_out = gen_hit = gen_reason = 0
        gen_cache_known = False
        usage_calls = 0
        gen_t0 = time.time()
        # 注：_turn_change_ids 不在回合开始时清空——它要跨回合累积（多轮工具、
        # 自主续聊），只在「回应用户的回合结束」或新消息边界时统一出块（见下）
        # 已见消息 id（而非 seen_count=len(msgs)）：压缩中间件会 RemoveMessage
        # 收缩消息列表，按长度增量会漏掉压缩后新增的 ToolMessage → 工具卡片
        # 永远停在"正在执行"转圈。按消息 id 去重，与列表收缩无关。
        seen_msg_ids = {getattr(m, "id", None) or id(m) for m in input_msgs}
        interrupted = False
        try:
            # 无 checkpointer：每次 invoke 都是全新 state，字段必须显式初始化
            stream = self._agent.stream(
                {"messages": input_msgs, "set_heartbeat_called": False},
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
                    # 思考过程（DeepSeek reasoning_content；llm.py 的 passthrough 塞进
                    # additional_kwargs）：逐 token 推 thinking_stream，只展示不落 buffer。
                    try:
                        rc = str((chunk.additional_kwargs or {}).get("reasoning_content") or "")
                    except Exception:
                        rc = ""
                    if rc:
                        if current_think_id is None:
                            current_think_id = uuid.uuid4().hex[:12]
                        self.push_event({
                            "type": "thinking_stream",
                            "content": rc,
                            "msg_id": current_think_id,
                        })
                    # 用量统计：流式最终块带 usage_metadata（stream_usage=True）；
                    # 缓存命中由 llm.py shim 从 DeepSeek prompt_cache_hit_tokens 映射而来
                    um = getattr(chunk, "usage_metadata", None)
                    if um:
                        usage_calls += 1
                        gen_in += int(um.get("input_tokens") or 0)
                        gen_out += int(um.get("output_tokens") or 0)
                        # 最近一次模型调用的输入 = 真实上下文规模（含系统提示/节点/图片，
                        # 用于标题栏显示；比字符估算准确）
                        self._last_prompt_tokens = int(um.get("input_tokens") or 0)
                        gen_reason += int((um.get("output_token_details") or {})
                                          .get("reasoning") or 0)
                        cr = (um.get("input_token_details") or {}).get("cache_read")
                        if cr is not None:
                            gen_hit += int(cr)
                            gen_cache_known = True
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
                    # 一个超步结束（模型/工具节点收束）→ 下一段思考开新块
                    current_think_id = None
        except Exception as exc:
            _logger.exception("[agent] stream 异常")
            cfg = resolve_llm_config(self.settings)
            self.push_error(
                f"调用模型失败（{cfg['provider']} / {cfg['model']}）：{self._short_error(exc)}",
                self._llm_error_hint(exc))
            # 新心跳语义：不自动重排（这次提醒丢掉，用户可再要求）
            return
        if interrupted:
            _logger.info("[agent] 用户打断，立即停止当前生成")
            # 后台关闭流：langgraph sync stream 的 close() 会阻塞等待底层模型流
            # 自然结束，放 daemon 线程做——主线程立即返回，chat_lock/_generating
            # 尽快释放给下一条消息。del stream 移除本地引用，返回时不再二次 close。
            threading.Thread(target=_close_stream_bg, args=(stream,),
                             name="planner-stream-close", daemon=True).start()
            del stream
        # 用量/缓存统计（无 usage 的端点/失败轮次不记）；打断也记（token 已消耗）
        if usage_calls and (gen_in or gen_out):
            cfg = resolve_llm_config(self.settings)
            try:
                self.llm_stats.record(
                    input_tokens=gen_in, output_tokens=gen_out,
                    cache_hit=gen_hit, cache_known=gen_cache_known,
                    reason_tokens=gen_reason,
                    duration_s=time.time() - gen_t0,
                    provider=cfg["provider"], model=cfg["model"],
                    rounds=usage_calls, mock=self.mock)
                self.push_event({"type": "stats_update",
                                 "summary": self.llm_stats.summary()})
            except Exception:
                _logger.exception("[stats] 记录失败")
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
                # 思考过程（additional_kwargs.reasoning_content）**随消息持久化**：
                # 历史/重启后仍能在对话里展开看；不会被发回端点（langchain 的
                # _convert_message_to_dict 是白名单式转换，见 test_thinking_stream）。
                # 单轮调用上限触发的英文注入消息 → 替换成中文提示（别进 buffer 当正文）
                for m in filtered:
                    self._neutralize_limit_message(m)
                # 生成期间新追加的消息不能被 final_messages 回写覆盖：
                # 输入是 buffer 快照，期间到达的玩家消息（语音/打字）必须保留，
                # 交由下一轮生成处理（pending_response 已由 enqueue 置位）
                in_ids = {getattr(m, "id", None) or id(m) for m in input_msgs}
                extra = [m for m in self.recent_buffer
                         if (getattr(m, "id", None) or id(m)) not in in_ids
                         and getattr(m, "type", None) == "human"]
                self.recent_buffer = filtered + extra
            self._stamp_missing_ts()   # agent 生成的 ai/tool 消息补时间戳
            self._place_node_messages()
        # 改动过的文件 → 回复下方快捷块。**只在"回应用户"的回合结束时统一出块**：
        # 多轮工具调用、heartbeat 续聊都会先累积到这里，不会中途冒多个块
        # （用户反馈"中途的一些回答也会显示多轮修改情况"）；自主回合
        # （heartbeat/scheduled/nudge）的改动继续累积，等下一次"回应用户"
        # 或新消息边界（_receive）时统一出块——与 /history 的聚合口径一致。
        if self._turn_change_ids and self.current_trigger == "player":
            files = self.changed_files_for_ids(self._turn_change_ids)
            self._turn_change_ids = []
            if files:
                self.push_event({"type": "changed_files", "files": files})
        self.push_agenda_update()

    def _neutralize_limit_message(self, m) -> bool:
        """单轮调用上限触发的英文注入消息 → 中文提示（原地替换，返回是否命中）。

        来源：ModelCallLimitMiddleware(exit_behavior="end") 在超限时注入
        `AIMessage("Model call limits exceeded: run limit (N/N)")` 并跳 end。
        它是系统的保险丝，不是小助说的话——替换成可读中文，避免英文错误进对话。
        """
        try:
            if getattr(m, "type", None) != "ai":
                return False
            text = str(getattr(m, "content", "") or "")
            if not text.startswith("Model call limits exceeded"):
                return False
            limit = int(self.settings.get("model_call_limit", 100) or 100)
            m.content = (f"（本轮达到单轮调用上限（{limit} 次模型调用），已强制收尾。"
                         "如果任务还没做完，和我说「继续」即可接着干；"
                         "也可以到 设置 → 交互 → 单轮调用上限 调大。）")
            return True
        except Exception:
            return False

    def _place_node_messages(self) -> None:
        """把节点消息放回它对应区间的原位置（追加为主，最小改动）。

        压缩产生的节点消息（add_messages / 追加）最初落在列表末尾；它代表被压缩的
        早期对话，应位于所有原始消息之前、且按 node_start 排序。这里只做必要插入：
        **已经就位的节点不做任何移动**（前缀零改动，保护 prompt cache；旧实现每次
        压缩都把全部节点重排到最前，扩大前缀重写范围）。
        """
        with self.buffer_lock:
            buf = list(self.recent_buffer)
            idx = {i for i, m in enumerate(buf)
                   if "node_id" in (getattr(m, "metadata", None) or {})}
            if not idx:
                return
            ordered = sorted(
                (buf[i] for i in idx),
                key=lambda m: (getattr(m, "metadata", None) or {}).get("node_start", 0))
            rest = [m for i, m in enumerate(buf) if i not in idx]
            new_buf = ordered + rest
            if new_buf != buf:
                self.recent_buffer = new_buf

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
            name = getattr(m, "name", "") or ""
            if name == "continue_speaking":
                return   # 对应卡片不展示
            # 写操作留底关联：取出属于本次调用名字的 change id（队列按执行顺序）
            change_ids: list[int] = []
            with self.buffer_lock:
                while self._pending_changes and self._pending_changes[0].get("tool") == name:
                    change_ids.append(int(self._pending_changes.pop(0)["id"]))
            if change_ids:
                ak = getattr(m, "additional_kwargs", None)
                if isinstance(ak, dict):
                    ak["change_ids"] = change_ids   # 白名单转换 → 不会发回模型端点
                # 本轮改动汇总（回复下方「改动文件」快捷块：打开 / 打开所在目录）
                self._turn_change_ids.extend(change_ids)
            diff = self.change_diff(change_ids, tool=name) if change_ids else None
            self.push_event({"type": "tool_result", "id": getattr(m, "tool_call_id", ""),
                             "content": str(getattr(m, "content", "") or ""),
                             "diff": diff})
            # show_image：图片事件（前端渲染图片气泡；路径写进消息供 /history 重建）
            images: list[dict] = []
            if name == "show_image":
                with self.buffer_lock:
                    while self._pending_images:
                        images.append(self._pending_images.pop(0))
                if images:
                    ak = getattr(m, "additional_kwargs", None)
                    if isinstance(ak, dict):
                        ak["image_paths"] = [img["path"] for img in images]
                        ak["image_captions"] = [img.get("caption", "") for img in images]
                for img in images:
                    self.push_event({"type": "image",
                                     "id": getattr(m, "tool_call_id", ""),
                                     "url": image_url(img["path"]),
                                     "caption": img.get("caption", "")})

    def change_diff(self, change_ids: list[int], tool: str = "") -> str | None:
        """变更条目的「修改前后」unified diff 文本（工具行/历史展示用）。

        - 单条：changes.db 的 before/after 快照直接对比；
        - move_file（两条：源消失 + 目标创建）：内容不变 → 不逐行对比，给一行说明；
        - 未留底（no_snapshot）/内容过大 → 给替代说明；无差异 → None。
        """
        try:
            from .changes import diff_text
            rows = []
            db = self.get_changes_db()
            for cid in list(change_ids)[:4]:
                row = db.get_full(int(cid))
                if row is not None:
                    rows.append(row)
            if not rows:
                return None
            if tool == "move_file" and len(rows) >= 2:
                # 源行 after=None（文件消失）；目标行 = 另一条
                src = next((str(r["rel"]) for r in rows if r["after"] is None),
                           str(rows[0]["rel"]))
                dst = next((str(r["rel"]) for r in rows if str(r["rel"]) != src),
                           str(rows[-1]["rel"]))
                return f"（移动：{src} → {dst}，内容未变）"
            if tool == "move_file":
                return "（移动操作，内容未变）"
            if len(rows) > 1:
                # 多文件补丁（apply_patch）：每个文件一段 diff 拼接
                parts: list[str] = []
                for r in rows[:8]:
                    if r["no_snapshot"]:
                        parts.append(f"（{r['rel']}：内容过大/二进制，未留底，无法展示 diff）")
                        continue
                    d = diff_text(r["before"], r["after"], str(r["rel"]))
                    if d:
                        parts.append(d)
                return "\n".join(parts) if parts else None
            r = rows[0]
            if r["no_snapshot"]:
                return "（内容过大或二进制，未留底全文，无法展示 diff）"
            d = diff_text(r["before"], r["after"], str(r["rel"]))
            if d is None and (r["before"] or "") == (r["after"] or ""):
                return None
            if d is None:
                return "（内容过大，省略 diff）"
            return d
        except Exception:
            _logger.warning("[changes] diff 生成失败", exc_info=True)
            return None

    def changed_files_for_ids(self, change_ids: list[int]) -> list[dict]:
        """一组变更 id → 去重聚合的改动文件列表（回复下方快捷块用）。

        返回 [{path, rel, name, count, missing}]，**按改动次数降序**（同次数按
        相对路径排）——谁改得多排前面；missing = 该文件最后一次变更后已不存在
        （delete_file / move 的源），前端只给「打开所在目录」。
        """
        try:
            db = self.get_changes_db()
            agg: dict[str, dict] = {}
            for cid in list(change_ids or []):
                row = db.get(int(cid))
                if row is None:
                    continue
                key = str(row["path"])
                item = agg.get(key)
                if item is None:
                    item = {"path": key, "rel": str(row["rel"]),
                            "name": Path(key).name, "count": 0, "missing": False}
                    agg[key] = item
                item["count"] += 1
                # 按时间序迭代，最后一条变更的 after_missing 为准（可能被重新写回）；
                # 再兜底查一次磁盘实际存在性（覆盖用 run_command 等非留底方式删除的文件）
                item["missing"] = bool(row.get("after_missing")) or not Path(key).is_file()
            files = list(agg.values())
            files.sort(key=lambda f: (-f["count"], f["rel"].lower()))
            return files
        except Exception:
            _logger.warning("[changes] 改动文件聚合失败", exc_info=True)
            return []

    # ── 对外状态 ──────────────────────────────────────────────

    # ── 上下文统计（token 估算）────────────────────────────────

    def context_stats(self) -> dict:
        """当前上下文的长度统计。

        - `tokens`：字符估算（无 tokenizer，中文按 chars × 1.2）——用于分解占比展示；
        - `real_tokens`：**上一轮模型调用 API 实报的输入 token**（含系统提示/记忆节点/
          图片，最准）——标题栏优先显示它；重启后未生成过时回退估算。
        breakdown：system / nodes（记忆节点）/ raw（原始消息）/ images（视觉注入，每张 ≤384）。
        """
        system_chars = len(self.system_prompt or "")
        nodes = raw = images = 0
        with self.buffer_lock:
            for m in self.recent_buffer:
                content = getattr(m, "content", "") or ""
                if isinstance(content, list):
                    text = "".join(str(b.get("text") or "") for b in content
                                   if isinstance(b, dict))
                    images += sum(1 for b in content
                                  if isinstance(b, dict) and b.get("type") == "image_url")
                else:
                    text = str(content)
                if "node_id" in (getattr(m, "metadata", None) or {}):
                    nodes += len(text)
                else:
                    raw += len(text)
            msgs = len(self.recent_buffer)
        est = lambda n: int(n * 1.2)   # noqa: E731
        breakdown = {"system": est(system_chars), "nodes": est(nodes),
                     "raw": est(raw), "images": images * 384}
        chars = system_chars + nodes + raw
        return {"messages": msgs, "chars": chars,
                "tokens": sum(breakdown.values()),
                "real_tokens": int(getattr(self, "_last_prompt_tokens", 0) or 0),
                "breakdown": breakdown}

    def state_dict(self) -> dict:
        with self.buffer_lock:
            now_ = _now()
            today = now_.strftime("%Y-%m-%d")
            today_items = self.db.list_calendar_range(today, today)
            # 悬浮球/标题栏红点：**只统计今天**逾期未做的项（更早日期的逾期不再刷屏；
            # 日历提醒扫描（_check_calendar_due）仍用全量逾期，不受影响）。
            overdue_today = sum(
                1 for i in self.db.list_overdue_calendar(now_) if i["date"] == today)
            return {
                "mode": self.mode,
                "thinking": self.thinking,
                "compressing": bool(getattr(self, "_compressing", False)),
                "heartbeat": self.heartbeat_dict(),
                "agenda": {
                    "today": today,
                    "today_total": len(today_items),
                    "today_todo": sum(1 for i in today_items if i["status"] == "todo"),
                    "overdue_count": overdue_today,
                },
                "context": self.context_stats(),
                "activity": self._activity,
            }
