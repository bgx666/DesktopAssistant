"""文件只读工具测试。"""

from planner.session import PlannerSession
from planner.tools import all_tool_schemas, build_tools


def _tools(session):
    return {t.name: t for t in build_tools(session)}


def test_read_file_utf8_and_line_chunks(data_root):
    """按行读取 + 行号前缀 + 续读提示（不整文件硬读）。"""
    p = data_root / "note.txt"
    p.write_text("\n".join(f"第{i}行内容" for i in range(1, 101)), encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        tools = _tools(s)
        r = tools["read_file"].invoke({"path": str(p), "offset": 1, "limit": 5})
        assert "共 100 行" in r and "本次显示第 1-5 行" in r
        assert "1| 第1行内容" in r and "5| 第5行内容" in r
        assert "如需继续可用 offset=6" in r
        # 从第 50 行续读
        r2 = tools["read_file"].invoke({"path": str(p), "offset": 50, "limit": 3})
        assert "本次显示第 50-52 行" in r2
        assert "50| 第50行内容" in r2 and "52| 第52行内容" in r2
        # offset 超出末尾
        r3 = tools["read_file"].invoke({"path": str(p), "offset": 999})
        assert "超出文件行数 100" in r3
    finally:
        s.close()


def test_read_file_long_line_truncated(data_root):
    """单行超长（压缩过的 js/一行大数据）→ 该行截断且提示，不撑爆上下文。"""
    p = data_root / "big-one-line.txt"
    p.write_text("x" * 5000 + "\n第二行", encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        r = _tools(s)["read_file"].invoke({"path": str(p)})
        assert "本行共 5000 字符，已截断" in r
        assert "2| 第二行" in r
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
        # limit 越界钳制（行数上限 1500，钳制后正常返回）
        txt = data_root / "t.txt"
        txt.write_text("内容\n" * 50, encoding="utf-8")
        r3 = tools["read_file"].invoke({"path": str(txt), "limit": 99999})
        assert "共 50 行" in r3
        # 空文件
        empty = data_root / "empty.txt"
        empty.write_text("", encoding="utf-8")
        r4 = tools["read_file"].invoke({"path": str(empty)})
        assert "空文件" in r4
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
