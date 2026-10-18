"""LLM 层（langchain）。

- build_chat_model()：ChatOpenAI（DeepSeek v4 适配：extra_body 传 thinking/reasoning_effort，
  不能用 model_kwargs——openai SDK 2.52 会把未知参数展开为 create() 命名参数而 TypeError；
  与 yaya backend 相同配置路径，配置来自 .env / D:\\xiaob\\.env）；
- MockChatModel：脚本化假 LLM（BaseChatModel），不调真实 API，供测试与无 key 联调。
  脚本逻辑驱动真实工具执行，形成「建任务 → 拆解 → 勾选完成」的自循环演示。
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from . import config as _config

_logger = logging.getLogger("planner.llm")


def _install_langchain_shims() -> None:
    """补齐 langchain_openai 对 DeepSeek 响应的两处静默丢弃（均为最小包装、幂等）。

    ① **reasoning_content**（思考过程）：`_convert_delta_to_message_chunk` /
    `_convert_dict_to_message` 只挑白名单字段，流式 delta 的 reasoning_content
    会被静默丢弃 → 面板拿不到思考流（只剩「思考中…」转圈）。包装后塞进
    `additional_kwargs["reasoning_content"]`（其余转换路径不变）。
    安全性：`_convert_message_to_dict` 是白名单式转换，additional_kwargs 里的
    非标准键**不会**被发回端点 → 不违反 DeepSeek「reasoning 不回传」的要求。

    ② **prompt_cache_hit_tokens**（缓存命中）：`_create_usage_metadata` 只认
    OpenAI 风格 `prompt_tokens_details.cached_tokens`，DeepSeek 的顶层字段
    `prompt_cache_hit_tokens` 会被丢 → 缓存命中统计拿不到数。包装后在
    OpenAI 风格字段缺省时用它填 `input_token_details.cache_read`。

    langchain 升级改变内部实现时，tests/test_thinking_stream.py 与
    tests/test_llm_stats.py 的假端点回归会直接红。
    """
    try:
        from langchain_openai.chat_models import base as _lo_base
    except Exception:   # pragma: no cover - 环境缺 langchain_openai 时静默跳过
        return
    if getattr(_lo_base, "_planner_shims_patched", False):
        return

    def _wrap_message_convert(fn):
        def _patched(_dict, *args, **kwargs):
            msg = fn(_dict, *args, **kwargs)
            try:
                rc = _dict.get("reasoning_content") if hasattr(_dict, "get") else None
            except Exception:
                rc = None
            if rc and getattr(msg, "additional_kwargs", None) is not None:
                if not msg.additional_kwargs.get("reasoning_content"):
                    msg.additional_kwargs["reasoning_content"] = rc
            return msg
        _patched.__name__ = getattr(fn, "__name__", "_patched")
        return _patched

    def _wrap_usage_convert(fn):
        def _patched(oai_token_usage, *args, **kwargs):
            usage = fn(oai_token_usage, *args, **kwargs)
            try:
                hit = (oai_token_usage or {}).get("prompt_cache_hit_tokens")
                details = usage.get("input_token_details") if isinstance(usage, dict) else None
                if hit is not None and details is not None and "cache_read" not in details:
                    details["cache_read"] = int(hit)
            except Exception:
                pass
            return usage
        _patched.__name__ = getattr(fn, "__name__", "_patched")
        return _patched

    for name in ("_convert_delta_to_message_chunk", "_convert_dict_to_message"):
        fn = getattr(_lo_base, name, None)
        if callable(fn):
            setattr(_lo_base, name, _wrap_message_convert(fn))
    usage_fn = getattr(_lo_base, "_create_usage_metadata", None)
    if callable(usage_fn):
        _lo_base._create_usage_metadata = _wrap_usage_convert(usage_fn)
    _lo_base._planner_shims_patched = True


_install_langchain_shims()


#: 服务商预设：端点 / 模型 / 模型清单地址（模型菜单与兜底用；切换服务商互不覆盖 Key 与模型）
PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "models_url": "https://api.deepseek.com/models",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "default": "deepseek-flash",
    },
    "opencode": {
        "label": "OpenCode Go",
        # 2026-10 官方端点为 /inference/go/openai/v1（旧 zen/go/v1 已要求会话头且随时可能停用）
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "models_url": "https://opencode.ai/inference/go/v1/models",
        # 只收录 chat/completions 兼容的模型（responses / messages / google 端点的模型不在菜单里）
        "models": ["deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3", "glm-5.3-flash",
                   "kimi-k3", "kimi-k2.7-code", "mimo-v2.6-flash", "mimo-v2.6-pro",
                   "longcat-2.5-preview-free", "step-5-preview", "hy3", "space-bunny"],
        "default": "deepseek-v4.1-flash",
    },
}

#: 自定义 User-Agent（OpenCode Go 要求客户端表明身份，不能用通用 SDK 名）
_LLM_USER_AGENT = "xiaozhu-planner/1.0 (code-agent; +https://github.com/)"


def provider_headers(provider: str, session_id: str = "") -> dict:
    """按服务商生成请求头：OpenCode Go 需要 `x-opencode-session`（会话稳定 ID，用于路由/缓存）。"""
    if (provider or "").strip().lower() != "opencode":
        return {}
    headers = {"User-Agent": _LLM_USER_AGENT}
    sid = str(session_id or "").strip()
    if sid:
        headers["x-opencode-session"] = sid
    return headers


def resolve_llm_config(settings: dict | None) -> dict:
    """设置 → 生效的 LLM 配置：{provider, base_url, api_key, model}。

    - provider：settings.llm_provider（默认 deepseek）；
    - base_url：settings.llm_base_url_<provider> 覆盖 > 预设端点；
    - api_key：settings.llm_api_key_<provider> > 旧字段 llm_api_key（仅 DeepSeek）> env LLM_API_KEY；
    - model：settings.llm_model_<provider> > 旧字段 llm_model（仅 DeepSeek）> env PLANNER_LLM_MODEL > 预设默认。
    """
    s = settings or {}
    provider = str(s.get("llm_provider") or "deepseek").strip().lower()
    preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["deepseek"]
    base_url = (str(s.get(f"llm_base_url_{provider}") or "").strip()
                or preset["base_url"])
    api_key = (str(s.get(f"llm_api_key_{provider}") or "").strip()
               or (str(s.get("llm_api_key") or "").strip() if provider == "deepseek" else "")
               or os.getenv("LLM_API_KEY", "").strip())
    model = (str(s.get(f"llm_model_{provider}") or "").strip()
             or (str(s.get("llm_model") or "").strip() if provider == "deepseek" else "")
             or os.getenv("PLANNER_LLM_MODEL", "").strip()
             or preset["default"])
    return {"provider": provider, "base_url": base_url, "api_key": api_key, "model": model}


# ── 模型构建 ──────────────────────────────────────────────────

def resolve_model_name(model_name: str | None = None) -> str:
    """模型名解析链：显式参数 > PLANNER_LLM_MODEL > 默认 `deepseek-flash`。

    planner 默认用 DeepSeek-V4.1-Flash（官方 API 名 `deepseek-flash`，多模态）；
    共享 .env 的 LLM_MODEL 是 xiaob 游戏的配置，不再影响 planner
    （回退纯文本：settings.json 的 llm_model 或 `PLANNER_LLM_MODEL=deepseek-v4-flash`）。
    （env 在调用时读取——config 常量在 import 时绑定，测试 monkeypatch 不到。）
    """
    if model_name and str(model_name).strip():
        return str(model_name).strip()
    planner_model = os.getenv("PLANNER_LLM_MODEL", "").strip()
    if planner_model:
        return planner_model
    return _config.PLANNER_DEFAULT_MODEL


def is_vision_model(model_name: str) -> bool:
    """模型是否具备图像输入能力（按模型名启发式判定，覆盖常见命名）。

    ⚠️ `deepseek-flash`（官方 V4.1-Flash）是多模态模型，必须收录——
    否则默认模型下 `capture_screen` 会**静默降级为 OCR**。
    另收录 OpenCode Go 常见家族（gpt-5/6、claude、gemini）便于切服务商后仍走原图。
    """
    name = (model_name or "").lower()
    return any(k in name for k in (
        "vision", "-vl", "-omni", "multimodal", "gpt-4o", "gpt-4.1",
        "deepseek-flash", "gpt-5", "gpt-6", "claude", "gemini"))


#: 思考开关的三个取值（官方 thinking.type，实测都有效）
THINKING_MODES = ("adaptive", "enabled", "disabled")
#: 思考强度的三个取值（官方 reasoning_effort）
REASONING_EFFORTS = ("low", "high", "max")


def is_deepseek_v4(model_name: str) -> bool:
    """是不是 DeepSeek v4 家族（决定发不发思考参数）。

    **不能只判断 `deepseek-v4` 前缀**：官方现在的主力 id 叫 `deepseek-flash`
    （正式名 DeepSeek-V4.1-Flash），前缀判断匹配不上它——
    用官方名时设置页里的思考开关会**静默失效**（测试抓到的就是这个）。
    """
    name = (model_name or "").strip().lower()
    return (
        name.startswith("deepseek-v4")
        or name == "deepseek-flash"
        or name.startswith("deepseek-flash-")
    )


def thinking_extra_body(model_name: str,
                        thinking_mode: str | None = None,
                        reasoning_effort: str | None = None,
                        provider: str | None = None) -> dict:
    """DeepSeek v4 的思考参数（**纯函数**，方便直接测）。

    单独抽出来的原因：LangChain 把 `extra_body` 交给底层 client 在请求时合并，
    实例上看不到这个属性——想断言"到底发了什么"就只能靠这个函数。

    规则：
    - 只有 DeepSeek v4 家族发这两个字段（别的服务商不认，发了会 400）
    - 取值非法就回落到默认（adaptive / high），不让设置页的脏值传到线上
    - `disabled` 时**不发** `reasoning_effort`：官方会忽略它，
      发了只会让人误以为强度还在起作用
    - **provider=opencode（OpenCode Go）：adaptive 映射为 enabled**——其网关
      （实测 deepseek-v4.1-flash）只认 `type: enabled/disabled`，发 adaptive 会 400
      （"thinking must be an object with type enabled or disabled"）；官方端点对
      adaptive 实测有效，保持不变。
    """
    if not is_deepseek_v4(model_name):
        return {}
    mode = (thinking_mode or "adaptive").strip().lower()
    if mode not in THINKING_MODES:
        mode = "adaptive"
    if (provider or "").strip().lower() == "opencode" and mode == "adaptive":
        mode = "enabled"
    effort = (reasoning_effort or "high").strip().lower()
    if effort not in REASONING_EFFORTS:
        effort = "high"
    extra: dict = {"thinking": {"type": mode}}
    if mode != "disabled":
        extra["reasoning_effort"] = effort
    return extra


def build_chat_model(api_key: str | None = None,
                     base_url: str | None = None,
                     model_name: str | None = None,
                     thinking_mode: str | None = None,
                     reasoning_effort: str | None = None,
                     max_tokens: int | None = None,
                     provider: str | None = None,
                     session_id: str | None = None) -> ChatOpenAI:
    """DeepSeek v4 适配的 ChatOpenAI（也兼容 OpenCode Go 等 OpenAI 兼容端点）。

    配置优先级：显式参数（用户设置）> PLANNER_LLM_MODEL > 默认 `deepseek-flash`。
    `provider`（+`session_id`）用于附加服务商要求的请求头：OpenCode Go 需要
    `x-opencode-session`（会话稳定 ID，路由/缓存优化）与自定义 User-Agent。

    思考开关与强度来自设置页（`settings.py` 的 `thinking_mode` / `reasoning_effort`），
    拼装逻辑见 [thinking_extra_body]。实测要点（2026-09-30，DeepSeek 官方）：
    - `disabled` 时 reasoning token **真的是 0**，同一道题快约 30%
    - 曾经这里写死 `reasoning_effort: "low"`（想更快更省），但用一道**真实的方案题**
      实测发现 low 反而推理更多、答得更短、还更慢（3005/1604字/20.5s vs
      high 的 1645/2222字/15.4s）。所以现在默认 `high`，并把选择权交给设置页
    - 不再显式发 `thinking: enabled`：默认就在推理，显式打开没收益
    """
    model_name = resolve_model_name(model_name)
    base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
    api_key = api_key or os.getenv("LLM_API_KEY")
    kwargs: dict[str, Any] = {}
    extra = thinking_extra_body(model_name, thinking_mode, reasoning_effort,
                                provider=provider)
    if extra:
        kwargs["extra_body"] = extra
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)
    headers = provider_headers(provider or "", session_id or "")
    if headers:
        kwargs["default_headers"] = headers
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
        timeout=int(os.getenv("LLM_TIMEOUT", "60")),
        stream_usage=True,   # 流式最终块带 usage（缓存命中/TPS 统计的数据源）
        **kwargs,
    )


# ── MockChatModel（脚本化假 LLM）─────────────────────────────

class MockChatModel(BaseChatModel):
    """脚本化假 LLM（BaseChatModel 子类）：不调真实 API，走同一 agent graph。

    脚本逻辑（依据 session 的任务库状态决策）：
    - 玩家消息 → 文本回复 + heartbeat(minutes=15)；
    - 心跳消息 → 按需轮转：建任务 / 拆解任务 / 勾选今日计划 / 更新任务状态，
      每轮都以 heartbeat 收尾（self-healing 循环，可无 API 演示完整链路）。
    """

    def __init__(self, session=None, **kwargs: Any) -> None:
        super().__init__(model="mock-planner", **kwargs)
        self._session = session
        self._heartbeat_count = 0
        self._rng = random.Random()
        self._uuid = uuid

    def reset(self) -> None:
        self._heartbeat_count = 0

    @property
    def _llm_type(self) -> str:
        return "mock-planner"

    def bind_tools(self, tools, **kwargs):
        """记录绑定的工具名并返回 self（create_agent 每次模型调用都会重新 bind_tools，
        不能新建副本——否则 _heartbeat_count 等脚本状态每次调用都会丢失）。"""
        self._bound_tools = [t.name for t in (tools or []) if hasattr(t, "name")]
        return self

    # ── BaseChatModel 实现 ───────────────────────────────────

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_user = self._last_user_content(messages)
        has_tools = bool(getattr(self, "_bound_tools", None) or kwargs.get("tools"))
        if not has_tools:
            # 压缩等独立调用：输出结构化 JSON（任务型 schema，见 middleware.MemoryNodeOutput）
            return self._make_result(json.dumps({
                "summary": "（摘要）助手和用户讨论了任务安排，并按计划推进。",
                "task": {"goal": "按时完成学习计划", "status": "按计划推进",
                         "todo": [], "next_steps": [], "blockers": []},
                "decisions": [{"what": "先复习线性代数第三章", "why": "基础不牢会影响后续"}],
                "lessons": {"errors": [], "patterns": ["先列计划再动手"]},
                "context": {"env": [], "commands": [], "artifacts": [], "constraints": []},
                "user": ["喜欢晚上学习", "做事有计划"],
            }, ensure_ascii=False))
        # 模拟 ask_user：上一轮工具结果就是用户的回答 → 收尾（只看**最后一条**，
        # 历史里的旧回答不能触发收尾，否则后续提问会被跳过）
        last = messages[-1] if messages else None
        if getattr(last, "type", None) == "tool":
            last_content = str(getattr(last, "content", "") or "")
            if "用户选择了" in last_content or "用户回答" in last_content:
                return self._make_result("好，收到，就按这个来。")
            # 写文件/补丁/展示图片/读图脚本收尾（dev/e2e 验证工具行 diff、图片气泡与看图注入）
            if getattr(last, "name", "") in {"write_file", "edit_file", "delete_file",
                                             "move_file", "show_image", "view_image",
                                             "apply_patch"}:
                return self._make_result("好了，弄完了。")
        # 模拟 continue_speaking：上一轮调用了分段工具 → 本轮纯文本收尾（无工具调用）
        for m in messages:
            if (getattr(m, "type", None) == "tool"
                    and "继续" in str(m.content or "")):
                return self._make_result("（这是分点说法的最后一点，说完了。）")
        # 玩家要求分点描述 → 先调 continue_speaking 说第一点（循环继续）
        # （用所有 HumanMessage 判断：计划快照会注入"（当前计划）"Human 且以 [ 开头）
        user_texts = " ".join(
            str(m.content or "") for m in messages if isinstance(m, HumanMessage))
        # ⚠️ 触发词扫描的是**全历史** HumanMessage：分支顺序 = 优先级，一个触发词
        # 一旦用过就永远留在历史里 → **新触发词必须加在现有词之前**（越新越靠前），
        # 否则新消息会被历史里的旧词劫持（实测：「考我」放最前会让 step12 之后的
        # 「改文件/画图/…」全被历史的「考考我」抢走；「考我」放最后则被「打补丁」抢走）。
        # 「出题」= ask_user 的专属高优先词（正式"考我"测试词已进历史，见上）。
        # 带「多想」时附带思考流（e2e 造"隐藏期间在途思考+提问卡"场景）。
        if "出题" in user_texts:
            return self._make_result("行，先考你一道：",
                                     [self._tc("ask_user", {
                                         "question": "这两个方案先做哪个？",
                                         "options": ["A：先做方案一", "B：先做方案二"]})],
                                     reasoning=self._mock_reasoning(user_texts))
        if "分点" in user_texts or "continue" in user_texts:
            return self._make_result("第一点：先梳理整体目标。",
                                     [self._tc("continue_speaking", {})])
        # 触发多文件补丁（dev/e2e：验证 apply_patch 的审批/多文件 diff 渲染）
        if "打补丁" in user_texts:
            patch = "\n".join([
                "*** Begin Patch",
                "*** Add File: e2e_patch_a.txt",
                "+alpha",
                "*** Add File: e2e_patch_b.txt",
                "+beta",
                "*** End Patch",
            ])
            return self._make_result("好，我打个补丁建两个文件。",
                                     [self._tc("apply_patch", {"patch": patch})])
        # 触发 HTML 预览（dev/e2e：回复里带 ```html 代码块 → 沙箱可交互预览卡）
        if "做个网页" in user_texts:
            demo = "\n".join([
                "<!doctype html><html><body style=\"font-family:sans-serif;padding:12px\">",
                "<button id=\"btn\">点我 +1</button> <span id=\"out\">0</span>",
                "<script>",
                "document.getElementById('btn').onclick = () => {",
                "  const o = document.getElementById('out');",
                "  o.textContent = Number(o.textContent) + 1;",
                "};",
                "</" + "script>",
                "</body></html>",
            ])
            return self._make_result("给你做了个能点的小演示：\n\n```html\n" + demo + "\n```")
        # 触发图片展示（dev/e2e：需已启用工作区 + 工作区里已有 plot.png）
        if "看图" in user_texts:
            return self._make_result("好，我看一眼这张图。",
                                     [self._tc("view_image", {
                                         "source": "plot.png",
                                         "note": "确认图表内容"})])
        if "画图" in user_texts:
            return self._make_result("好，图给你看。",
                                     [self._tc("show_image", {
                                         "source": "plot.png",
                                         "caption": "测试图表（mock）"})])
        # 触发写文件（dev/e2e 验证工具行的「修改前后」diff：需已启用代码工作区）
        if "改文件" in user_texts:
            return self._make_result("好，我在工作区里写一个文件。",
                                     [self._tc("write_file", {
                                         "path": "e2e_note.txt",
                                         "content": "# 小舟的测试文件\n第一行内容\n第二行内容\n"})])
        # 触发 ask_user 弹窗（dev/e2e 验证提问链路：发一句带「考我」的消息；
        # 带「多想」时同时附带思考流）
        if "考我" in user_texts:
            return self._make_result("行，先考你一道：",
                                     [self._tc("ask_user", {
                                         "question": "这两个方案先做哪个？",
                                         "options": ["A：先做方案一", "B：先做方案二"]})],
                                     reasoning=self._mock_reasoning(user_texts))
        if last_user.startswith(("（", "[", "【")):
            return self._heartbeat_reply(reasoning=self._mock_reasoning(user_texts))
        return self._player_reply(reasoning=self._mock_reasoning(user_texts))

    @staticmethod
    def _mock_reasoning(user_texts: str) -> str:
        """触发思考流展示（dev/e2e 验证 thinking_stream：发一句带「多想」的消息）。"""
        if "多想" in user_texts:
            return "先扫一眼上下文和日历，判断用户现在最需要什么，再决定怎么回。（mock 思考流）"
        return ""

    # ── 脚本 ─────────────────────────────────────────────────

    @staticmethod
    def _last_user_content(messages) -> str:
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                return str(m.content or "")
        return ""

    def _today(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    @staticmethod
    def _upcoming(db) -> list[dict]:
        """脚本用：今天起 7 天内的日历项（读失败就当空，别让 mock 把测试带崩）。"""
        from datetime import datetime, timedelta, timezone
        try:
            today = datetime.now(timezone(timedelta(hours=8))).date()
            return db.list_calendar_range(today.isoformat(),
                                          (today + timedelta(days=7)).isoformat())
        except Exception:
            return []

    def _player_reply(self, reasoning: str = "") -> ChatResult:
        calls = [self._tc("heartbeat", {"minutes": 15})]
        session = self._session
        db = getattr(session, "db", None) if session else None
        text = self._rng.choice(_PLAYER_REPLIES)
        if db is not None and not self._upcoming(db):
            text = "（用户还没安排什么，我先主动把学习排进日历。）"
            calls.insert(0, self._tc("add_schedule", {
                "date": self._today(),
                "content": "复习线性代数第三章",
                "time": "20:00",
                "detail": "矩阵与行列式，重点：逆矩阵与秩",
            }))
        return self._make_result(text, calls, reasoning=reasoning)

    def _heartbeat_reply(self, reasoning: str = "") -> ChatResult:
        calls = []
        text = "（我看了看日历，一切照常推进。）"
        session = self._session
        db = getattr(session, "db", None) if session else None
        if db is not None:
            try:
                items = self._upcoming(db)
                todo = [i for i in items if i["status"] == "todo"]
                if not items:
                    text = "（日历上还是空的，我排一条进去。）"
                    calls.append(self._tc("add_schedule", {
                        "date": self._today(),
                        "content": "复习线性代数第三章",
                        "time": "20:00",
                    }))
                elif todo:
                    text = "（日历上还有没做的，先勾掉最早的一项。）"
                    calls.append(self._tc("update_schedule",
                                          {"item_id": todo[0]["id"], "status": "done"}))
                else:
                    text = "（日历上的安排都做完了，休息一下。）"
            except Exception:
                _logger.exception("[mock] 脚本状态读取失败")
        calls.append(self._tc("heartbeat", {"minutes": 15}))
        return self._make_result(text, calls, reasoning=reasoning)

    # ── 构造工具调用 ─────────────────────────────────────────

    def _tc(self, name: str, args: dict) -> dict:
        return {"name": name, "args": args, "id": f"mockcall_{self._uuid.uuid4().hex[:12]}",
                "type": "tool_call"}

    def _make_result(self, content: str, tool_calls: list[dict] | None = None,
                     reasoning: str = "") -> ChatResult:
        kwargs = {"reasoning_content": reasoning} if reasoning else {}
        # mock 用量（确定性；缓存命中率 75%）：让统计面板/dev/e2e 有可看的数据
        text_len = len(str(content or ""))
        usage = {
            "input_tokens": 1200,
            "output_tokens": max(1, text_len // 3),
            "total_tokens": 1200 + max(1, text_len // 3),
            "input_token_details": {"cache_read": 900},
            "output_token_details": {},
        }
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=content, tool_calls=tool_calls or [],
                                             additional_kwargs=kwargs,
                                             usage_metadata=usage))
        ])


# ── mock 脚本素材 ─────────────────────────────────────────

_PLAYER_REPLIES = [
    "收到，我看看今天的计划安排，稍后跟你同步。",
    "好的，我先把任务记下来，拆好计划再告诉你。",
    "明白，我会盯进度，到点提醒你。",
    "没问题，先把手头的活干完，回头我再跟你确认。",
]
