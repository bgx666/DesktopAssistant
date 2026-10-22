"""Agent 生成管线 + 工具行为测试（mock LLM，不调真实 API）。"""

import time
from datetime import timedelta

import pytest
from langchain_core.messages import HumanMessage

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree
from planner.session import PlannerSession
from planner.tools import build_tools


def _drive_generation(session, content: str, trigger: str = "player") -> None:
    """手动驱动一次生成（单线程，绕过 worker 线程）。"""
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response(trigger)


def _wait_until(pred, timeout: float = 5.0) -> None:
    """轮询等待条件成立（杜绝"赌固定 sleep 时长"造成的偶发失败）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("等待条件超时")


def test_stop_interrupts_streaming_generation(data_root):
    """打断（_stop_requested）应在流式模型调用中途立即停止，不等其自然结束。

    回归：旧实现只在 StopRequestMiddleware.before_model 检查停止标志——
    正在流式的这次模型调用会一直流到底，用户说话/点停止后 AI 仍继续吐字。
    现在 _run_agent 的流式循环内检查并 break。
    """
    import threading

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

    yielded: list[int] = []

    class _SlowStreamModel(BaseChatModel):
        def _llm_type(self) -> str:
            return "slow-stream"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            for i in range(200):
                time.sleep(0.03)          # 总时长 ~6s，若不打断会跑满
                yielded.append(i)
                yield ChatGenerationChunk(message=AIMessageChunk(content=f"字{i}"))

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_SlowStreamModel())
        threading.Timer(0.3, s.request_stop).start()   # 0.3s 后请求停止
        s._receive("你好", trigger=True)
        s.pending_response = False
        t0 = time.time()
        s._generate_response("player")
        elapsed = time.time() - t0
        assert yielded, "测试模型应真正流式产出 chunk（否则测试无意义）"
        assert elapsed < 2.0, f"打断应立即停止流式生成（而非跑满 ~6s），实际 {elapsed:.1f}s"
    finally:
        s.close()


def test_player_message_adds_calendar_item(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "帮我安排一下学习计划")
        items = s.db.list_calendar_range("2026-01-01", "2030-12-31")
        assert items, "mock 应把安排写进日历"
        assert items[0]["content"] == "复习线性代数第三章"
        # buffer 里应该有 assistant 消息（流式文本）+ 工具结果
        assert any(getattr(m, "type", None) == "ai" for m in s.recent_buffer)
        assert any(getattr(m, "type", None) == "tool" for m in s.recent_buffer)
        # 事件队列里有文本 + 流式事件（一次 drain 后分类）
        events = s.drain_events()
        texts = [e for e in events if e["type"] == "text"]
        assert texts
        streams = [e for e in events if e["type"] == "text_stream"]
        assert streams, "应有 text_stream 流式事件"
        by_id = {}
        for e in streams:
            by_id.setdefault(e["msg_id"], []).append(e["content"])
        joined = "".join("".join(v) for v in by_id.values())
        assert joined == texts[0]["content"], "流式拼接应与完整文本一致"
        # 工具调用卡片事件：tool_call 在前、tool_result 在后（含 heartbeat 定时唤醒）
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert tool_calls, "应有 tool_call 事件"
        assert any(e["name"] == "add_schedule" for e in tool_calls), "应有日历工具卡片"
        assert any(e["name"] == "heartbeat" for e in tool_calls), "heartbeat 也应展示卡片"
        assert len(tool_results) >= len(tool_calls)
        for tc in tool_calls:
            assert any(r["id"] == tc["id"] for r in tool_results), "tool_result 应对应 tool_call"
    finally:
        s.close()


def test_history_tool_calls_not_replayed(data_root):
    """历史 buffer 里的工具调用不应在后续每轮重新推送 tool_call 卡片。"""
    s = PlannerSession(data_root, mock=True)
    try:
        # 第一轮：往日历里排一条（产生 add_schedule 工具卡片）
        _drive_generation(s, "帮我安排一下学习计划")
        first = s.drain_events()
        assert any(e["type"] == "tool_call" for e in first)
        # 第二轮：历史 buffer 含 add_schedule 调用，不应重放
        _drive_generation(s, "（心跳）继续看看", "heartbeat")
        second = s.drain_events()
        second_calls = [e for e in second if e["type"] == "tool_call"]
        # 心跳轮 mock 会勾完成（可能有新工具），但绝不能出现
        # 第一轮 add_schedule 的历史工具调用（按参数识别）
        assert not any(
            e["name"] == "add_schedule"
            and e["args"].get("content") == "复习线性代数第三章"
            for e in second_calls
        ), "历史 add_schedule 被重放为卡片"
        # 第三轮同验
        _drive_generation(s, "（心跳）继续", "heartbeat")
        third = s.drain_events()
        assert not any(
            e["type"] == "tool_call"
            and e["name"] == "add_schedule"
            and e["args"].get("content") == "复习线性代数第三章"
            for e in third
        ), "历史 add_schedule 被重放为卡片"
    finally:
        s.close()


def test_heartbeat_cycle_adds_then_completes(data_root):
    """心跳循环：先排进日历，之后的心跳把最早一项勾完成。"""
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "给我安排点事", "player")
        items = s.db.list_calendar_range("2026-01-01", "2030-12-31")
        assert items, "第一轮应排进日历"
        assert all(i["status"] == "todo" for i in items)

        # 模拟心跳：勾掉最早的一项
        _drive_generation(s, "（心跳到了，请主动跟用户说点东西。）", "heartbeat")
        done = [i for i in s.db.list_calendar_range("2026-01-01", "2030-12-31")
                if i["status"] == "done"]
        assert done, "心跳轮应勾完成一项"
    finally:
        s.close()


def test_heartbeat_clamp_in_tool(data_root):
    """心跳护栏：下界 1 分钟（秒级续跑已废除）、上界 720 分钟。"""
    from planner import config as _config
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        # 下界：30 秒 < 1 分钟 → 被抬到 1 分钟
        tools["heartbeat"].invoke({"seconds": 30})
        assert abs(s._heartbeat_minutes - _config.PLANNER_HEARTBEAT_MIN_SECONDS / 60) < 1e-6
        assert s._heartbeat_minutes == 1.0
        # 下界之内保留：90 秒 = 1.5 分钟
        tools["heartbeat"].invoke({"seconds": 90})
        assert abs(s._heartbeat_minutes - 1.5) < 1e-6
        # 0 / 负数同样被抬到下界
        tools["heartbeat"].invoke({"minutes": 0})
        assert s._heartbeat_minutes == 1.0
        # 上界护栏：12 小时
        tools["heartbeat"].invoke({"minutes": 100000})
        assert s._heartbeat_minutes == 720
        d = s.heartbeat_dict()
        assert d["in_seconds"] > 0
    finally:
        s.close()


def test_heartbeat_update_overwrites(data_root):
    """改时间：再调一次 heartbeat 直接覆盖旧的（不需要先取消）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        tools["heartbeat"].invoke({"minutes": 30, "note": "旧嘱托"})
        first = s._next_heartbeat_at
        tools["heartbeat"].invoke({"minutes": 5, "note": "新嘱托"})
        assert s._next_heartbeat_at < first, "新设定应覆盖旧的到期时刻"
        assert abs((s._next_heartbeat_at - time.time()) - 5 * 60) < 5
        assert s._heartbeat_note == "新嘱托"
    finally:
        s.close()


def test_heartbeat_cancel(data_root):
    """取消：清空排定 + 落盘（重启不复活）；无排定时给明确提示。"""
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        tools["heartbeat"].invoke({"minutes": 30, "note": "旧嘱托"})
        assert s._next_heartbeat_at > 0
        r = tools["heartbeat"].invoke({"cancel": True})
        assert "已取消" in r
        assert s._next_heartbeat_at == 0 and s._heartbeat_note == ""
        assert s.heartbeat_dict()["in_seconds"] == 0
        # 没有排定时再取消 → 明确提示 + 不报错
        r2 = tools["heartbeat"].invoke({"cancel": True})
        assert "没有排定" in r2
    finally:
        s.close()
    # 已落盘：重启后仍是 0（不会复活）
    s2 = PlannerSession(data_root, mock=True)
    try:
        assert s2._next_heartbeat_at == 0
    finally:
        s2.close()


def test_heartbeat_requires_one_of_seconds_minutes(data_root):
    """两个间隔参数都不填 → 报错（给模型明确反馈，而不是默默取 0）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        with pytest.raises(Exception):
            tools["heartbeat"].invoke({"note": "什么都不填"})
    finally:
        s.close()


def test_calendar_tools_roundtrip(data_root):
    """日历工具走一遍：加日程 → 改期/改时刻 → 标完成 → 取消。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = {t.name: t for t in build_tools(s)}
        assert "add_schedule" in T and "create_task" not in T, \
            "任务工具应已随模块整体移除"

        out = T["add_schedule"].invoke({"date": "2026-09-20", "content": "开组会",
                                        "time": "09:30"})
        assert "已加入日历" in out
        iid = s.db.list_calendar_range("2026-09-20", "2026-09-20")[0]["id"]

        assert "已更新" in T["update_schedule"].invoke({"item_id": iid, "time": "10:00"})
        assert s.db.get_calendar_item(iid)["time"] == "10:00"

        assert "已更新" in T["update_schedule"].invoke({"item_id": iid, "status": "done"})
        assert s.db.get_calendar_item(iid)["status"] == "done"

        assert "已取消" in T["cancel_schedule"].invoke({"item_id": iid})
        assert s.db.get_calendar_item(iid)["status"] == "cancelled"
    finally:
        s.close()


def test_buffer_persists_across_restart(data_root):
    s1 = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s1, "你好")
        assert s1.recent_buffer
        saved_counter = s1._msg_counter
    finally:
        s1.close()
    s2 = PlannerSession(data_root, mock=True)
    try:
        assert s2.recent_buffer, "重启后应恢复 buffer"
        assert s2._msg_counter == saved_counter
    finally:
        s2.close()


def test_memory_tree_wired_to_session(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        assert isinstance(s.get_memory_tree(), SQLiteMemoryTree)
        nid = s.get_memory_tree().add_leaf("测试摘要", (0, 1), None)
        info = s.get_memory_tree().get_node_children_info(nid)
        assert info["details"] == []
        assert info["profile"] is None
        assert info["future_notes"] is None
    finally:
        s.close()

def test_player_message_does_not_reset_heartbeat(data_root):
    """用户说话 → 不重置心跳（一人一句）：旧定时保留。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.schedule_heartbeat(120)          # 模拟已有的长心跳
        assert s.heartbeat_dict()["in_minutes"] > 60
        s.enqueue_player_message("我回来了")
        # 心跳保持原定时（不被重置为短间隔）
        assert s.heartbeat_dict()["in_minutes"] > 60
    finally:
        s.close()


def test_continue_speaking_tool(data_root):
    """continue_speaking：分点描述时每调用一次暂停片刻再说下一点（循环不退出）；
    暂停时长 = 2s + 每 10 字 +1s（上限 10s）；且该工具不产生前端工具卡片。"""
    class _FakePauseEvent:
        """只替换本实例的暂停事件（monkeypatch threading.Event 会误伤心跳线程）。"""
        def __init__(self):
            self.waits = []
        def clear(self):
            pass
        def set(self):
            pass
        def wait(self, timeout=None):
            self.waits.append(timeout)
            return True
    s = PlannerSession(data_root, mock=True)
    try:
        fake = _FakePauseEvent()
        s._continue_pause_event = fake
        _drive_generation(s, "帮我分点描述一下学习计划")
        # 两轮 AI 输出：第一点（调 continue）+ 最后一点（纯文本收尾）
        ais = [m for m in s.recent_buffer if getattr(m, "type", None) == "ai"]
        assert len(ais) >= 2, "调 continue 后循环应继续（多轮 AI 输出）"
        texts = " ".join(str(m.content or "") for m in ais)
        assert "第一点" in texts and "最后一点" in texts
        # 工具确实被调用（buffer 里有 continue 的工具结果）
        assert any(getattr(m, "type", None) == "tool"
                   and getattr(m, "name", "") == "continue_speaking"
                   for m in s.recent_buffer)
        # 暂停过（Event.wait 以公式时长调用：2s 基础 + 上段字数/10，封顶 10s）
        assert fake.waits, "应触发暂停"
        assert all(2.0 <= t <= 10.0 for t in fake.waits), f"暂停时长应在 2~10s，实际 {fake.waits}"
        # 不产生前端工具卡片事件
        events = s.drain_events()
        calls = [e for e in events if e["type"] == "tool_call"]
        results = [e for e in events if e["type"] == "tool_result"]
        assert not any(e["name"] == "continue_speaking" for e in calls)
        # tool_result 事件也不该有 continue 对应的（其 tool_call 未展示）
        assert all(any(c["id"] == r["id"] for c in calls) for r in results)
    finally:
        s.close()


def test_continue_pause_interruptible(data_root):
    """continue 分段暂停可被用户打断：Event.set() 后 wait 立即返回（远小于超时）。"""
    import threading
    from planner.session import PlannerSession
    s = PlannerSession(data_root, mock=True)
    try:
        s._last_text_len = 80          # 时长 10s（封顶）
        t0 = time.time()
        # 后台线程 0.2s 后打断暂停
        threading.Timer(0.2, s.interrupt_continue_pause).start()
        s.pause_before_continue()
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"打断后暂停应立即结束，实际等了 {elapsed:.1f}s"
        # 未打断的暂停按公式时长等待（2s 基础）
        s._last_text_len = 0
        t0 = time.time()
        s.pause_before_continue()
        elapsed = time.time() - t0
        assert 1.5 <= elapsed <= 3.0, f"基础暂停应约 2s，实际 {elapsed:.1f}s"
    finally:
        s.close()


def test_parent_node_renders_time_range(data_root):
    """父节点（level>0）buffer 消息必须渲染 [时间] 行——模型回忆时按时间定位。
    回归：_compact_level 曾漏传 time_range，只有叶子显示时间。"""
    from langchain_core.messages import HumanMessage
    from planner.middleware import SummarizationMiddleware
    from planner.session import PlannerSession
    s = PlannerSession(data_root, mock=True)
    try:
        tree = s.get_memory_tree()
        msgs = []
        for i in range(8):
            t0 = f"2026-08-0{1 + i // 5} 0{i % 5}:00:00"
            t1 = f"2026-08-0{1 + i // 5} 0{i % 5}:30:00"
            nid = tree.add_leaf(f"摘要{i}", (i * 2, i * 2 + 1), None,
                                time_range=(t0, t1))
            msgs.append(HumanMessage(
                content=f"[{nid}] 第{i * 2}-{i * 2 + 1}条\n[摘要] 摘要{i}",
                metadata={"node_id": nid, "node_start": i * 2}))
        comp = SummarizationMiddleware(s)
        removes, adds = comp.compress_snapshot(msgs)
        parents = [m for m in adds
                   if str((getattr(m, "metadata", None) or {}).get("node_id", "")).startswith("node1_")]
        assert parents, "8 片叶子应触发向上合并，生成父节点"
        content = parents[0].content
        assert "[时间]" in content, f"父节点渲染应含 [时间]，实际: {content[:200]}"
        assert "2026-08-01" in content, "时间范围应来自子节点聚合"
    finally:
        s.close()


def test_heartbeat_prompt_carries_time_facts(data_root, monkeypatch):
    """心跳唤醒提示：时间锚点 + 用户嘱托 + 巡检语义（完成收尾/未完成再设）。"""
    s = PlannerSession(data_root, mock=True)
    injected: list[str] = []
    try:
        monkeypatch.setattr(s, "_spawn_worker", lambda trigger: injected.append(trigger))
        from planner.session import _now
        s._last_player_message_at = _now() - timedelta(minutes=95)   # 上次说话在 95 分钟前
        s.schedule_heartbeat(30, "盯一下简历投递")
        s._next_heartbeat_at = time.time() - 1
        s._fire_heartbeat()
        assert injected == ["heartbeat"]
        text = s.recent_buffer[-1].content
        assert "现在" in text and "周" in text, f"应带当前时间锚点：{text}"
        assert "1 小时" in text, f"应带距上次说话的间隔：{text}"
        assert "盯一下简历投递" in text, "应带上用户嘱托/设闹钟的原因"
        assert "做完了就正常收尾" in text, "应强调完成就收尾（不自动重排）"
        assert "还没完成" in text and "再调 heartbeat" in text, "应给出未完成的巡检写法"
        assert s._next_heartbeat_at == 0, "醒来后不自动重排（由模型决定是否再设）"
    finally:
        s.close()


def test_start_heartbeat_does_not_auto_schedule(data_root):
    """启动调度线程不自动排心跳（不再有开机自主唤醒）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.start_heartbeat()
        assert s._next_heartbeat_at == 0
    finally:
        s.stop_heartbeat()
        s.close()


def test_heartbeat_not_rescheduled_unless_model_asks(data_root, monkeypatch):
    """巡检循环：醒来后模型不调 heartbeat → 不再排程（完成即收尾）。"""
    from planner.llm import MockChatModel

    class _NoHbMock(MockChatModel):
        def _heartbeat_reply(self, reasoning: str = ""):
            return self._make_result("（看过了，还没完成，先不打扰。）")

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_NoHbMock(session=s))
        monkeypatch.setattr(s, "_spawn_worker", lambda trigger: None)
        s.schedule_heartbeat(1, "盯着任务")
        s._next_heartbeat_at = time.time() - 1
        s._fire_heartbeat()
        assert s._next_heartbeat_at == 0
        s._generate_response("heartbeat")   # 模拟醒来后的处理：模型没设心跳
        assert s._next_heartbeat_at == 0, "模型不设心跳时不得自动重排"
    finally:
        s.close()


def test_silence_desc_variants(data_root):
    """_silence_desc 只描述时间事实（无计数、无递进）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        assert "还没" in s._silence_desc()            # 从未说过话
        from planner.session import _now
        s._last_player_message_at = _now() - timedelta(seconds=10)
        assert "刚刚" in s._silence_desc()
        s._last_player_message_at = _now() - timedelta(hours=3)
        assert "3 小时" in s._silence_desc()
    finally:
        s.close()


def test_heartbeat_min_floor_clamped(data_root, monkeypatch):
    """心跳下界 1 分钟：秒级短心跳（续跑时代的残留）被 clamp 掉。

    轮内继续由原生 ReAct 循环承担，不再需要"秒级心跳续下一轮"。
    """
    s = PlannerSession(data_root, mock=True)
    injected: list[str] = []
    try:
        monkeypatch.setattr(s, "_spawn_worker", lambda trigger: injected.append(trigger))
        s.schedule_heartbeat(0.1 / 60)     # 0.1 秒 → 应被抬到 1 分钟
        assert s._heartbeat_minutes == 1.0, f"下界应是 1 分钟，实际 {s._heartbeat_minutes}"
        s._next_heartbeat_at = time.time() - 1
        s._fire_heartbeat()
        assert injected == ["heartbeat"]
        # 秒级心跳不再走"续跑"分支，而是正常唤醒（带时间锚点）
        assert "现在" in s.recent_buffer[-1].content
    finally:
        s.close()


def test_long_heartbeat_injects_time_anchor(data_root, monkeypatch):
    """定时唤醒：注入时间锚点（现在 + 距上次说话），不递增任何计数。"""
    s = PlannerSession(data_root, mock=True)
    injected: list[str] = []
    try:
        monkeypatch.setattr(s, "_spawn_worker", lambda trigger: injected.append(trigger))
        s.schedule_heartbeat(30)
        s._next_heartbeat_at = time.time() - 1
        s._fire_heartbeat()
        assert injected == ["heartbeat"]
        text = s.recent_buffer[-1].content
        assert "现在" in text, f"长心跳应给时间锚点：{text}"
        assert not hasattr(s, "_heartbeat_silent_count"), "沉默计数已移除"
    finally:
        s.close()


def test_react_loop_multi_round(data_root):
    """原生 ReAct 循环：工具执行完框架会再喂一次模型，模型看到结果才收口。

    回归：模型第一轮返回 tool_calls，第二轮看到工具结果后给最终答复。
    应发生 2 次模型调用（工具前 + 工具后），而不是"工具执行完就掐断回合"。
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    calls: list[int] = []

    class _ToolThenAnswerModel(BaseChatModel):
        """第一次调用返回工具调用，第二次（看到工具结果后）返回纯文本答复。"""

        def _llm_type(self) -> str:
            return "tool-then-answer"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(len(messages))
            if len(calls) == 1:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="我看一下接下来的安排。",
                    tool_calls=[{"name": "get_next_actions", "args": {}, "id": "call_1"}],
                ))])
            # 第二次：必须能看到工具结果（tool 消息已回喂），才给出最终答复
            assert any(getattr(m, "type", None) == "tool" for m in messages), \
                "第二轮模型调用必须带上工具结果"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="接下来先推进论文主线。",
            ))])

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_ToolThenAnswerModel())
        s._receive("接下来做什么？", trigger=True)
        s.pending_response = False
        s._generate_response("player")
        assert len(calls) == 2, f"工具→观察→再答应有 2 次模型调用，实际 {len(calls)} 次"
        # 工具结果留在 buffer 里
        assert any(getattr(m, "type", None) == "tool" for m in s.recent_buffer)
        # 最终答复也进了 buffer
        assert any("推进论文主线" in str(getattr(m, "content", ""))
                   for m in s.recent_buffer), "第二轮答复应写入 buffer"
    finally:
        s.close()


def _node_count(session) -> int:
    return sum(1 for m in session.recent_buffer
               if "node_id" in (getattr(m, "metadata", None) or {}))


# 叶子压缩只看 token 预算：测试把预算调到下限并用填充消息快速达到
_TEST_COMPRESS_BUDGET = 10000
_PAD = "x" * 200


def _prefill_for_compress(s, count, tag="占位"):
    """灌 count 条带填充的消息，使估算 token 越过测试压缩预算（条数 > keep）。"""
    s.update_settings({"compress_token_budget": _TEST_COMPRESS_BUDGET})
    for i in range(count):
        s.recent_buffer.append(HumanMessage(content=f"{tag} {i} " + _PAD))
    s._msg_counter += count


def _trigger_async_compress(session, timeout=15.0):
    """触发异步压缩并等待节点数增加（后台线程完成）。

    先等上一个压缩线程完全结束（_compressing 复位），避免 _maybe_compress_async
    因并发标志跳过导致下一轮不触发；按节点数增加判断，避免上一轮的
    节点让等待条件提前满足。
    """
    deadline = time.time() + timeout
    while session._compressing and time.time() < deadline:
        time.sleep(0.05)
    before = _node_count(session)
    session._maybe_compress_async()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _node_count(session) > before:
            return
        time.sleep(0.1)
    raise AssertionError("异步压缩超时未完成")


def test_tool_result_not_lost_after_compress(data_root):
    """工具结果事件配对正常（历史 seen_count 回归场景由异步压缩规避）。"""
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        # 灌到压缩预算边缘后，玩家消息触发生成 → 压缩必然发生
        _prefill_for_compress(s, 60, tag="占位消息")

        _drive_generation(s, "帮我安排一下学习计划")
        events = s.drain_events()

        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert tool_calls, "本轮应有工具调用事件"
        for tc in tool_calls:
            assert any(r["id"] == tc["id"] for r in tool_results), (
                f"工具 {tc['name']} 的 tool_result 未发出"
            )
        # 异步压缩最终应落节点
        _trigger_async_compress(s)
        assert any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer), "压缩应已触发"
    finally:
        s.close()


def test_compressed_nodes_kept_at_start(data_root):
    """连续多次压缩后，节点消息按 node_start 排序集中在 buffer 开头。

    回归：压缩节点经 langgraph add_messages 追加到末尾，多次压缩后
    早期摘要会卡在对话中间（时间顺序错乱）。
    """
    from planner.middleware import SUMMARIZE_KEEP_MESSAGES
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        for _round in range(2):
            _prefill_for_compress(s, 60, tag=f"第{_round}轮占位")
            _drive_generation(s, f"帮我安排任务（第{_round}轮）")
            _trigger_async_compress(s)

        nodes = [m for m in s.recent_buffer
                 if "node_id" in (getattr(m, "metadata", None) or {})]
        rest = [m for m in s.recent_buffer
                if "node_id" not in (getattr(m, "metadata", None) or {})]
        assert len(nodes) >= 2, "两轮压缩后应有 ≥2 个节点"
        # 节点全部在开头
        assert s.recent_buffer[:len(nodes)] == nodes, "节点应全部排在 buffer 开头"
        # 按 node_start 升序
        starts = [(getattr(m, "metadata", None) or {}).get("node_start") for m in nodes]
        assert starts == sorted(starts), f"节点应按 node_start 升序：{starts}"
        assert all("node_id" not in (getattr(m, "metadata", None) or {}) for m in rest)
        assert len(rest) <= SUMMARIZE_KEEP_MESSAGES + 10
    finally:
        s.close()


def test_compressed_ranges_are_global_sequential(data_root):
    """多次压缩的节点范围应为全局连续序号（001: 0-39, 002: 40-79…），不重叠。

    回归：旧实现用 enumerate(msgs) 相对索引，每次压缩节点都显示"第0-40条"
    （假重叠）；对齐小B _span 机制后按 _compressed_total 累计。
    """
    import re
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        for _round in range(2):
            _prefill_for_compress(s, 60, tag=f"第{_round}轮占位")
            _drive_generation(s, f"帮我安排任务（第{_round}轮）")
            _trigger_async_compress(s)

        nodes = [m for m in s.recent_buffer
                 if "node_id" in (getattr(m, "metadata", None) or {})]
        assert len(nodes) >= 2
        ranges = []
        for m in nodes:
            match = re.search(r"第(\d+)-(\d+)条", str(m.content))
            assert match, f"节点消息应有范围标注: {str(m.content)[:60]}"
            ranges.append((int(match.group(1)), int(match.group(2))))
        # 范围连续不重叠：后一个 start == 前一个 end + 1
        for prev, cur in zip(ranges, ranges[1:]):
            assert cur[0] == prev[1] + 1, f"节点范围应连续: {ranges}"
        # 首个节点从全局 0 开始；累计值 = 最后节点 end + 1
        assert ranges[0][0] == 0, f"首个节点应从 0 开始: {ranges}"
        assert s._compressed_total == ranges[-1][1] + 1
    finally:
        s.close()


def test_compression_emits_memory_update(data_root):
    """压缩成功后应推送 memory_update 事件（前端据此重载历史同步对话框）。"""
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        _prefill_for_compress(s, 60, tag="占位消息")
        _drive_generation(s, "帮我安排一下学习计划")
        # 异步压缩完成 → memory_update 事件
        deadline = time.time() + 15
        got_update = False
        while time.time() < deadline:
            events = s.drain_events()
            if any(e["type"] == "memory_update" for e in events):
                got_update = True
                break
            if any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer):
                break
            s._maybe_compress_async()
            time.sleep(0.1)
        assert got_update, "压缩应推送 memory_update"
        assert any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer), "压缩应已发生"
    finally:
        s.close()


def test_compress_batch_includes_paired_tool_messages(data_root):
    """压缩 batch 切开 ai(tool_calls) 与其工具结果时，配套 tool 消息必须一并压缩。

    回归：否则删 ai 留 tool → 孤儿 ToolMessage → DeepSeek 400（生成全挂）。
    """
    from langchain_core.messages import AIMessage, ToolMessage
    from planner.middleware import SummarizationMiddleware
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000})   # 只看 token 预算
        # 构造：ai(tool_calls) 恰好在 batch 边界（40 条填充后即 ai）
        for i in range(40):
            s.recent_buffer.append(HumanMessage(content=f"占位 {i} " + "x" * 200))
        ai = AIMessage(content="调用工具", id="cut-ai",
                       tool_calls=[{"name": "get_next_actions", "args": {},
                                    "id": "cut-call-1", "type": "tool_call"}])
        tool = ToolMessage(content="结果", tool_call_id="cut-call-1", id="cut-tool")
        s.recent_buffer.append(ai)
        s.recent_buffer.append(tool)
        for i in range(19):
            s.recent_buffer.append(HumanMessage(content=f"后置 {i} " + "x" * 200))
        s._msg_counter += 40 + 2 + 19

        comp = SummarizationMiddleware(s)
        removes, _ = comp.compress_snapshot(list(s.recent_buffer))
        remove_ids = {getattr(m, "id", None) or id(m) for m in removes}
        # 配套 tool 消息必须与 ai 一起被压缩删除（不在 remove_ids 会留孤儿）
        assert "cut-ai" in remove_ids, "边界处的 ai(tool_calls) 应被压缩"
        assert "cut-tool" in remove_ids, "配套 tool 消息必须一并压缩（否则孤儿 400）"

        # 应用删除后 buffer 无孤儿 tool
        kept = [m for m in s.recent_buffer
                if (getattr(m, "id", None) or id(m)) not in remove_ids]
        call_ids = set()
        for m in kept:
            for tc in (getattr(m, "tool_calls", None) or []):
                call_ids.add(tc.get("id"))
        orphans = [m for m in kept if getattr(m, "type", None) == "tool"
                   and (getattr(m, "tool_call_id", None) or "") not in call_ids]
        assert not orphans, "应用删除后不应有孤儿 tool 消息"
    finally:
        s.close()


def test_strip_orphan_tool_messages(data_root):
    """生成输入前清理孤儿 tool 消息（防御 400）。"""
    from langchain_core.messages import ToolMessage
    s = PlannerSession(data_root, mock=True)
    try:
        s.recent_buffer.append(HumanMessage(content="你好"))
        s.recent_buffer.append(ToolMessage(content="孤儿结果", tool_call_id="ghost-call"))
        s._strip_orphan_tool_messages()
        assert not any(getattr(m, "type", None) == "tool" for m in s.recent_buffer), \
            "孤儿 tool 应被清理"
        # 正常配对的不动
        s.recent_buffer.append(HumanMessage(content="再问"))
        from langchain_core.messages import AIMessage
        s.recent_buffer.append(AIMessage(
            content="",
            tool_calls=[{"name": "x", "args": {}, "id": "ok-call", "type": "tool_call"}]))
        s.recent_buffer.append(ToolMessage(content="ok", tool_call_id="ok-call"))
        s._strip_orphan_tool_messages()
        assert any(getattr(m, "tool_call_id", None) == "ok-call" for m in s.recent_buffer)
    finally:
        s.close()


def test_player_message_carries_gap_hint(data_root):
    """玩家消息附带「距上次说话 X」间隔提示（首次不带、之后带、格式化）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        # 首次：无上次记录 → 不带间隔
        s.enqueue_player_message("第一句")
        first = next(m for m in s.recent_buffer
                     if getattr(m, "type", "") == "human" and "第一句" in str(m.content))
        assert "距上次" not in str(first.content), "首次消息不应带间隔"

        # 第二次：间隔几秒 → 带"X 秒"
        time.sleep(1.2)
        s.enqueue_player_message("第二句")
        second = next(m for m in s.recent_buffer
                      if getattr(m, "type", "") == "human" and "第二句" in str(m.content))
        c = str(second.content)
        assert "距上次说话" in c, f"应有间隔提示: {c}"
        assert "秒" in c, f"秒级间隔: {c}"

        # 格式：1 小时 5 分钟
        assert s._fmt_gap(3900) == "1 小时 5 分钟"
        assert s._fmt_gap(125) == "2 分 5 秒"
        assert s._fmt_gap(30) == "30 秒"
    finally:
        s.close()


def test_message_during_generation_not_lost(data_root):
    """生成期间到达的玩家消息不被 final_messages 回写覆盖（语音偶发丢失根因）。"""
    import threading
    import time as _time
    from planner.llm import MockChatModel

    class _SlowMock(MockChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            _time.sleep(0.4)   # 拉长生成窗口，暴露竞态
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_SlowMock(session=s))
        s._player_worker = lambda: None   # 禁掉 enqueue 的生成线程（手动控制时序）
        # 心跳生成开始（后台线程）
        s._receive("（系统：心跳到了，请主动和用户说话。）", trigger=True)
        s.pending_response = False
        t = threading.Thread(target=s._generate_response, args=("heartbeat",))
        t.start()
        _time.sleep(0.05)                # 确保生成已开始（输入快照已取）
        # 生成进行中：玩家语音消息到达
        s.enqueue_player_message("语音内容测试")
        t.join(15)
        assert not t.is_alive(), "生成线程未结束"
        human_contents = [str(m.content) for m in s.recent_buffer
                          if getattr(m, "type", "") == "human"]
        assert any("语音内容测试" in c for c in human_contents), (
            "生成中到达的玩家消息被回写覆盖丢失"
        )
    finally:
        s.close()


def test_generation_without_heartbeat_keeps_original(data_root):
    """LLM 回复时不调用 heartbeat 工具 → 保持用户原定时间（不再被兜底改成 10 分钟）。

    场景：用户说"40 分钟后叫我"→ 期间又对话 → 对话回复不需要调用心跳工具，
    原定的 40 分钟必须保持不变。
    """
    from planner.llm import MockChatModel

    class _NoHbMock(MockChatModel):
        def _player_reply(self, reasoning: str = ""):
            return self._make_result("好的，我记住了。")

        def _heartbeat_reply(self, reasoning: str = ""):
            return self._make_result("（已查看，一切正常。）")

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_NoHbMock(session=s))
        s.schedule_heartbeat(40)          # 用户先设 40 分钟
        s._receive("请 40 分钟后叫我", trigger=True)
        s.pending_response = False
        s._generate_response("player")    # 该轮 LLM 不调用 heartbeat 工具
        assert abs(s._heartbeat_minutes - 40) < 1e-6, \
            f"心跳被兜底覆盖: {s._heartbeat_minutes}"
    finally:
        s.close()


def test_gap_hint_not_shown_in_history(data_root):
    """间隔提示在"对你说："之前 → /history 切分后不显示。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.enqueue_player_message("第一句")
        # ⚠️ 不能只 sleep 一个固定时长：全量跑时负载重，mock 生成可能超过它，
        # 第二句会落进 _inbox 还没并回 buffer → next() 抛 StopIteration（偶发失败）。
        # 等"彻底空闲"和"第二句确实进 buffer"两个确定条件，而不是等时间。
        _wait_until(lambda: not s._generating and not s.pending_response and not s._inbox)
        s.enqueue_player_message("第二句")
        _wait_until(lambda: any(
            getattr(m, "type", "") == "human" and "第二句" in str(m.content)
            for m in s.recent_buffer))
        second = next(m for m in s.recent_buffer
                      if getattr(m, "type", "") == "human" and "第二句" in str(m.content))
        c = str(second.content)
        assert "距上次" in c
        # 模拟 /history 的切分：对你说：之后的部分不应含间隔
        shown = c.split("对你说：", 1)[1]
        assert "距上次" not in shown, "间隔不应显示在对话框"
    finally:
        s.close()


def test_gap_hint_persists_across_restart(data_root):
    """上次玩家消息时间持久化：重启后首句也带离线时长间隔（累积）。"""
    from datetime import datetime, timedelta
    from planner.session import _TZ
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.enqueue_player_message("第一句")
        s1._last_player_message_at = datetime.now(_TZ) - timedelta(hours=2)   # 模拟离线 2 小时
        s1._save_buffer_state()
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        s2.enqueue_player_message("第二句")
        m = next(x for x in s2.recent_buffer
                 if "第二句" in str(getattr(x, "content", "")))
        c = str(m.content)
        assert "距上次说话" in c, f"重启后首句应带间隔: {c}"
        assert "2 小时" in c, f"应显示离线时长: {c}"
    finally:
        s2.close()


def test_heartbeat_may_stay_silent_with_empty_reply(data_root):
    """心跳无话可说（空回复）→ 不冒泡、buffer 不留空 AI 消息。"""
    from langchain_core.messages import AIMessage
    from planner.llm import MockChatModel

    class _SilentMock(MockChatModel):
        """心跳类系统消息 → 空回复；玩家消息走原脚本。"""

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            last = self._last_user_content(messages)
            if last.startswith(("（", "[", "【")):
                return self._make_result("")
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_SilentMock(session=s))
        _drive_generation(s, "（系统：心跳到了，请主动和用户说话。如果此刻实在没什么想说的，可以不说。）", "heartbeat")
        events = s.drain_events()
        assert not any(e["type"] == "text" for e in events), "空回复不应冒泡 text 事件"
        # 心跳注入消息之外，buffer 不应残留空 AI 消息
        assert not any(
            getattr(m, "type", None) == "ai"
            and not str(getattr(m, "content", "") or "").strip()
            for m in s.recent_buffer
        ), "空 AI 消息不应落 buffer"
        # 玩家消息仍正常回复
        _drive_generation(s, "在吗？", "player")
        assert any(e["type"] == "text" for e in s.drain_events())
    finally:
        s.close()

def test_stop_aborts_blocked_model_immediately(data_root):
    """点停止要立即中断卡住的模型 HTTP 请求，而不是等 API 超时。"""
    import threading
    from typing import ClassVar

    from langchain_core.language_models.chat_models import BaseChatModel

    release = threading.Event()
    started = threading.Event()
    closed = threading.Event()

    class _Root:
        def close(self):
            closed.set()
            release.set()

    class _BlockedModel(BaseChatModel):
        root_client: ClassVar = _Root()

        def _llm_type(self):
            return "blocked"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            release.wait(20)
            raise RuntimeError("aborted")

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            release.wait(20)
            raise RuntimeError("aborted")
            yield  # pragma: no cover

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_BlockedModel())
        threading.Timer(0.3, s.request_stop).start()
        s._receive("你好", trigger=True)
        s.pending_response = False
        t0 = time.time()
        s._generate_response("player")
        elapsed = time.time() - t0
        assert started.is_set(), "模型应已开始调用"
        assert closed.is_set(), "停止应关闭模型 HTTP 客户端"
        assert elapsed < 2.5, f"停止应立即中断阻塞请求，实际 {elapsed:.1f}s"
    finally:
        s.close()
