"""安全审计测试：小助（LLM 可达面）的能力边界。

保证面（代码 Agent 改造后）：
- **无命令执行**：无 shell/exec/subprocess/os.system；无 os.remove/os.rename
- **写文件只经两处**：工作区代码工具（仅 workspace 启用时注册、审批制、黑名单兜底）
  与工具结果卸载（data/tool_results/）；默认（无 workspace）工具集无任何写工具
- 源码：build_tools 的 read_file 段所有 open() 均为只读模式
- 后端写操作仅限自身 data/（记忆树/日志/任务库），路径由代码固定、LLM 不可指定
"""

from pathlib import Path

from planner.tools import all_tool_schemas, build_tools

TOOLS_SRC = Path(__file__).resolve().parent.parent / "src" / "planner" / "tools.py"


def test_toolset_names_are_safe():
    """默认工具集（无工作区）不含 shell/exec/写文件等危险能力。"""
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
    """tools.py 不出现 os.system / exec / eval 这类"无门禁"执行；命令走审批工具。

    注：`run_command` 是**审批制**命令工具（默认不注册、逐条审批、破坏性命令硬拒），
    它用 subprocess 是设计内的——这里保证其它"裸执行"入口不存在。
    """
    src = TOOLS_SRC.read_text(encoding="utf-8")
    for bad in ("os.system", "os.popen", "exec(", "eval(",
                "os.remove", "os.rename", "os.makedirs"):
        assert bad not in src, f"tools.py 含 {bad}"
    # write_bytes 只允许出现在 show_image 的图片缓存写入（网络下载缓存 + 本地
    # 展示持久副本，均落 data/images；content-type/大小白名单 + /file 只服务
    # 工作区/缓存目录，另见 test_show_image.py）
    for i, line in enumerate(src.splitlines(), 1):
        if "write_bytes" in line:
            assert "dest.write_bytes" in line, f"tools.py:{i} 出现计划外的 write_bytes"
    assert "def build_code_tools" in src and "def _offload_if_large" in src
    assert "def run_command" in src and "def _kill_process_tree" in src


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
