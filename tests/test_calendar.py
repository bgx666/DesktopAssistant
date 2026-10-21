"""日历（calendar_items）：存储层 + planner 工具 + HTTP 端点。

日历是**唯一**的待办概念：按时间排、用户可见可编辑、可带时刻到点提醒、可按规则重复。
（2026-09-12 前还有 tasks / plan_items 这套"长期目标 + 推进队列"，已整体移除。）
"""

import json
import threading
import urllib.request
from datetime import datetime

import pytest

from planner.server import create_server
from planner.session import PlannerSession
from planner.tools import build_tools


# ── 存储层 ──────────────────────────────────────────────────

def test_calendar_crud(data_root):
    """新增 / 范围查询 / 改期改时刻 / 完成 / 取消。"""
    s = PlannerSession(data_root, mock=True)
    try:
        db = s.db
        i1 = db.add_calendar_item("2026-09-11", "改论文", "20:00", kind="event")
        i2 = db.add_calendar_item("2026-09-11", "买牛奶", None, kind="task")
        i3 = db.add_calendar_item("2026-09-15", "投简历", "09:30")

        # 范围查询（闭区间），全天项排当天最前
        got = db.list_calendar_range("2026-09-11", "2026-09-12")
        assert [x["id"] for x in got] == [i2, i1], "全天项应排当天最早"
        assert [x["id"] for x in db.list_calendar_range("2026-09-13", "2026-09-15")] == [i3]

        # 改内容与时刻
        assert db.update_calendar_item(i2, content="买牛奶和面包", time="08:00")
        it = db.get_calendar_item(i2)
        assert it["content"] == "买牛奶和面包" and it["time"] == "08:00"

        # 完成（自动写 done_at）
        assert db.set_calendar_status(i1, "done")
        assert db.get_calendar_item(i1)["done_at"]

        # 取消 = 软删，默认不再出现在查询里
        assert db.cancel_calendar_item(i3)
        assert db.get_calendar_item(i3)["status"] == "cancelled"
        assert db.list_calendar_range("2026-09-15", "2026-09-15") == []
        assert [x["id"] for x in
                db.list_calendar_range("2026-09-15", "2026-09-15", include_cancelled=True)] == [i3]
    finally:
        s.close()


def test_calendar_due_and_reminded(data_root):
    """到点未提醒的项可被扫描出来；标记后不再重复。全天项不参与。"""
    s = PlannerSession(data_root, mock=True)
    try:
        db = s.db
        db.add_calendar_item("2026-09-11", "改论文", "20:00")
        db.add_calendar_item("2026-09-11", "买牛奶", None)      # 全天：不提醒
        db.add_calendar_item("2026-09-11", "投简历", "09:30")

        due = db.list_calendar_due(datetime(2026, 9, 11, 21, 0))
        assert [x["content"] for x in due] == ["改论文"] or \
               [x["content"] for x in due] == ["投简历", "改论文"]

        before = datetime(2026, 9, 11, 8, 0)
        assert db.list_calendar_due(before) == [], "未到点不应提醒"

        # 标记已提醒 → 不再出现
        for x in db.list_calendar_due(datetime(2026, 9, 11, 23, 59)):
            db.mark_calendar_reminded(x["id"], "2026-09-11 22:00")
        assert db.list_calendar_due(datetime(2026, 9, 11, 23, 59)) == []
    finally:
        s.close()


def test_state_overdue_counts_today_only(data_root):
    """state_dict 的 overdue_count 只统计**今天**逾期（更早日期的逾期不进红点）。"""
    from datetime import timedelta
    from planner.session import _now

    s = PlannerSession(data_root, mock=True)
    try:
        now_ = _now()
        today = now_.strftime("%Y-%m-%d")
        yesterday = (now_ - timedelta(days=1)).strftime("%Y-%m-%d")
        s.db.add_calendar_item(yesterday, "昨天没做完", "20:00")
        s.db.add_calendar_item(today, "今天已过点", "00:00")
        s.db.add_calendar_item(today, "今天全天", None)      # 全天项 23:59 才算逾期
        ag = s.state_dict()["agenda"]
        assert ag["today_total"] == 2
        assert ag["overdue_count"] == 1, "红点只应统计今天的逾期"
        # 全量逾期查询（日历补提醒用）不受影响
        assert len(s.db.list_overdue_calendar(now_)) == 2
    finally:
        s.close()


def test_overdue_skip_reminded_for_nudge(data_root):
    """已到点提醒过的逾期项不再触发"逾期追问"（20:00 提醒 + 20:13 又问，用户实测）。

    红点统计（默认）仍算它逾期；只有调度线程的追问要跳过已提醒的。
    """
    from datetime import timedelta
    from planner.session import _now

    s = PlannerSession(data_root, mock=True)
    try:
        now_ = _now()
        yesterday = (now_ - timedelta(days=1)).strftime("%Y-%m-%d")
        iid = s.db.add_calendar_item(yesterday, "开会", "20:00")
        reminded = [i for i in s.db.list_overdue_calendar(now_) if i["id"] == iid]
        assert len(reminded) == 1
        s.db.mark_calendar_reminded(iid, yesterday + " 20:00")
        # 红点统计：提醒过但没勾掉，仍算逾期
        assert any(i["id"] == iid for i in s.db.list_overdue_calendar(now_))
        # 逾期追问：跳过已提醒的
        assert not any(i["id"] == iid
                       for i in s.db.list_overdue_calendar(now_, skip_reminded=True))
    finally:
        s.close()


# ── planner 工具 ────────────────────────────────────────────

def _tools(session):
    return {t.name: t for t in build_tools(session)}


def test_add_and_list_schedule_tools(data_root):
    """add_schedule / list_schedule：加日程带时刻，按范围列出。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = _tools(s)
        out = T["add_schedule"].invoke({"date": "2026-09-11", "content": "改论文", "time": "20:00"})
        assert "到点我会提醒你" in out, "带时刻的日程应承诺提醒"
        T["add_schedule"].invoke({"date": "2026-09-11", "content": "买牛奶", "kind": "task"})

        listed = T["list_schedule"].invoke({"start_date": "2026-09-11"})
        assert "改论文" in listed and "买牛奶" in listed
        assert "20:00" in listed and "全天" in listed
        assert "#1" in listed and "#2" in listed, "应给出可操作的 id"

        # 范围外查不到
        assert "没有任何日程" in T["list_schedule"].invoke({"start_date": "2026-09-20"})
    finally:
        s.close()


def test_add_schedule_validates_inputs(data_root):
    """非法日期/时刻要明确报错，而不是默默写库——尤其是"晚上8点"这类自然语言。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = _tools(s)
        assert "YYYY-MM-DD" in T["add_schedule"].invoke({"date": "明天", "content": "x"})
        # 自然语言时刻必须被打回并教会它用 HH:MM（旧路径是折算成心跳分钟数，会算错）
        r = T["add_schedule"].invoke({"date": "2026-09-11", "content": "x", "time": "晚上8点"})
        assert "HH:MM" in r and "20:00" in r
        assert T["add_schedule"].invoke({"date": "2026-09-11", "content": "x", "time": "25:00"})
        assert "HH:MM" in T["add_schedule"].invoke(
            {"date": "2026-09-11", "content": "x", "time": "99:99"})
        # 非法输入不落库
        assert s.db.list_calendar_range("2026-09-01", "2026-12-31") == []
    finally:
        s.close()


def test_update_and_cancel_schedule_tools(data_root):
    """update_schedule 只改传入的字段；cancel_schedule 软取消。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = _tools(s)
        T["add_schedule"].invoke({"date": "2026-09-11", "content": "改论文", "time": "20:00"})
        iid = s.db.list_calendar_range("2026-09-11", "2026-09-11")[0]["id"]

        # 改期，时刻与内容保持不变
        T["update_schedule"].invoke({"item_id": iid, "date": "2026-09-12"})
        it = s.db.get_calendar_item(iid)
        assert it["date"] == "2026-09-12" and it["time"] == "20:00"

        # 改成全天
        T["update_schedule"].invoke({"item_id": iid, "time": "全天"})
        assert s.db.get_calendar_item(iid)["time"] is None

        # 标完成 / 撤销完成
        T["update_schedule"].invoke({"item_id": iid, "status": "done"})
        assert s.db.get_calendar_item(iid)["status"] == "done"
        T["update_schedule"].invoke({"item_id": iid, "status": "todo"})
        assert s.db.get_calendar_item(iid)["status"] == "todo"

        # 取消
        assert "已取消" in T["cancel_schedule"].invoke({"item_id": iid})
        assert s.db.get_calendar_item(iid)["status"] == "cancelled"
        assert "找不到" in T["cancel_schedule"].invoke({"item_id": 999})
    finally:
        s.close()


# ── HTTP 端点 ──────────────────────────────────────────────

@pytest.fixture
def backend(data_root):
    session = PlannerSession(data_root, mock=True)
    httpd = create_server(session, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield session, base
    httpd.shutdown()
    session.close()


def _req(base, path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def test_calendar_endpoints(backend):
    """/calendar 查询 + /calendar/add + /calendar/update + /calendar/cancel。"""
    session, base = backend

    added = _req(base, "/calendar/add",
                 {"date": "2026-09-11", "content": "改论文", "time": "20:00"})
    assert added["ok"] and added["id"] > 0
    _req(base, "/calendar/add", {"date": "2026-09-11", "content": "买牛奶", "kind": "task"})

    got = _req(base, "/calendar?from=2026-09-11&to=2026-09-11")
    assert got["ok"] and len(got["items"]) == 2
    assert got["from"] == "2026-09-11" and got["to"] == "2026-09-11"

    # 范围外为空
    assert _req(base, "/calendar?from=2026-10-01&to=2026-10-02")["items"] == []

    iid = added["id"]
    upd = _req(base, "/calendar/update", {"id": iid, "date": "2026-09-12"})
    assert upd["ok"] and upd["item"]["date"] == "2026-09-12"
    assert upd["item"]["time"] == "20:00", "未传的字段不应被清空"

    assert _req(base, "/calendar/cancel", {"id": iid})["ok"]
    assert session.db.get_calendar_item(iid)["status"] == "cancelled"
    # 取消后默认查不到
    assert _req(base, "/calendar?from=2026-09-12&to=2026-09-12")["items"] == []


def test_calendar_endpoint_rejects_bad_input(backend):
    """非法输入返回 400，不落库。"""
    from urllib.error import HTTPError
    session, base = backend

    with pytest.raises(HTTPError) as e:
        _req(base, "/calendar/add", {"date": "明天", "content": "x"})
    assert e.value.code == 400

    with pytest.raises(HTTPError) as e:
        _req(base, "/calendar/add", {"date": "2026-09-11", "content": "x", "time": "晚上8点"})
    assert e.value.code == 400

    assert session.db.list_calendar_range("2026-09-01", "2026-12-31") == []


def test_calendar_edit_does_not_trigger_reply(backend):
    """用户在日历页操作 → 只静默告知，不触发一轮生成（否则每加一条就说一句）。"""
    session, base = backend
    _req(base, "/calendar/add", {"date": "2026-09-11", "content": "改论文", "time": "20:00"})
    # 静默告知：不应置 pending_response（trigger=False）
    assert session.pending_response is False
    # 但内容确实进了 buffer，下次调用时能看到
    assert any("改论文" in str(getattr(m, "content", "")) for m in session.recent_buffer)


# ── 到点提醒（调度器）───────────────────────────────────────

def _add_due(session, content, when: datetime, time_="10:00"):
    return session.db.add_calendar_item(when.strftime("%Y-%m-%d"), content, time_)


def test_calendar_due_fires_and_marks(data_root, monkeypatch):
    """到点的项：注入提醒 + 唤醒一次；标记后不再重复投递。"""
    s = PlannerSession(data_root, mock=True)
    try:
        fired: list[str] = []
        monkeypatch.setattr(s, "_spawn_worker", lambda trigger: fired.append(trigger))

        # 已过点的项（时间设在过去）
        iid = s.db.add_calendar_item("2020-01-01", "交房租", "10:00")
        s._check_calendar_due()

        assert fired == ["reminder"], "到点应唤醒一次"
        text = str(s.recent_buffer[-1].content)
        assert "交房租" in text and "原定" in text
        # 已标记 → 不再重复
        assert s.db.get_calendar_item(iid)["reminded_at"]
        s._check_calendar_due()
        assert fired == ["reminder"], "不应重复投递"
    finally:
        s.close()


def test_calendar_due_all_day_not_reminded(data_root, monkeypatch):
    """全天项不参与到点提醒；带时刻的照常投递（已无免打扰窗口拦截）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        fired: list[str] = []
        monkeypatch.setattr(s, "_spawn_worker", lambda trigger: fired.append(trigger))

        # 全天项：不提醒
        s.db.add_calendar_item("2020-01-01", "买牛奶", None)
        s._check_calendar_due()
        assert fired == [], "全天项不参与到点提醒"

        # 带时刻的到点项：照常投递
        s.db.add_calendar_item("2020-01-01", "交房租", "10:00")
        s._check_calendar_due()
        assert fired == ["reminder"], "到点项应投递提醒"
    finally:
        s.close()


# ── 重复系列 ────────────────────────────────────────────────

def test_add_schedule_with_repeat(data_root):
    """add_schedule(repeat=...) 生成重复系列；list_schedule 标出 ↻ 与规则。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = _tools(s)
        out = T["add_schedule"].invoke(
            {"date": "2026-09-16", "content": "吃药", "time": "08:00", "repeat": "daily"})
        assert "每天" in out and "共排了" in out, f"应说明重复规则与期数：{out}"

        listed = T["list_schedule"].invoke({"start_date": "2026-09-16",
                                            "end_date": "2026-09-18"})
        assert listed.count("吃药") == 3, "每天应覆盖 9/16~9/18 三天"
        assert "↻每天" in listed, "重复项应标出来（模型据此决定只改这次还是整个系列）"

        # 非法规则被打回
        bad = T["add_schedule"].invoke({"date": "2026-09-16", "content": "x",
                                        "repeat": "everyday"})
        assert "daily/weekly/weekdays" in bad
    finally:
        s.close()


def test_cancel_schedule_whole_series(data_root):
    """默认只取消这一次；whole_series=True 才连同今后未完成的一起取消。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = _tools(s)
        T["add_schedule"].invoke({"date": "2026-09-16", "content": "站会",
                                  "time": "09:30", "repeat": "weekdays"})
        ids = [r["id"] for r in s.db.list_calendar_range("2026-09-16", "2026-09-30")]
        assert len(ids) >= 5

        # 只取消这一次
        first = s.db.get_calendar_item(ids[0])
        out = T["cancel_schedule"].invoke({"item_id": ids[0]})
        assert "已取消" in out and "whole_series" in out, "应提示还有整个系列"
        assert s.db.get_calendar_item(ids[0])["status"] == "cancelled"
        assert s.db.get_calendar_item(ids[1])["status"] == "todo", "其它期不应受影响"

        # 取消整个系列（从某天起）
        out2 = T["cancel_schedule"].invoke({"item_id": ids[1], "whole_series": True})
        assert "已取消重复项" in out2
        rest = [r for r in s.db.list_calendar_range("2026-09-17", "2026-12-31")
                if r["series_id"] == first["series_id"]]
        assert rest == [], "今后所有未完成的期都应被取消"
    finally:
        s.close()


def test_update_schedule_whole_series(data_root):
    """whole_series 只改"规则型字段"（时刻/内容）；改期与标完成只能作用于这一次。"""
    s = PlannerSession(data_root, mock=True)
    try:
        T = _tools(s)
        T["add_schedule"].invoke({"date": "2026-09-16", "content": "吃药",
                                  "time": "08:00", "repeat": "daily"})
        iid = s.db.list_calendar_range("2026-09-16", "2026-09-16")[0]["id"]

        # 批量改时刻 + 内容
        out = T["update_schedule"].invoke({"item_id": iid, "time": "08:30",
                                           "content": "吃维生素", "whole_series": True})
        assert "已更新重复系列" in out
        later = s.db.list_calendar_range("2026-10-01", "2026-10-01")[0]
        assert later["time"] == "08:30" and later["content"] == "吃维生素", "今后各期应一起改"
        # 今天这一条也应改到（from_date=该条日期）
        assert s.db.get_calendar_item(iid)["time"] == "08:30"

        # 只改期：whole_series 无意义 → 明确拒绝而不是默默改一条
        out2 = T["update_schedule"].invoke({"item_id": iid, "date": "2026-09-20",
                                            "whole_series": True})
        assert "date/status 只能改这一次" in out2
        assert s.db.get_calendar_item(iid)["date"] == "2026-09-16", "不应被改掉"

        # 单次改期仍然可用
        assert "已更新日历项" in T["update_schedule"].invoke({"item_id": iid,
                                                              "date": "2026-09-20"})
        assert s.db.get_calendar_item(iid)["date"] == "2026-09-20"
    finally:
        s.close()


def test_calendar_series_endpoints(backend):
    """/calendar/add 带 repeat → 建系列；/calendar/series/cancel 取消今后各期。"""
    session, base = backend

    added = _req(base, "/calendar/add", {"date": "2026-09-16", "content": "吃药",
                                         "time": "08:00", "repeat": "daily"})
    assert added["ok"] and added["series_id"] and added["count"] > 1

    got = _req(base, "/calendar?from=2026-09-16&to=2026-09-18")
    assert len(got["items"]) == 3
    assert all(it["repeat"] == "daily" for it in got["items"]), "应带回重复规则供前端显示"
    assert all(it["series_id"] == added["series_id"] for it in got["items"])

    # 非法 repeat → 400
    from urllib.error import HTTPError
    with pytest.raises(HTTPError) as e:
        _req(base, "/calendar/add", {"date": "2026-09-16", "content": "x",
                                     "repeat": "hourly"})
    assert e.value.code == 400

    # 取消整个系列（从 9/17 起）
    iid = got["items"][1]["id"]
    r = _req(base, "/calendar/series/cancel", {"id": iid})
    assert r["ok"] and r["count"] > 0
    after = _req(base, "/calendar?from=2026-09-17&to=2026-10-31")["items"]
    assert after == [], "今后各期都该消失"
    # 9/16 那期不受影响
    assert len(_req(base, "/calendar?from=2026-09-16&to=2026-09-16")["items"]) == 1
