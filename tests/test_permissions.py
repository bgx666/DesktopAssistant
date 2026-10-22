from pathlib import Path

from planner.middleware import ApprovalMiddleware
from planner.permissions import PermissionDb
from planner.session import PlannerSession


def test_recursive_grants_merge_and_remove(data_root, tmp_path):
    root = tmp_path / "projects"
    child = root / "alpha"
    child.mkdir(parents=True)
    db = PermissionDb(data_root)

    first = db.add_read(str(child), source="user")
    assert first["ok"] and not first["merged"]
    assert db.has_read(child / "a.py")

    # 授权父目录：子目录已被授权 → 列表合并成父目录，覆盖计数 1
    parent = db.add_read(str(root), source="user")
    assert parent["ok"] and not parent["merged"]
    effective = db.list_effective()
    assert [g["path"] for g in effective] == [str(root.resolve())]
    assert effective[0]["covered_count"] == 1
    assert db.has_read(child / "deep" / "b.py")

    # 再授权子目录：已被父目录覆盖 → 合并不新增行
    again = db.add_read(str(child), source="assistant")
    assert again["ok"] and again["merged"]
    assert len(db.all_grants()) == 2

    # 删除父目录：之前显式子授权仍在，重新成为顶层
    parent_id = effective[0]["id"]
    assert db.remove(parent_id)
    effective = db.list_effective()
    assert [g["path"] for g in effective] == [str(child.resolve())]
    assert db.has_read(child / "a.py")


def test_permission_db_rejects_missing_or_blocked(data_root, tmp_path):
    db = PermissionDb(data_root)
    assert not db.add_read("", source="user")["ok"]
    assert not db.add_read(str(tmp_path / "missing"), source="user")["ok"]
    system = Path(r"C:\Windows") if Path(r"C:\Windows").exists() else None
    if system is not None:
        r = db.add_read(str(system), source="user")
        assert not r["ok"] and r["error"] == "blocked"


def test_read_grant_allows_outside_and_write_stays_workspace(data_root, tmp_path):
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "a.py").write_text("x", encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(workspace),
                           "code_read_outside_workspace": False})
        mw = ApprovalMiddleware(s)
        verdict, msg = mw._authorize("read_file", {"path": str(outside / "a.py")})
        assert verdict == "deny" and "未授权" in msg

        s.get_permission_db().add_read(str(outside), source="user")
        verdict, _ = mw._authorize("read_file", {"path": str(outside / "a.py")})
        assert verdict == "allow"

        verdict, msg = mw._authorize("write_file", {"path": str(outside / "b.py")})
        assert verdict == "deny" and "写权限只允许" in msg
    finally:
        s.close()


def test_request_read_permission_tool_writes_grant(data_root, tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    outside = tmp_path / "docs"
    workspace.mkdir()
    outside.mkdir()
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(workspace),
                           "code_read_outside_workspace": False})
        seen = []

        def fake_approval(tool, paths, preview="", **kw):
            seen.append(tool)
            return "always_dir"

        monkeypatch.setattr(s, "request_approval", fake_approval)
        from planner.tools import build_tools
        tools = {t.name: t for t in build_tools(s)}
        tool = tools["request_read_permission"]
        out = tool.invoke({"path": str(outside), "reason": "看项目文档"})
        assert "已授权只读目录" in out
        assert seen == ["request_read_permission"]
        assert s.get_permission_db().has_read(outside)
        grants = s.get_permission_db().list_effective()
        assert grants and Path(grants[0]["path"]) == outside.resolve()
    finally:
        s.close()


def test_approval_always_dir_adds_read_grant(data_root, tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "a.py"
    target.write_text("x", encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(workspace),
                           "code_read_outside_workspace": True})
        monkeypatch.setattr(s, "request_approval",
                            lambda *a, **k: "always_dir")
        verdict, _ = ApprovalMiddleware(s)._authorize(
            "read_file", {"path": str(target)})
        assert verdict == "allow"
        assert s.get_permission_db().has_read(target)
        assert s.get_permission_db().list_effective()[0]["path"] == str(outside.resolve())
    finally:
        s.close()
