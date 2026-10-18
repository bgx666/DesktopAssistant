"""代码工作区：路径校验 + 权限规则存储（审批制地基，纯逻辑便于单测）。

- 工具分类：WRITE_TOOLS（需审批）/ READ_TOOLS（区内免审批）/ RUN_TOOLS（命令，逐条审批）
- resolve_target / is_within：路径归一化与边界判定
- is_blocked：黑名单（敏感文件 / .git / 系统目录）→ 硬拒，不弹审批
- blocked_command_reason：命令硬黑名单（递归删、格式化、下载即执行…）→ 硬拒，不弹审批
- tool_targets：从工具参数提取要判定的路径（move_file 两个，glob/grep 可留空）
- PermissionStore：data/permissions.json 的读写与匹配（「always」落规则；workspace 变更清空）

交互流程（挂起/事件/超时默认拒绝）在 middleware.ApprovalMiddleware。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from pathlib import Path

_logger = logging.getLogger("planner.workspace")

#: 写操作（新建/覆盖/移动/删除）——一律需要审批
WRITE_TOOLS = frozenset({"write_file", "edit_file", "create_dir", "move_file",
                         "delete_file", "apply_patch"})
#: 读操作（读取/列目录/检索）——区内免审批；工作区外按时设置决定是否走审批
READ_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep", "view_image"})
#: 命令执行——**逐条审批**（Ask 与 Auto 模式都要确认；只读模式拒绝）
RUN_TOOLS = frozenset({"run_command"})

#: 黑名单：文件名前缀（.env / .env.local / id_rsa / id_rsa.pub …）
_BLOCKED_NAME_PREFIXES = (".env", "id_rsa")
#: 黑名单：文件名后缀（证书/私钥）
_BLOCKED_NAME_SUFFIXES = (".key", ".pem")
#: 黑名单：任意层级的 .git 目录（config/credentials 常含凭据）
_BLOCKED_PART = ".git"

#: POSIX 系统目录（非 Windows 平台）
_POSIX_SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
                      "/System", "/Library")


def _system_dirs() -> list[Path]:
    """系统目录（集中一处，防误改）。"""
    dirs: list[Path] = []
    if os.name == "nt":
        for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            v = os.environ.get(env)
            if v:
                dirs.append(Path(v))
        dirs.append(Path(r"C:\$Recycle.Bin"))
    else:
        dirs.extend(Path(p) for p in _POSIX_SYSTEM_DIRS)
    return dirs


def resolve_target(base: Path | None, raw: str) -> Path:
    """模型给的路径 → 绝对路径（相对路径按 base＝当前工作目录解析）。"""
    text = str(raw or "").strip().strip('"').strip("'")
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = (base if base is not None else Path.cwd()) / p
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.abspath(str(p)))


def is_within(root: Path, target: Path) -> bool:
    """target 是否在 root 之内（含 root 本身）；Windows 下大小写不敏感。"""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        pass
    r = os.path.normcase(os.path.abspath(str(root))).rstrip("\\/")
    t = os.path.normcase(os.path.abspath(str(target)))
    return t == r or t.startswith(r + os.sep)


def rel_posix(root: Path, target: Path) -> str:
    """工作区相对路径（POSIX 形式；区外返回归一化绝对路径）。"""
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        pass
    r = os.path.normcase(os.path.abspath(str(root))).rstrip("\\/")
    t = os.path.normcase(os.path.abspath(str(target)))
    if t == r:
        return "."
    if t.startswith(r + os.sep):
        return t[len(r) + 1:].replace("\\", "/")
    return t.replace("\\", "/")


def is_blocked(target: Path) -> bool:
    """黑名单硬拒：敏感文件名 / .git 内部 / 系统目录。"""
    name = target.name.lower()
    if name.startswith(_BLOCKED_NAME_PREFIXES) or name.endswith(_BLOCKED_NAME_SUFFIXES):
        return True
    if _BLOCKED_PART in [p.lower() for p in target.parts]:
        return True
    for d in _system_dirs():
        try:
            if is_within(d, target):
                return True
        except OSError:
            continue
    return False


def tool_targets(name: str, args: dict, base: Path | None) -> list[Path] | None:
    """从工具参数提取要判定的路径列表（相对路径按 base 解析）；None = 参数缺失。"""
    if name == "move_file":
        src = str(args.get("src") or "").strip()
        dst = str(args.get("dst") or "").strip()
        if not src or not dst:
            return None
        return [resolve_target(base, src), resolve_target(base, dst)]
    if name == "apply_patch":
        # 多文件补丁：解析 V4A 里的 File/Move 路径（解析不出任何路径 → 交给工具自身报错）
        text = str(args.get("patch") or "")
        found = re.findall(
            r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$|^\*\*\* Move to:\s*(.+?)\s*$",
            text, re.MULTILINE)
        paths = [a or b for a, b in found if (a or b)]
        if not paths:
            return [base] if base is not None else None
        out: list[Path] = []
        for raw in paths[:20]:
            p = resolve_target(base, raw)
            if p not in out:
                out.append(p)
        return out
    if name == "view_image":
        raw = str(args.get("source") or "").strip()
        if not raw:
            return None
        return [resolve_target(base, raw)]
    raw = str(args.get("path") or "").strip()
    if name in ("glob", "grep"):
        if not raw:
            return [base] if base is not None else None
        return [resolve_target(base, raw)]
    if not raw:
        return None
    return [resolve_target(base, raw)]


def effective_cwd(workspace: Path | None, cwd_raw: str) -> Path | None:
    """当前工作目录：必须是存在的、工作区内的目录；否则回退工作区根（无工作区时 None）。"""
    if workspace is None:
        return None
    raw = str(cwd_raw or "").strip()
    if not raw:
        return workspace
    try:
        p = Path(raw).expanduser().resolve()
    except OSError:
        return workspace
    if p.is_dir() and is_within(workspace, p):
        return p
    return workspace


def latest_recent(recent: list[str] | None, path: str, keep: int = 8) -> list[str]:
    """把 path 置顶进「最近目录」（去重、大小写不敏感、截断上限）。"""
    s = str(path or "").strip()
    if not s:
        return list(recent or [])
    out = [r for r in (recent or [])
           if str(r or "").strip() and str(r).strip().lower() != s.lower()]
    out.insert(0, s)
    return out[:keep]


# ── 命令硬黑名单（命令能力能逃出工作区，破坏性命令直接拒，不弹审批）──
# 统一小写后正则匹配；宁可误伤提示用户手工执行，也不放行
_BLOCKED_COMMANDS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", "递归强制删除"),
    (r"\b(del|erase)\s+(/[a-z]\s+)*/[sfq]", "Windows 递归/强制删除"),
    (r"\b(rd|rmdir)\s+[^|]*?/s", "删除目录树"),
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区工具"),
    (r"\bshutdown\b", "关机/重启"),
    (r"\breg\s+(delete|add)\b", "改注册表"),
    (r"\btaskkill\b", "结束进程"),
    (r"\b(vssadmin|bcdedit|cipher\s+/w|takeown|icacls\s+[^|]*/grant)\b", "系统级操作"),
    (r"\b(mkfs|fdisk|dd\s+if=)", "磁盘写入"),
    (r"(curl|iwr|invoke-webrequest|wget)[^|]*\|\s*(iex|sh|bash|powershell|cmd)", "下载即执行"),
    (r"\b(invoke-expression|iex)\b", "动态执行代码"),
    (r"-encodedcommand|frombase64string", "混淆命令"),
    (r"\bgit\s+push\b[^|]*(--force|-f\b)", "强推远端"),
    (r"\bnet\s+user\b", "账号操作"),
)


def blocked_command_reason(command: str) -> str | None:
    """命令是否命中硬黑名单；命中返回原因（供提示），否则 None。"""
    low = str(command or "").lower()
    for pattern, why in _BLOCKED_COMMANDS:
        if re.search(pattern, low):
            return why
    return None


def command_prefix(command: str) -> str:
    """命令的「前缀模式」：首 token + ' *'（如 `pytest *`），供「记住并允许」使用。"""
    head = str(command or "").strip().split()
    if not head:
        return ""
    return head[0] + " *"


# ── 权限规则存储（data/permissions.json）──────────────────────

def _norm_rel(rel: str) -> str:
    """规则路径归一化：POSIX 分隔符、去 ./ 前缀。"""
    s = str(rel or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s


class PermissionStore:
    """「always」授权规则：按 (工具, 工作区相对路径) 匹配；workspace 变更时清空。"""

    def __init__(self, data_root: Path) -> None:
        self.path = Path(data_root) / "permissions.json"

    def load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            _logger.warning("[workspace] 清空权限规则失败")

    def save(self, rules: list[dict]) -> None:
        """整表覆写（规则管理 UI 删除单条后回写）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(list(rules or []), ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            _logger.warning("[workspace] 写入权限规则失败")

    def add(self, tool: str, rel_path: str, decision: str = "allow") -> None:
        rel = _norm_rel(rel_path)
        rules = self.load()
        if any(r.get("tool") == tool and _norm_rel(str(r.get("path_glob") or "")) == rel
               for r in rules):
            return
        rules.append({"tool": tool, "path_glob": rel, "decision": decision})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rules, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            _logger.warning("[workspace] 写入权限规则失败")

    def match(self, tool: str, rel_path: str) -> bool:
        """rel_path 是否命中该工具的一条 allow 规则（fnmatch，大小写不敏感）。"""
        rel = _norm_rel(rel_path).lower()
        if not rel:
            return False
        for r in self.load():
            if r.get("tool") != tool or r.get("decision") != "allow":
                continue
            pat = _norm_rel(str(r.get("path_glob") or "")).lower()
            if pat and fnmatch.fnmatch(rel, pat):
                return True
        return False

    # ── 命令规则（run_command 的「记住并允许」）────────────────

    def add_command(self, tool: str, cmd_glob: str, decision: str = "allow") -> None:
        """落一条命令规则（cmd_glob 如 `pytest *`；精确命令＝命令原文）。"""
        pat = str(cmd_glob or "").strip().lower()
        if not pat:
            return
        rules = self.load()
        if any(r.get("tool") == tool
               and str(r.get("cmd_glob") or "").strip().lower() == pat for r in rules):
            return
        rules.append({"tool": tool, "cmd_glob": pat, "decision": decision})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rules, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            _logger.warning("[workspace] 写入命令规则失败")

    def match_command(self, tool: str, command: str) -> bool:
        """命令是否命中 allow 规则（fnmatch；`pytest *` 也匹配无参数的 `pytest`）。"""
        cmd = str(command or "").strip().lower()
        if not cmd:
            return False
        for r in self.load():
            if r.get("tool") != tool or r.get("decision") != "allow":
                continue
            pat = str(r.get("cmd_glob") or "").strip().lower()
            if not pat:
                continue
            if fnmatch.fnmatch(cmd, pat):
                return True
            if pat.endswith(" *") and cmd == pat[:-2].strip():
                return True
        return False
