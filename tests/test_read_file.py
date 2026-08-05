"""文件只读工具测试。"""

from planner.session import PlannerSession
from planner.tools import all_tool_schemas, build_tools


def _tools(session):
    return {t.name: t for t in build_tools(session)}


def test_read_file_utf8_and_chunk(data_root):
    p = data_root / "note.txt"
    p.write_text("一二三四五六七八九十" * 100, encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        tools = _tools(s)
        r = tools["read_file"].invoke({"path": str(p), "start": 0, "limit": 20})
        assert "共 1000 字符" in r
        assert "本次显示 0-20" in r
        assert "一二三四" in r
        # 分段读取
        r2 = tools["read_file"].invoke({"path": str(p), "start": 20, "limit": 20})
        assert "本次显示 20-40" in r2
        assert "如需继续" in r2
    finally:
        s.close()


def test_read_file_gbk(data_root):
    p = data_root / "gbk.txt"
    p.write_bytes("中文内容测试".encode("gbk"))
    s = PlannerSession(data_root, mock=True)
    try:
        r = _tools(s)["read_file"].invoke({"path": str(p)})
        assert "中文内容测试" in r
    finally:
        s.close()


def test_read_file_rejections(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        tools = _tools(s)
        # 不存在
        r = tools["read_file"].invoke({"path": str(data_root / "nope.txt")})
        assert "无法访问" in r
        # 二进制
        binp = data_root / "bin.dat"
        binp.write_bytes(b"\x00\x01\x02\xffbinary")
        r2 = tools["read_file"].invoke({"path": str(binp)})
        assert "二进制" in r2 or "无法解码" in r2
        # limit 越界钳制验证
        txt = data_root / "t.txt"
        txt.write_text("内容" * 50, encoding="utf-8")
        r3 = tools["read_file"].invoke({"path": str(txt), "limit": 99999})
        assert "共 " in r3
    finally:
        s.close()


def test_list_dir(data_root):
    (data_root / "a.py").write_text("x", encoding="utf-8")
    (data_root / "sub").mkdir()
    s = PlannerSession(data_root, mock=True)
    try:
        r = _tools(s)["list_dir"].invoke({"path": str(data_root)})
        assert "a.py" in r and "sub/" in r
        r2 = _tools(s)["list_dir"].invoke({"path": str(data_root / "no_such")})
        assert "无法列出" in r2
    finally:
        s.close()


def test_toolset_has_no_write_tools(data_root):
    """工具集只读：不存在写文件/改文件相关工具。"""
    s = PlannerSession(data_root, mock=True)
    try:
        names = [t["function"]["name"] for t in all_tool_schemas()]
        write_hints = ("write", "edit", "delete", "create_file", "move", "copy", "remove")
        for n in names:
            for hint in write_hints:
                assert hint not in n, f"工具 {n} 疑似写操作"
        assert "read_file" in names and "list_dir" in names
    finally:
        s.close()
