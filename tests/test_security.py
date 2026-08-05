"""安全审计测试：小助（LLM 可达面）没有任何修改文件/执行命令的能力。

保证面：
- 工具集：只读文件（read_file/list_dir）+ 任务/记忆业务，无 shell/exec/写文件
- 源码：tools.py 所有 open() 均为只读模式；无 subprocess/os.system/exec/eval
- 后端写操作仅限自身 data/（记忆树/日志/任务库），路径由代码固定、LLM 不可指定
"""

from pathlib import Path

from planner.tools import all_tool_schemas, build_tools

TOOLS_SRC = Path(__file__).resolve().parent.parent / "src" / "planner" / "tools.py"


def test_toolset_names_are_safe():
    """工具名不含 shell/exec/写文件等危险能力。"""
    names = [t["function"]["name"] for t in all_tool_schemas()]
    forbidden = ("shell", "exec", "command", "system", "write", "edit",
                 "delete", "remove", "move", "copy", "rename", "create_file")
    for n in names:
        for bad in forbidden:
            assert bad not in n.lower(), f"工具 {n} 疑似危险能力"
    assert {"read_file", "list_dir"} <= set(names)


def test_read_file_opens_readonly():
    """read_file 的 open() 只读：源码里只允许 'r'/'rb' 模式。"""
    src = TOOLS_SRC.read_text(encoding="utf-8")
    # 提取 read_file 定义段
    seg = src.split("def read_file", 1)[1].split("\n    return [", 1)[0]
    opens = [line for line in seg.splitlines() if "open(" in line]
    assert opens, "read_file 应含 open 调用"
    for line in opens:
        assert '"rb"' in line or '"r"' in line, f"非只读模式: {line.strip()}"
        assert '"w"' not in line and '"a"' not in line and '"x"' not in line


def test_tools_source_no_command_execution():
    """tools.py 无命令执行/代码执行能力。"""
    src = TOOLS_SRC.read_text(encoding="utf-8")
    for bad in ("subprocess", "os.system", "Popen", "exec(", "eval(",
                "os.remove", "os.rename", "os.makedirs", "write_text", "write_bytes"):
        assert bad not in src, f"tools.py 含 {bad}"


def test_read_only_behavior():
    """实测只读：读文件不改内容；工具不产生任何写操作。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.txt"
        p.write_text("原始内容", encoding="utf-8")
        s = type("S", (), {"push_log": lambda *a: None, "push_event": lambda *a: None,
                            "get_memory_tree": lambda self: None})()
        tools = {t.name: t for t in build_tools(s)}
        tools["read_file"].invoke({"path": str(p)})
        assert p.read_text(encoding="utf-8") == "原始内容", "读文件不应改动内容"
        assert list(Path(td).iterdir()) == [p], "不应产生新文件"
