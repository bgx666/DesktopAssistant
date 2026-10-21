"""改动文件快捷块：聚合（次数降序 / missing 标记）+ /history 条目（挂在回复后）。"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from planner.session import PlannerSession


def _start_backend(session):
    from planner.server import create_server
    httpd = create_server(session, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_changed_files_for_ids_aggregates_and_sorts(data_root):
    """同一文件多条变更合并计数；按次数降序；missing 标记（留底标记 + 磁盘兜底）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        db = s.get_changes_db()
        ws = Path(data_root) / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        a, b, c = ws / "a.py", ws / "b.py", ws / "c.txt"
        a.write_text("xx", encoding="utf-8")
        b.write_text("y", encoding="utf-8")
        ida1 = db.record("write_file", str(a), "a.py", None, "x", before_missing=True)
        ida2 = db.record("edit_file", str(a), "a.py", "x", "xx")
        idb1 = db.record("write_file", str(b), "b.py", None, "y", before_missing=True)
        idc1 = db.record("delete_file", str(c), "c.txt", "z", None, after_missing=True)
        files = s.changed_files_for_ids([ida1, ida2, idb1, idc1])
        assert [f["rel"] for f in files] == ["a.py", "b.py", "c.txt"], "按改动次数降序"
        assert files[0]["count"] == 2 and files[0]["missing"] is False
        assert files[1]["count"] == 1 and files[1]["missing"] is False
        assert files[2]["missing"] is True, "删除的文件应标记 missing"
        assert files[2]["name"] == "c.txt"
        # 磁盘存在性兜底：记录后文件被（非留底方式，如 run_command）删掉 → 也标 missing
        b.unlink()
        files2 = s.changed_files_for_ids([idb1])
        assert files2[0]["missing"] is True
        # 空/无效 id 容错
        assert s.changed_files_for_ids([]) == []
        assert s.changed_files_for_ids([999999]) == []
    finally:
        s.close()


def test_history_emits_changed_files_after_reply(data_root):
    """buffer 里的 change_ids 聚合 → /history 在最终回复后面出 changed_files 条目。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    s = PlannerSession(data_root, mock=True)
    try:
        db = s.get_changes_db()
        f1 = Path(data_root) / "ws" / "resume.py"
        cid = db.record("write_file", str(f1), "resume.py", None, "print(1)",
                        before_missing=True)
        user = HumanMessage(content="[10:00] 张三对你说：改一下简历", id="u1")
        ai_call = AIMessage(content="", id="a1",
                            tool_calls=[{"name": "write_file", "id": "t1", "args": {}}])
        tool = ToolMessage(content="已写入", name="write_file", tool_call_id="t1",
                           id="t1m", additional_kwargs={"change_ids": [cid]})
        ai_final = AIMessage(content="改好了。", id="a2")
        s.recent_buffer = [user, ai_call, tool, ai_final]
        httpd, base = _start_backend(s)
        try:
            with urllib.request.urlopen(base + "/history", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        finally:
            httpd.shutdown()
        roles = [m["role"] for m in data["messages"]]
        assert "changed_files" in roles
        idx_assistant = roles.index("assistant")
        idx_changed = roles.index("changed_files")
        assert idx_changed == idx_assistant + 1, "快捷块应紧跟在最终回复后面"
        entry = data["messages"][idx_changed]
        assert entry["files"][0]["rel"] == "resume.py"
        assert entry["files"][0]["count"] == 1
        assert entry["files"][0]["path"] == str(f1)
    finally:
        s.close()


def test_history_carries_autonomous_turn_changes(data_root):
    """自主回合（注入消息触发）的改动不出块，累积到下一个用户消息边界统一出。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    s = PlannerSession(data_root, mock=True)
    try:
        db = s.get_changes_db()
        ws = Path(data_root) / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        fa, fb = ws / "auto.py", ws / "user.py"
        fa.write_text("x", encoding="utf-8")
        fb.write_text("y", encoding="utf-8")
        ca = db.record("write_file", str(fa), "auto.py", None, "x", before_missing=True)
        cb = db.record("write_file", str(fb), "user.py", None, "y", before_missing=True)
        buf = [
            HumanMessage(content="（系统：心跳时间到，可以自主做点事）", id="h1"),
            AIMessage(content="", id="a1",
                      tool_calls=[{"name": "write_file", "id": "t1", "args": {}}]),
            ToolMessage(content="已写入", name="write_file", tool_call_id="t1", id="t1m",
                        additional_kwargs={"change_ids": [ca]}),
            AIMessage(content="（没什么要说的，我继续学习。）", id="a2"),
            HumanMessage(content="[10:00] 张三对你说：帮我也改一个", id="u2"),
            AIMessage(content="", id="a3",
                      tool_calls=[{"name": "write_file", "id": "t2", "args": {}}]),
            ToolMessage(content="已写入", name="write_file", tool_call_id="t2", id="t2m",
                        additional_kwargs={"change_ids": [cb]}),
            AIMessage(content="改好了。", id="a4"),
        ]
        s.recent_buffer = buf
        httpd, base = _start_backend(s)
        try:
            with urllib.request.urlopen(base + "/history", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        finally:
            httpd.shutdown()
        roles = [m["role"] for m in data["messages"]]
        # 自主回合的改动块在用户消息**之前**（不挂自主回复后）
        i_cf, i_user = roles.index("changed_files"), roles.index("user")
        assert i_cf < i_user, "自主回合改动应在下一条用户消息之前统一出块"
        assert data["messages"][i_cf]["files"][0]["rel"] == "auto.py"
        # 玩家回合的改动块挂在最终回复后
        assert roles[-1] == "changed_files"
        assert data["messages"][-1]["files"][0]["rel"] == "user.py"
    finally:
        s.close()


def test_history_player_turn_heartbeat_end_flushes(data_root):
    """玩家回合以 heartbeat 收尾（调度下条消息）也是回合终点 → 改动块挂它后面。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    s = PlannerSession(data_root, mock=True)
    try:
        db = s.get_changes_db()
        ws = Path(data_root) / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        f = ws / "r.py"
        f.write_text("z", encoding="utf-8")
        cid = db.record("write_file", str(f), "r.py", None, "z", before_missing=True)
        buf = [
            HumanMessage(content="[10:00] 张三对你说：改一下", id="u1"),
            AIMessage(content="", id="a1",
                      tool_calls=[{"name": "write_file", "id": "t1", "args": {}}]),
            ToolMessage(content="已写入", name="write_file", tool_call_id="t1", id="t1m",
                        additional_kwargs={"change_ids": [cid]}),
            AIMessage(content="改好了。", id="a2",
                      tool_calls=[{"name": "heartbeat", "id": "hb",
                                   "args": {"minutes": 30}}]),
        ]
        s.recent_buffer = buf
        httpd, base = _start_backend(s)
        try:
            with urllib.request.urlopen(base + "/history", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        finally:
            httpd.shutdown()
        roles = [m["role"] for m in data["messages"]]
        assert roles[-1] == "changed_files", f"heartbeat 收尾应出块，实际 {roles}"
        assert data["messages"][-1]["files"][0]["rel"] == "r.py"
    finally:
        s.close()


def test_history_midturn_injections_do_not_split(data_root):
    """回合中途的注入（view_image 图片注入 / 历史遗留的 nudge 收尾提示——
    中间件已移除但旧 buffer 里还有）不算回合边界：改动仍聚合到玩家回合终点，
    一个回合只出一个块。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    s = PlannerSession(data_root, mock=True)
    try:
        db = s.get_changes_db()
        ws = Path(data_root) / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        fa, fb = ws / "a.py", ws / "b.py"
        fa.write_text("x", encoding="utf-8")
        fb.write_text("y", encoding="utf-8")
        ca = db.record("write_file", str(fa), "a.py", None, "x", before_missing=True)
        cb = db.record("write_file", str(fb), "b.py", None, "y", before_missing=True)
        buf = [
            HumanMessage(content="[10:00] 张三对你说：帮我改两个文件", id="u1"),
            AIMessage(content="", id="a1",
                      tool_calls=[{"name": "write_file", "id": "t1", "args": {}}]),
            ToolMessage(content="已写入", name="write_file", tool_call_id="t1", id="t1m",
                        additional_kwargs={"change_ids": [ca]}),
            AIMessage(content="第一个好了。", id="a2",
                      tool_calls=[{"name": "view_image", "id": "v1", "args": {}}]),
            HumanMessage(content="（这是小助读取的图片：x.png。请查看画面内容后继续。）",
                         id="inj"),
            AIMessage(content="", id="a3",
                      tool_calls=[{"name": "write_file", "id": "t2", "args": {}}]),
            ToolMessage(content="已写入", name="write_file", tool_call_id="t2", id="t2m",
                        additional_kwargs={"change_ids": [cb]}),
            HumanMessage(content="（你已经忙了一小会儿了，把最重要的事做完就歇一下吧。）",
                         id="nudge"),
            AIMessage(content="两个都改好了。", id="a4"),
        ]
        s.recent_buffer = buf
        httpd, base = _start_backend(s)
        try:
            with urllib.request.urlopen(base + "/history", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        finally:
            httpd.shutdown()
        cf = [m for m in data["messages"] if m["role"] == "changed_files"]
        assert len(cf) == 1, f"中途注入不得切块，实际 {len(cf)} 个块"
        assert {f["rel"] for f in cf[0]["files"]} == {"a.py", "b.py"}
        roles = [m["role"] for m in data["messages"]]
        assert roles[-1] == "changed_files", "块应在最终回复后（玩家回合）"
    finally:
        s.close()
