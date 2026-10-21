"""进程内工具注册表（langchain @tool 工厂，参照 yaya backend 选型）。

工具集：任务录入/拆解/跟进/改期 + 记忆树翻阅 + heartbeat（定时/巡检）
+ 文件读取 + 代码工作区读写/检索（仅 workspace_root 非空时注册）。
输入模型用 pydantic（多参数 + 枚举/范围约束），闭包捕获 session。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config as _config
from .store.calendar_db import CalendarDb

_logger = logging.getLogger("planner.tools")

_TZ = timezone(timedelta(hours=8))

#: show_image / view_image 共用的图片格式与大小白名单
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
IMAGE_MAX_BYTES = 15 * 1024 * 1024

TASK_STATUSES = ("todo", "in_progress", "done", "abandoned")


def _today() -> str:
    return date.today().isoformat()


def _now_str() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")


# ── 输入模型 ──────────────────────────────────────────────────







class HeartbeatInput(BaseModel):
    """heartbeat 输入：下一次定时唤醒的时间（二选一）+ 可选的取消开关。"""
    minutes: float | None = Field(
        default=None,
        description="多少分钟后醒来（巡检/定时检查用，最大 720 分钟）")
    seconds: float | None = Field(
        default=None,
        description="多少秒后醒来（过一小会儿要回来看看时用；最短 1 分钟 = 60 秒）")
    note: str = Field(default="", description="用户当时的嘱托 / 下次醒来要看什么（取消时忽略）")
    cancel: bool = Field(
        default=False,
        description="true = 取消已排定的下一次唤醒（用户说「不用提醒了 / 算了 / 已经好了」时用）")

    @model_validator(mode="after")
    def require_one(self):
        if self.cancel:
            return self   # 取消不需要间隔
        if self.minutes is None and self.seconds is None:
            raise ValueError("minutes 与 seconds 至少填一个：短间隔填 seconds，长间隔填 minutes")
        return self


class AskUserInput(BaseModel):
    question: str = Field(description="要问用户的问题（一句话，≤200 字）")
    options: list[str] = Field(
        default_factory=list,
        description="给用户的候选答案（1~4 个、每个 ≤40 字）；用户也可以自己打字回答")


# ── 工具工厂 ──────────────────────────────────────────────────

def build_tools(session) -> list[BaseTool]:
    """闭包捕获 session 的工具工厂。session 可为 None（仅生成 schema 用）。"""
    db: CalendarDb | None = getattr(session, "db", None)

    def _require_db():
        if db is None:
            return "（日历库不可用）"
        return None





    @tool(args_schema=HeartbeatInput)
    def heartbeat(minutes: float | None = None, note: str = "",
                  seconds: float | None = None, cancel: bool = False) -> str:
        """设置/修改/取消「下一次定时唤醒」（**只有用户明确要求时才用**），然后结束本轮。

        心跳是**用户授权的定时器/巡检**（2026-10-19 起）：
        - **用户让你"过 X 分钟/半小时后做某事、盯着某任务看进度"** → 设一个间隔；
        - **普通对话结束不要调用它**（不会被唤醒，等用户开口即可）；
        - **不要用它主动搭话/稍后主动提起/自主学习**——这些行为已移除。

        巡检循环：醒来后会带着用户当时的嘱托；你去检查/做事——**做完了就正常
        收尾（不要再设心跳）**；**还没完成**（在等用户、等外部状态）→ 再调用
        本工具设下一次，`note` 写明「还没完成什么、下次醒来要看什么」。

        **修改/取消**：用户说"不用提醒了 / 算了 / 已经好了" → 传 `cancel=true`
        取消已排定的唤醒；要改时间/改嘱托 → 直接再调一次本工具（新设定会覆盖旧的，
        不用先取消）。

        定时提醒（钟点，如"晚上 8 点提醒我"）用日历 `add_schedule`（time 填
        "20:00"）——到点系统自己叫，不占心跳；本工具只用于间隔性的定时/巡检
        （`seconds` 适合很短的重查，`minutes` 适合几十分钟到 12 小时）。"""
        if cancel:
            had = session.cancel_heartbeat()
            return ("（已取消下一次定时唤醒。）" if had
                    else "（当前没有排定的定时唤醒。）")
        m = float(minutes) if minutes is not None else float(seconds or 0.0) / 60.0
        session.set_heartbeat_state(m, str(note))
        return f"（我 {session._fmt_duration(session._heartbeat_minutes)}后再醒，{note or '到点再醒'}。）"

    @tool(parse_docstring=True)
    def continue_speaking() -> str:
        """需要分点、分段描述时调用本工具：每调用一次，会暂停片刻，然后继续说下一点（一句一句地说）。

        用法：每说一点之前调用一次；说完这一点、还想继续说下一点时再调用。
        所有点都说完了就不要再调用，正常收尾（如调用 heartbeat 结束本轮）。
        不要为了一句普通的话调用它。"""
        session.pause_before_continue()
        return "（收到，继续。请接着说下一点。）"

    @tool(args_schema=AskUserInput)
    def ask_user(question: str, options: list[str] | None = None) -> str:
        """向用户弹窗提问：需要他拍板、做选择、给关键信息时用，弹窗里选选项或直接打字。

        什么时候用：**决定权在用户手里**且你不想瞎猜的时候——二选一的方案、要不要执行、
        用哪个文件/哪个名字、参数取值、需要用户提供的信息。比"我在对话里问一句"更醒目，
        场上有专门的弹窗，用户必须看见。
        什么时候不用：你自己能判断的（直接做）；用户刚刚已经说过的（别重复确认）；
        模糊寒暄式的"你觉得呢"（那就别问）。
        options 给 1~4 个候选，短而互斥（用户也可以无视选项自己打字）；
        用户超时没答就继续做别的，不要反复追问。"""
        return session.ask_user(question, options or [])


    # ── 日历（日程 / 提醒）──────────────────────────────────────
    # 这是**唯一**的待办概念：按时间排的日程与提醒，用户可见、可自己编辑，
    # 模型也能读写。不再有"任务/推进队列"那套并行概念。
    # 关键：带 time 的项**到点会真的提醒**（调度线程扫描），所以用户说"晚上 8 点
    # 提醒我"时直接写 HH:MM，**不要再折算成心跳分钟数**——那是旧路径，会算错、
    # 而且心跳是单槽位、多件事会互相覆盖。

    import re as _re_cal

    def _norm_date(raw: str) -> str | None:
        s = str(raw or "").strip()
        if not _re_cal.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return None
        try:
            date.fromisoformat(s)
        except ValueError:
            return None
        return s

    def _norm_time(raw: str) -> str | None:
        """HH:MM 归一化（接受 9:05 / 09:05；非法返回 None）。"""
        s = str(raw or "").strip()
        m = _re_cal.fullmatch(r"(\d{1,2}):(\d{2})", s)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        return f"{h:02d}:{mi:02d}"

    @tool(parse_docstring=True)
    def list_schedule(start_date: str, end_date: str = "") -> str:
        """查看某个日期范围内的日历日程与待办（含时刻、状态、id）。

        问"接下来几天有什么安排""这周还有什么没做"时用。闭区间；
        end_date 留空就只看 start_date 当天。

        Args:
            start_date: 起始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD（留空＝只看当天）
        """
        err = _require_db()
        if err:
            return err
        start = _norm_date(start_date)
        if start is None:
            return f"日期格式应为 YYYY-MM-DD，收到 {start_date!r}"
        end = _norm_date(end_date) if str(end_date or "").strip() else start
        if end is None:
            return f"日期格式应为 YYYY-MM-DD，收到 {end_date!r}"
        if end < start:
            start, end = end, start
        items = db.list_calendar_range(start, end)
        if not items:
            return f"{start} ~ {end} 没有任何日程或待办。"
        lines = [f"{start} ~ {end} 共 {len(items)} 项："]
        for it in items:
            mark = {"done": "[已完成]", "cancelled": "[已取消]"}.get(it["status"], "")
            kind = "日程" if it["kind"] == "event" else "任务"
            detail = f"（{it['detail']}）" if it["detail"] else ""
            # 重复项标出来：模型据此判断用户说的"取消/改"是指这一次还是整个系列
            rep = ""
            if it.get("series_id"):
                label = {"daily": "每天", "weekly": "每周", "weekdays": "工作日"}.get(
                    it.get("repeat") or "", "重复")
                rep = f" ↻{label}"
            lines.append(f"  #{it['id']} {it['date']} {it['time'] or '全天'} "
                         f"[{kind}]{mark}{rep} {it['content']}{detail}")
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def add_schedule(date: str, content: str, time: str = "", kind: str = "event",
                     detail: str = "", repeat: str = "") -> str:
        """往日历加一条日程或待办。用户说"记一下/提醒我/安排/加到日历"时用。

        用户给了明确时刻（"晚上 8 点提醒我"）→ 填 time；只是当天要做、没指定几点
        → 留空（全天项）。**直接把时刻写成 HH:MM，不要折算成分钟数。**

        **要重复的用 repeat**（"每天/每周/工作日"），别一条条加：
        - "每天八点吃药" → repeat="daily"
        - "每周三开会" → date 填**最近的某个周三**，repeat="weekly"（按 date 的星期几）
        - "工作日九点半站会" → repeat="weekdays"

        Args:
            date: 日期 YYYY-MM-DD（重复项＝起始日）
            content: 做什么
            time: 时刻 HH:MM，留空＝全天
            kind: event（日程）/ task（当天任务）
            detail: 备注（可选）
            repeat: 留空＝一次性；daily（每天）/ weekly（每周同一天）/ weekdays（工作日）
        """
        err = _require_db()
        if err:
            return err
        d = _norm_date(date)
        if d is None:
            return f"日期格式应为 YYYY-MM-DD，收到 {date!r}"
        if not str(content or "").strip():
            return "内容不能为空。"
        t = None
        if str(time or "").strip():
            t = _norm_time(time)
            if t is None:
                return f"时刻格式应为 HH:MM（如 20:00），收到 {time!r}"
        if kind not in ("event", "task"):
            return f"kind 必须是 event/task，收到 {kind!r}"
        rule = str(repeat or "").strip().lower()
        if rule and rule not in ("daily", "weekly", "weekdays"):
            return f"repeat 只能是 daily/weekly/weekdays（或留空），收到 {repeat!r}"
        if rule:
            sid, cnt = db.add_calendar_series(d, str(content).strip(), rule, t, kind,
                                              str(detail or ""))
            if not sid:
                return "重复规则没有算出任何日期，请检查起始日。"
            label = {"daily": "每天", "weekly": "每周同一天", "weekdays": "工作日"}[rule]
            session.push_log(f"日历 +{d} {t or '全天'}（{label}）：{str(content).strip()}")
            return (f"已加入日历（{label}，从 {d} 起，共排了 {cnt} 期，之后会自动续）："
                    f"{t or '（全天）'}「{str(content).strip()}」"
                    + ("，到点我会提醒你。" if t else "。"))
        iid = db.add_calendar_item(d, str(content).strip(), t, kind, str(detail or ""))
        session.push_log(f"日历 +{d} {t or '全天'}：{str(content).strip()}")
        return (f"已加入日历 #{iid}：{d} {t or '（全天）'}「{str(content).strip()}」"
                + ("，到点我会提醒你。" if t else "。"))

    @tool(parse_docstring=True)
    def update_schedule(item_id: int, date: str = "", time: str = "", content: str = "",
                        detail: str = "", status: str = "", whole_series: bool = False) -> str:
        """修改日历里的一项：改期 / 改时刻 / 改内容 / 标完成或撤销完成。

        只传要改的字段，其余留空保持不变。**取消请用 cancel_schedule。**

        这一项属于重复系列（list_schedule 会标出来）时：
        - 默认**只改这一次**
        - 想改**今后所有还没做的**（改时间、改内容）→ whole_series=True
          （注意：改"日期"和标"完成"永远只作用于这一项，那是历史不是规则）

        Args:
            item_id: 项目 id（list_schedule 返回的 #id）
            date: 新日期 YYYY-MM-DD（不改留空）
            time: 新时刻 HH:MM；传"全天"表示去掉时刻（不改留空）
            content: 新内容（不改留空）
            detail: 新备注（不改留空）
            status: todo（未完成）/ done（已完成）——标完成、撤销完成用
            whole_series: true＝把这次改动应用到该系列今后所有未完成的项
        """
        err = _require_db()
        if err:
            return err
        it = db.get_calendar_item(int(item_id))
        if it is None:
            return f"找不到日历项 #{item_id}。"
        fields: dict = {}
        if str(date or "").strip():
            d = _norm_date(date)
            if d is None:
                return f"日期格式应为 YYYY-MM-DD，收到 {date!r}"
            fields["date"] = d
        raw_time = str(time or "").strip()
        if raw_time:
            if raw_time in ("全天", "all-day", "-"):
                fields["time"] = None
            else:
                t = _norm_time(raw_time)
                if t is None:
                    return f"时刻格式应为 HH:MM（如 20:00），收到 {time!r}"
                fields["time"] = t
        if str(content or "").strip():
            fields["content"] = str(content).strip()
        if str(detail or "").strip():
            fields["detail"] = str(detail).strip()
        if str(status or "").strip():
            st = str(status).strip()
            if st not in ("todo", "done"):
                return f"status 必须是 todo/done，收到 {status!r}（取消请用 cancel_schedule）"
            fields["status"] = st
        if not fields:
            return "没有要修改的字段。"

        # 整个系列：只有"规则型字段"能批量改；日期/完成状态是这一次的事
        if whole_series:
            sid = it.get("series_id")
            if not sid:
                return f"#{item_id} 不是重复项，whole_series 无意义（已按单次处理）。"
            batch = {k: v for k, v in fields.items() if k in ("time", "content", "detail", "kind")}
            if not batch:
                return (f"#{item_id} 属于重复系列，但 date/status 只能改这一次"
                        f"（要改期或标完成，请对具体那一条操作）。")
            n = db.update_calendar_series(sid, from_date=it["date"], **batch)
            return (f"已更新重复系列（{it['date']} 起、未完成的 {n} 项）："
                    f"{'、'.join(batch)}。")
        if not db.update_calendar_item(int(item_id), **fields):
            return f"修改 #{item_id} 失败。"
        return f"已更新日历项 #{item_id}（{'、'.join(fields)}）。"

    @tool(parse_docstring=True)
    def cancel_schedule(item_id: int, whole_series: bool = False) -> str:
        """取消日历里的一项（用户说"不去了/取消掉/这个不做了"时用）。

        软取消：保留痕迹，不再出现在日历与提醒里。
        这一项属于重复系列时，默认**只取消这一次**；用户说"以后都不去了/别再排了"
        → whole_series=True（取消该系列从这天起、今后所有未完成的）。

        Args:
            item_id: 项目 id（list_schedule 返回的 #id）
            whole_series: true＝连同该重复系列今后所有未完成的项一起取消
        """
        err = _require_db()
        if err:
            return err
        it = db.get_calendar_item(int(item_id))
        if it is None:
            return f"找不到日历项 #{item_id}。"
        sid = it.get("series_id")
        if whole_series and sid:
            n = db.cancel_calendar_series(sid, from_date=it["date"])
            session.push_log(f"日历 -系列（{n} 项）：{it['content']}")
            return (f"已取消重复项「{it['content']}」自 {it['date']} 起、"
                    f"今后未完成的 {n} 期；之前已完成的不动。")
        if not db.cancel_calendar_item(int(item_id)):
            return f"取消 #{item_id} 失败。"
        session.push_log(f"日历 -#{item_id}：{it['content']}")
        tail = "（它属于重复系列，今后各期仍在——要全取消请传 whole_series。）" if sid else ""
        return (f"已取消 #{item_id}「{it['content']}」（{it['date']} {it['time'] or '全天'}）。"
                + tail)



    @tool(parse_docstring=True)
    def explore_memory_tree(node_id: str = "") -> str:
        """翻阅记忆树，逐层下钻找回过去对话的具体内容。回忆往事、确认约定/决策时用。

        记忆树是**自下而上生长的摘要树**：最底层 `node0_xxx` 最**具体**（展开是
        当时的**原始对话**），越往上越**抽象**（node1 是多个 node0 的合并、node2
        更浓缩）。**上下文里的 node 消息就是树当前的顶层节点**（压缩替换时放进
        来的，带条数/时间范围）——翻树从它们开始。

        展开一个节点 X = 返回 X **下一层**所有子节点的摘要（每个都带条数/时间
        范围），由你判断该往哪支钻；逐层下钻到 `node0_xxx` 叶子时，返回该叶子
        覆盖范围的**原始对话**。这是一个循环（通常 2~4 次调用，**查一次没找到
        别放弃**）：没找到就换分支、换时间段再试。返回"找不到节点"是**编号问题**
        （拼错/已被合并的旧编号），不是"记忆里没有"——改用上下文里的 node 编号
        再试。

        Args:
            node_id: 要展开的节点编号（node0_xxx / node1_xxx / node2_xxx …）。
                     留空或传 "root"/"根" 时返回**整树顶层概览**（记不清有哪些
                     节点时的备用视图）。
        """
        tree = session.get_memory_tree()
        if not node_id or node_id.strip().lower() in ("root", "根"):
            overview = tree.get_root_overview()
            if not overview:
                return "记忆树还没有节点（对话尚未触发压缩）。"
            lines = [f"记忆树顶层概览（共 {len(overview)} 支；这些就是上下文里的"
                     "顶层节点，越往下展开越具体，node0 叶子展开是当时的原文）："]
            for n in overview:
                span = ""
                if n.get("round_start") is not None:
                    span = f"，第{n['round_start']}-{n['round_end']}条"
                tr = ""
                if n.get("time_start") and n.get("time_end"):
                    tr = f"，{n['time_start']} ~ {n['time_end']}"
                lines.append(f"- {n['node_id']}（L{n['level']}{span}{tr}）：{n['summary']}")
            lines.append('选一支展开：explore_memory_tree("nodeX_xxx")')
            return "\n".join(lines)
        info = tree.get_node_children_info(node_id)
        if info is None:
            return (f"找不到节点 {node_id}——该编号不存在（可能拼错或来自已被合并的"
                    "旧消息），不是记忆里没有。留空调用看顶层概览，或改用上下文里"
                    "出现过的 node 编号再试。")
        # 大输出（叶子 details 含原文）落盘，上下文只留预览 + 指针
        return _offload_if_large(json.dumps(info, ensure_ascii=False), session,
                                 "explore_memory_tree")

    # ── 文件读取（只读；本工具集不存在任何写文件工具）──────────

    FILE_MAX_BYTES = 50 * 1024 * 1024  # 超过该大小的文件拒绝读取

    @tool(parse_docstring=True)
    def list_dir(path: str) -> str:
        """查看一个目录下有什么（文件和子目录），探索用户电脑上的文件用。
        只读操作；启用代码工作区后，工作区外的目录需要用户审批。

        Args:
            path: 目录绝对路径，如 D:\\project 或 C:\\Users\\用户名\\Documents
        """
        import os
        try:
            entries = os.listdir(path)
        except (OSError, NotADirectoryError) as exc:
            return f"无法列出目录：{exc}"
        files, dirs = [], []
        for name in sorted(entries):
            try:
                if os.path.isdir(os.path.join(path, name)):
                    dirs.append(name + "/")
                else:
                    files.append(name)
            except OSError:
                continue
        lines = [f"{path}（{len(dirs)} 个目录，{len(files)} 个文件）："]
        if dirs:
            lines.append("目录：" + "、".join(dirs[:50]))
        if files:
            lines.append("文件：" + "、".join(files[:50]))
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
        """读取电脑上的文本文件（只读，**按行读、带行号**，不能修改任何文件）。

        大文件**按需精读**：先用 `grep` 定位到行号，再用 offset/limit 只读那一片段
        （不要整文件硬读）。输出每行带行号前缀（如 `42| ...`），行号可直接用于
        继续读（offset）或对照 edit_file 的上下文。
        启用代码工作区后，工作区外的文件需要用户审批（敏感文件在保护名单内）。

        Args:
            path: 文件绝对路径，如 D:\\project\\main.py
            offset: 从第几行开始读（1 起始），默认 1
            limit: 本次最多读取的行数（1~1500），默认 400
        """
        import os
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return f"无法访问文件：{exc}"
        if size > FILE_MAX_BYTES:
            return f"文件过大（{size / 1024 / 1024:.0f}MB），拒绝读取。"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            return f"无法读取文件：{exc}"
        # 二进制探测（null 字节特征）
        if b"\x00" in data[:2048]:
            return "这是二进制文件，无法读取。"
        text = None
        for enc in ("utf-8", "gbk"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = data.decode("latin-1")   # 单字节兜底（不会失败）
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()                     # 末尾换行不算额外一行（与编辑器一致）
        total = len(lines)
        if total == 0:
            return f"{path}（空文件，0 行）"
        offset = max(1, int(offset or 1))
        limit = max(1, min(1500, int(limit or 400)))
        if offset > total:
            return f"（offset={offset} 超出文件行数 {total}，已到结尾；文件共 {total} 行）"
        start = offset
        end = min(total, start + limit - 1)
        width = len(str(end))
        out: list[str] = []
        used = 0
        last = start - 1
        for num in range(start, end + 1):
            line = lines[num - 1].rstrip("\r")
            if len(line) > 2000:            # 单行超长（压缩过的 js/一行大数据）防爆
                line = line[:2000] + f" …（本行共 {len(line)} 字符，已截断）"
            row = f"{str(num).rjust(width)}| {line}"
            if used + len(row) > 60000:     # 总输出预算兜底
                end = num - 1
                break
            out.append(row)
            used += len(row) + 1
            last = num
        head = f"{path}（共 {total} 行，本次显示第 {start}-{last} 行）"
        if last < total:
            head += f"，如需继续可用 offset={last + 1}"
        return head + "：\n" + "\n".join(out)

    @tool(parse_docstring=True)
    def capture_screen() -> str:
        """截取当前主屏的屏幕截图，让模型查看屏幕内容。

        用户让你"看一下屏幕 / 看看我正在做什么 / 屏幕上有什么"时使用。
        视觉模型可用时：截图以 user 消息注入对话（画面进入上下文，可反复查看，
        与对话共用前缀缓存）；视觉不可用或失败时回退 OCR 只识别文字。
        """
        import mss

        try:
            with mss.MSS() as sct:
                shot = sct.grab(sct.monitors[1])   # 主屏
        except Exception as exc:
            return f"（屏幕截图失败：{exc}）"
        # 视觉路径：图片块仅限 user 消息，工具结果不能带图——
        # 截图存入 _pending_screenshots，由 ScreenShotInjectMiddleware 在下一轮
        # 模型调用前注入为 user 消息（进对话上下文，可反复看、共享前缀缓存）
        if session is not None and session.vision_capable:
            from .imageutil import numpy_to_data_url
            url = numpy_to_data_url(shot)
            if url:
                session._pending_screenshots.append({
                    "url": url,
                    "text": "（这是屏幕截图，请查看画面内容后回答。）"})
                return "（屏幕截图已截取，画面已注入本次对话，请查看截图后回答。）"
            _logger.warning("[screen] 截图转 data URL 失败，回退 OCR")
        # 回退：OCR 文字识别（无视觉模型 / 转换失败）
        from .ocr import ocr_png_from_screen
        text = ocr_png_from_screen(shot)
        if not text:
            return "（屏幕截图已获取，但 OCR 未识别到文字）"
        return f"【屏幕截图 OCR 识别文字】\n{text}"

    @tool(parse_docstring=True)
    def view_image(source: str, note: str = "") -> str:
        """把一张**本地图片**读进对话上下文，让自己真正"看到"画面内容。

        用途：自查刚画出来的图表/示意图（matplotlib 保存的 png）、用户让你查看的
        本地图片（截图、照片、图表）。图片作为画面进入上下文后可以直接描述内容；
        **同一张图重复调用不会重复占用上下文**（除非它已被压缩掉）。
        启用代码工作区时：工作区内直接读；区外需要用户审批（可在设置里控制）。
        当前模型不支持视觉时会退化为 OCR 只识别文字。

        Args:
            source: 图片路径（工作区启用时相对当前工作目录；未启用时需绝对路径）
            note: 想从图里看什么（可选，如"确认坐标轴标签是否完整"）
        """
        import os as _os
        from pathlib import Path as _VPath

        from .workspace import is_blocked as _is_blocked
        raw = str(source or "").strip()
        if not raw:
            return "（source 不能为空）"
        p = _VPath(raw).expanduser()
        if not p.is_absolute():
            st = getattr(session, "settings", None) or {}
            ws_raw = str(st.get("workspace_root") or "").strip()
            if not ws_raw:
                return "（相对路径需要先启用代码工作区；未启用时请给绝对路径。）"
            from .workspace import effective_cwd as _eff, resolve_target as _resolve
            base = _eff(_VPath(ws_raw), str(st.get("workspace_cwd") or "")) or _VPath(ws_raw)
            p = _resolve(base, raw)
        else:
            try:
                p = p.resolve()
            except OSError:
                pass
        if _is_blocked(p):
            return f"（该路径在保护名单内，已拒绝：{p}）"
        if not p.is_file():
            return f"（图片不存在：{p}）"
        if p.suffix.lower() not in IMAGE_EXTS:
            return f"（不是支持的图片格式（{'/'.join(sorted(IMAGE_EXTS))}）：{p}）"
        try:
            size = p.stat().st_size
        except OSError as exc:
            return f"（读取失败：{exc}）"
        if size > IMAGE_MAX_BYTES:
            return f"（图片过大（{size // 1024}KB > 15MB），拒绝读入。）"
        # 去重：buffer 里已有这张图的注入消息（还在上下文里）→ 不重复注入。
        # 被压缩/移除后自然会允许再看一次；重启后 metadata 随 buffer_state 恢复。
        key = _os.path.normcase(str(p))
        try:
            with session.buffer_lock:
                for m in session.recent_buffer:
                    ak = getattr(m, "additional_kwargs", None) or {}
                    if ak.get("viewed_image") == key:
                        return "（这张图已经在你当前的上下文里了，直接看图回答即可，无需重复读取。）"
        except Exception:
            pass
        note_text = f" 关注：{str(note).strip()[:120]}" if str(note or "").strip() else ""
        if session is not None and session.vision_capable:
            from .imageutil import image_to_data_url
            url = image_to_data_url(str(p))
            if url:
                session._pending_screenshots.append({
                    "url": url,
                    "text": f"（这是小助读取的图片：{p.name}。请查看画面内容后继续。{note_text}）",
                    "viewed": key})
                return f"（图片已读入上下文（{p.name}，{size // 1024}KB），请查看画面后回答。）"
            _logger.warning("[view_image] 转 data URL 失败，回退 OCR")
        from .ocr import ocr_enabled, ocr_image_file
        text = ocr_image_file(str(p))
        if text:
            return f"【图片 OCR 识别文字】\n{text}"
        if ocr_enabled():
            return "（当前模型不支持视觉；OCR 未识别到文字（图片可能以图形为主）。）"
        return "（当前模型不支持视觉，且 OCR 不可用——图片未能读入，请改用文字说明。）"

    @tool(parse_docstring=True)
    def render_page(url: str, width: int = 1280, height: int = 800) -> str:
        """无头渲染网页并"看"截图——开发网页/前端时用它自查效果（改完 → 渲染 → 看图 → 再改）。

        能一眼看出：布局错位、元素溢出、样式没生效、脚本报错白屏、内容没加载。
        本地文件传路径（相对路径按当前工作目录解析）；跑着 dev server 时传 http 地址。
        截图会作为图片注入本次对话（视觉模型直接看图）；模型不支持视觉时只返回保存路径。

        Args:
            url: 网页地址：本地 html 文件路径，或 http(s):// 地址
            width: 视口宽度像素（320~2560），默认 1280
            height: 视口高度像素（240~4000），默认 800（长页面可调大）
        """
        import subprocess
        import tempfile
        import time as _time
        from pathlib import Path as _RPath

        raw = str(url or "").strip()
        if not raw:
            return "（url 不能为空）"
        width = max(320, min(2560, int(width or 1280)))
        height = max(240, min(4000, int(height or 800)))
        # 解析目标：http(s) 直连；本地路径按工作目录解析 + 黑名单拦截
        label = raw
        if raw.lower().startswith(("http://", "https://")):
            target = raw
        else:
            from .workspace import is_blocked as _is_blocked
            p = _RPath(raw).expanduser()
            if not p.is_absolute():
                st = getattr(session, "settings", None) or {}
                ws_raw = str(st.get("workspace_root") or "").strip()
                if not ws_raw:
                    return "（相对路径需要先启用代码工作区；未启用时请传绝对路径或 http 地址）"
                from .workspace import effective_cwd as _eff, resolve_target as _resolve
                base = _eff(_RPath(ws_raw), str(st.get("workspace_cwd") or "")) or _RPath(ws_raw)
                p = _resolve(base, raw)
            else:
                try:
                    p = p.resolve()
                except OSError:
                    pass
            if _is_blocked(p):
                return f"（该路径在保护范围内，已拒绝：{p}）"
            if not p.is_file():
                return f"（文件不存在：{p}）"
            target = p.as_uri()
            label = str(p)
        browser = _find_browser()
        if not browser:
            return "（没找到本机可用的 Edge/Chrome 浏览器，无法无头渲染截图）"
        out_dir = ((getattr(session, "data_root", None) or _RPath(tempfile.gettempdir()))
                   / "screenshots")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"（截图目录创建失败：{exc}）"
        _prune_screenshots(out_dir)
        out = out_dir / f"shot_{_time.strftime('%Y%m%d_%H%M%S')}.png"
        # 临时 profile：不碰用户浏览器的配置/登录态，也避免与正在运行的 Edge 冲突
        profile = out_dir / "_profile"
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--mute-audio", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--virtual-time-budget=8000",   # 等页面脚本跑完再截
            f"--screenshot={out}",
            target,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=60,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except subprocess.TimeoutExpired:
            return "（渲染超时（60s）：页面可能有死循环或加载卡住）"
        except Exception as exc:
            return f"（启动浏览器失败：{exc}）"
        try:
            ok = out.is_file() and out.stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            tail = (proc.stderr or b"").decode("utf-8", "ignore")[-300:].strip()
            return f"（渲染没有产出截图；浏览器输出：{tail or '无'}）"
        # 视觉注入（与 view_image / capture_screen 同链路：下一轮注入 user 消息图片块）
        if session is not None and getattr(session, "vision_capable", False):
            from .imageutil import image_to_data_url
            data_url = image_to_data_url(str(out))
            if data_url:
                session._pending_screenshots.append({
                    "url": data_url,
                    "text": f"（你无头渲染的网页截图：{label}，视口 {width}×{height}）"
                            "检查布局/样式/内容是否符合预期。"})
                return (f"已渲染并截图（{width}×{height}），截图已注入本次对话——"
                        "请直接查看效果，确认是否需要修改。")
        return f"已渲染截图保存到 {out}（当前模型不支持视觉，无法直接看图）。"

    @tool(parse_docstring=True)
    def web_search(query: str, limit: int = 5) -> str:
        """搜索互联网，返回相关网页的标题、链接和摘要。需要查最新信息、新闻、资料时使用。

        关键词要**短而准**：用 2~5 个「专有名词/术语」拼起来，别写成一整句自然语言问题。
        长句会被搜索引擎拆词降权，反而搜回一堆泛泛的官网首页和百科词条。
        例：查小米的语音模型用「小米 MiMo 语音 模型」，不要用「小米公司有没有自己的语音大模型」。
        支持搜索语法（直接写进 query）：site:域名、-site:排除、\"精确短语\"、-排除词、
        lang:zh,en、after:2025-01-01、before:2026-06-01；查最新进展可以加 after:。
        摘要只是节选，判断某条是否真有用要再用 fetch_web 打开正文。

        Args:
            query: 短关键词组合（专有名词优先）
            limit: 返回结果数（1~8），默认 5
        """
        q = str(query).strip()
        if not q:
            return "（搜索关键词不能为空）"
        limit = max(1, min(8, int(limit or 5)))
        items, engine, err = _search_web(q, limit, session)
        if not items:
            if err:
                return f"（搜索失败：{err}）"
            return f"（没有搜到「{q}」的结果，换个更短的关键词试试）"
        lines = [f"「{q}」搜索结果（{len(items)} 条，来源 {engine}）："]
        for i, it in enumerate(items, 1):
            date = f"（{it['date']}）" if it.get("date") else ""
            lines.append(f"{i}. {it['title']}{date}\n   {it['url']}\n   {it['snippet']}")
        lines.append("（摘要只是节选，想确认某条是否真有用，用 fetch_web 打开正文。）")
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def fetch_web(url: str, max_chars: int = 6000) -> str:
        """抓取一个网页的正文文本（DeepSeek 官方抓取为 Markdown；回退内置抓取时附页面链接）。
        web_search 找到链接后用它读取内容；页面正文里提到的内容如果不够，
        从【页面链接】里挑相关链接再抓。

        Args:
            url: 网页地址（http/https 开头）
            max_chars: 最多返回的正文字符数（1000~20000），默认 6000
        """
        import re as _re

        import httpx

        u = str(url).strip()
        if not u.lower().startswith(("http://", "https://")):
            return "（只支持 http/https 链接）"
        max_chars = max(1000, min(20000, int(max_chars or 6000)))
        # 优先 DeepSeek 官方 Fetch（直接返回 Markdown 正文，无需自己解析 HTML）
        base, key = _deepseek_search_config(session)
        if key:
            try:
                title, md = _deepseek_fetch(base, key, u)
                if md.strip():
                    out = md[:max_chars] + ("\n…（内容已截断）" if len(md) > max_chars else "")
                    return (f"【{title}】\n" if title else "") + out
            except Exception as exc:
                _logger.warning("[fetch_web] DeepSeek 官方抓取失败，回退内置抓取：%s", exc)
        try:
            r = httpx.get(u, headers=_WEB_HEADERS, timeout=20, follow_redirects=True)
            r.raise_for_status()
            html = r.text
        except Exception as exc:
            return f"（抓取失败：{exc}）"
        # 去脚本/样式/标签 → 正文文本
        text = _re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        text = _re.sub(r"(?s)<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", text).strip()
        if not text:
            return "（页面没有可读文本内容）"
        out = text[:max_chars] + ("\n…（内容已截断）" if len(text) > max_chars else "")
        # 附带页面链接（相对路径补全为绝对地址），供模型顺藤摸瓜
        links = _extract_links(html, u, limit=20)
        if links:
            out += "\n\n【页面链接】\n" + "\n".join(f"- {t}：{href}" for t, href in links)
        return out

    return [heartbeat, continue_speaking, ask_user,
            list_schedule, add_schedule, update_schedule, cancel_schedule,
            explore_memory_tree, list_dir, read_file, capture_screen, view_image,
            render_page, web_search, fetch_web] + build_code_tools(session)


# ── 工具结果卸载（大输出落盘，上下文只留指针）──────────────────

def _offload_if_large(text: str, session, name: str, limit: int = 12000) -> str:
    """超长工具结果落盘（data/tool_results/，随 DATA_ROOT 隔离），返回预览 + 指针。

    上下文只保留前 limit 个字符；需要细节时用 read_file 读落盘文件
    （tool_results 是助手自身数据，ApprovalMiddleware 视为免审批）。
    落盘失败则退化为纯截断。
    """
    if len(text) <= limit:
        return text
    try:
        import hashlib
        import time as _time
        from pathlib import Path as _Path
        root = _Path(getattr(session, "data_root", "") or ".")
        root = root / "tool_results"
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:10]
        path = root / f"{name}_{int(_time.time())}_{digest}.txt"
        path.write_text(text, encoding="utf-8")
    except Exception:
        return text[:limit] + f"\n…（内容过长已截断；共 {len(text)} 字符）"
    return (text[:limit]
            + f"\n…（内容过长，仅显示预览；完整内容共 {len(text)} 字符，"
              f"已落盘：{path}（需要细节时用 read_file 读取该文件））")


# ── 子进程工具（run_command 的进程树终止）──────────────────────

def _fuzzy_edit_span(text: str, old: str) -> dict | None:
    """edit_file 的宽松匹配兜底（对齐 opencode 的 edit 策略；文本已归一为 \\n）。

    逐级尝试（只在**精确匹配 0 次**时才会被调用）：
    ① 行级 trim：忽略每行首尾空白/缩进差异；
    ② 首尾行锚定（≥3 行的块）：首尾行 trim 后相等且唯一 → 整块替换。
    返回 {"start", "end", "count", "how"}；无候选返回 None；
    有候选但不唯一返回 {"count": n}（调用方报"宽松匹配不唯一"，让模型补上下文）。
    """
    import re as _re

    old_lines = old.split("\n")
    # ① 行级 trim
    pat = "\n".join("[\\t ]*" + _re.escape(l.strip()) + "[\\t ]*" for l in old_lines)
    try:
        matches = list(_re.finditer(pat, text))
    except _re.error:
        matches = []
    if len(matches) == 1:
        m = matches[0]
        return {"start": m.start(), "end": m.end(), "count": 1,
                "how": "缩进/行尾空白有差异"}
    if len(matches) > 1:
        return {"count": len(matches)}
    # ② 首尾行锚定（内部行有差异时）
    if len(old_lines) >= 3:
        first = old_lines[0].strip()
        last = old_lines[-1].strip()
        lines = text.split("\n")
        cand: list[tuple[int, int]] = []
        for i, l in enumerate(lines):
            if l.strip() != first:
                continue
            for j in range(len(lines) - 1, i + 1, -1):
                if lines[j].strip() == last:
                    cand.append((i, j))
                    break
        if len(cand) == 1:
            i, j = cand[0]
            start = sum(len(x) + 1 for x in lines[:i])
            end = sum(len(x) + 1 for x in lines[:j]) + len(lines[j])
            return {"start": start, "end": end, "count": 1,
                    "how": "首尾行锚定（中间内容与你给的略有差异）"}
        if len(cand) > 1:
            return {"count": len(cand)}
    return None


# ── apply_patch（Codex V4A 风格的多文件补丁）────────────────────

def _parse_apply_patch(text: str) -> tuple[list[dict], list[str]]:
    """解析 V4A 风格补丁 → (ops, errors)。

    ops: [{"kind": "add|delete|update", "path": str, "move_to": str|None,
           "hunks": [{"anchor": str|None, "lines": [(op, text), ...]}]}]
    op ∈ " "（上下文）/ "-"（删除）/ "+"（新增）。
    解析错误全部收集（调用方一次性回报，不改任何文件）。
    """
    errors: list[str] = []
    ops: list[dict] = []
    cur: dict | None = None
    cur_hunk: dict | None = None
    lines = text.split("\n")
    i = 0
    # 跳过首尾空行，找 Begin Patch
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "*** Begin Patch":
        return [], ["补丁必须以 `*** Begin Patch` 开头（见工具说明的格式）。"]
    i += 1
    seen_end = False
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        i += 1
        if not s:
            if cur_hunk is not None:
                cur_hunk["lines"].append((" ", ""))
            continue
        if s == "*** End Patch":
            seen_end = True
            break
        if s.startswith("*** Update File:"):
            cur = {"kind": "update", "path": s.split(":", 1)[1].strip(),
                   "move_to": None, "hunks": []}
            ops.append(cur)
            cur_hunk = None
            continue
        if s.startswith("*** Add File:"):
            cur = {"kind": "add", "path": s.split(":", 1)[1].strip(),
                   "move_to": None, "hunks": [{"anchor": None, "lines": []}]}
            ops.append(cur)
            cur_hunk = cur["hunks"][0]
            continue
        if s.startswith("*** Delete File:"):
            cur = {"kind": "delete", "path": s.split(":", 1)[1].strip(),
                   "move_to": None, "hunks": []}
            ops.append(cur)
            cur_hunk = None
            continue
        if s.startswith("*** Move to:"):
            if cur is None or cur["kind"] != "update":
                errors.append(f"`*** Move to:` 只能出现在 Update File 段内（第 {i} 行附近）")
            else:
                cur["move_to"] = s.split(":", 1)[1].strip()
            continue
        if s.startswith("*** "):
            errors.append(f"无法识别的指令：{s}（第 {i} 行附近）")
            continue
        if raw.startswith("\\"):     # "\ No newline at end of file" 之类，忽略
            continue
        if cur is None:
            errors.append(f"内容出现在任何文件段之前（第 {i} 行附近）：{raw[:60]}")
            continue
        if s.startswith("@@"):
            if cur["kind"] != "update":
                errors.append(f"`@@` 锚点只能用于 Update File（第 {i} 行附近）")
                continue
            cur_hunk = {"anchor": s[2:].strip() or None, "lines": []}
            cur["hunks"].append(cur_hunk)
            continue
        if cur["kind"] == "delete":
            errors.append(f"Delete File 段不应有内容行（第 {i} 行附近）")
            continue
        if cur_hunk is None:
            cur_hunk = {"anchor": None, "lines": []}
            cur["hunks"].append(cur_hunk)
        if raw.startswith("+"):
            cur_hunk["lines"].append(("+", raw[1:]))
        elif raw.startswith("-"):
            cur_hunk["lines"].append(("-", raw[1:]))
        elif raw.startswith(" "):
            cur_hunk["lines"].append((" ", raw[1:]))
        else:
            # 没带前缀的行（模型少写空格）→ 按上下文行宽容处理
            cur_hunk["lines"].append((" ", raw))
    if not seen_end:
        errors.append("补丁缺少结尾的 `*** End Patch`。")
    for op in ops:
        if not op["path"]:
            errors.append("文件路径为空。")
        if op["kind"] == "update" and not op["hunks"] and not op["move_to"]:
            errors.append(f"Update File {op['path']} 段没有任何改动内容。")
    if not ops and not errors:
        errors.append("补丁里没有任何文件操作。")
    return ops, errors


def _match_line_block(lines: list[str], block: list[str], pos: int,
                      fallback_start: bool = True,
                      ) -> tuple[int, int, str] | None:
    """在 lines[pos:] 里找 block（返回 [start, end) 与匹配方式；找不到再从 0 找一遍）。

    ① 逐行精确；② 逐行 trim（忽略缩进/行尾空白）。返回 (start, end, how)。
    fallback_start=False（有 @@ 锚点时）不做"从头再找"的兜底。
    """
    if not block:
        return None

    def _scan(from_idx: int, fuzzy: bool):
        n, m = len(lines), len(block)
        for start in range(from_idx, n - m + 1):
            ok = True
            for k in range(m):
                a, b = lines[start + k], block[k]
                if fuzzy:
                    if a.strip() != b.strip():
                        ok = False
                        break
                elif a != b:
                    ok = False
                    break
            if ok:
                return start
        return -1

    scopes = (pos, 0) if fallback_start else (pos,)
    for scope in scopes:
        start = _scan(scope, False)
        if start >= 0:
            return start, start + len(block), "精确"
    for scope in scopes:
        start = _scan(scope, True)
        if start >= 0:
            return start, start + len(block), "宽松（缩进/行尾空白有差异）"
    return None


def _kill_process_tree(proc) -> None:
    """终止整棵进程树（Windows 用 `taskkill /T`；其它平台 kill；失败静默）。"""
    import subprocess as _sp
    if os.name == "nt":
        try:
            _sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


# ── 代码工作区工具（仅当 settings.workspace_root 非空时注册）──────

def build_code_tools(session) -> list[BaseTool]:
    """代码工作区工具工厂；未设置工作区（或目录无效）时返回空列表。

    审批不在这里做：写操作 / 工作区外读由 ApprovalMiddleware 挂起确认
    （黑名单硬拒也在那里）。这里只负责执行、结果格式化，以及黑名单兜底。
    """

    def _workspace():
        from pathlib import Path as _Path
        raw = str((getattr(session, "settings", None) or {}).get("workspace_root") or "").strip()
        if not raw:
            return None
        p = _Path(raw).expanduser()
        try:
            p = p.resolve()
        except OSError:
            return None
        return p if p.is_dir() else None

    ws_root = _workspace()
    code_tools: list[BaseTool] = []

    if ws_root is not None:
        from pathlib import Path as _Path

        from .workspace import (blocked_command_reason, effective_cwd, is_blocked,
                                is_within, rel_posix, resolve_target)

        WS = ws_root
        _st = getattr(session, "settings", None) or {}
        CWD = effective_cwd(WS, str(_st.get("workspace_cwd") or "")) or WS
        MAX_READ_BYTES = 50 * 1024 * 1024     # 与 read_file 一致：超大文件拒绝
        GREP_MAX_BYTES = 1 * 1024 * 1024      # grep 跳过超过 1MB 的文件

        def _target(raw: str) -> _Path:
            return resolve_target(CWD, raw)   # 相对路径按「当前工作目录」解析

        def _rel(p: _Path) -> str:
            return rel_posix(CWD, p)

        def _guard(p: _Path) -> str | None:
            """黑名单兜底（正常由 ApprovalMiddleware 先拦）。"""
            if is_blocked(p):
                return f"（该路径在保护名单内，已拒绝：{p}）"
            return None

        @tool(parse_docstring=True)
        def get_cwd() -> str:
            """查看当前的工作目录 / 工作区根 / 权限模式（**实时值**，不是记忆里的）。

            用户切换了工作目录后、或你不确定相对路径以哪里为基准时用；平时直接看
            系统提示词的 代码工作区 段即可（那一段每次请求都会刷新）。
            """
            st = getattr(session, "settings", None) or {}
            mode = str(st.get("permission_mode") or "ask").strip().lower()
            return (f"当前工作目录：{CWD}\n工作区根：{WS}\n权限模式：{mode}")

        def _record_change(tool_name: str, path: _Path,
                           before: str | None, before_missing: bool,
                           after: str | None, no_snapshot: bool = False) -> None:
            """写操作留底（进变更历史，供"回退到某条之前"）；失败不影响操作本身。

            同时把 change id 记到 session 的待关联队列：本轮生成结束时
            `_emit_tool_events` 会把它附到对应的 tool_result 事件上（前端展示
            修改前后 diff），并写进消息的 additional_kwargs（/history 重建用）。
            """
            if session is None:
                return
            try:
                from .changes import MAX_SNAPSHOT_CHARS
                # after=None 即"操作后文件不存在"（delete / move 源 / 补丁删除）——
                # 曾经漏传 after_missing：删除的文件在「改动文件」块里仍显示「打开」
                # 按钮，点了没反应（用户实测）
                after_missing = after is None
                if ((before is not None and len(before) > MAX_SNAPSHOT_CHARS)
                        or (after is not None and len(after) > MAX_SNAPSHOT_CHARS)):
                    before, after, no_snapshot = None, None, True
                cid = session.get_changes_db().record(
                    tool_name, str(path), _rel(path), before, after,
                    before_missing=before_missing, after_missing=after_missing,
                    no_snapshot=no_snapshot)
                try:
                    session._pending_changes.append({"tool": tool_name, "id": int(cid)})
                except Exception:
                    pass
            except Exception:
                _logger.warning("[changes] 变更留底失败", exc_info=True)

        def _read_raw(p: _Path) -> tuple[str, str, str] | None:
            """→ (原文, 换行符, 编码)；保留原生换行（不把 CRLF 转成 LF）。"""
            try:
                if p.stat().st_size > MAX_READ_BYTES:
                    return None
                with open(p, "rb") as f:
                    if b"\x00" in f.read(2048):
                        return None
            except OSError:
                return None
            for enc in ("utf-8", "gbk"):
                try:
                    with open(p, "r", encoding=enc, newline="") as f:
                        text = f.read()
                except (UnicodeDecodeError, OSError):
                    continue
                return text, ("\r\n" if "\r\n" in text else "\n"), enc
            return None

        @tool(parse_docstring=True)
        def apply_patch(patch: str) -> str:
            """对工作区做**多文件补丁**（一次调用可改多个文件/多处；**要么全部生效、要么
            全部不改**）。需要用户审批（按补丁里涉及的全部文件逐一批）。

            格式（V4A，与 Codex apply_patch 相同）：

            *** Begin Patch
            *** Update File: src/app.py
            @@ def main():
                 （保持不变的行：行首一个空格；用它定位）
            -要删除的行（原样抄）
            +替换成的新行
            *** Add File: notes/new.md
            +第一行内容
            *** Delete File: old.txt
            *** End Patch

            规则：
            - Update 靠**上下文行**定位（至少带 1 行上下文；`@@ 锚点文字` 可先按文字
              快速定位）；同一段上下文出现多处会报错，多带几行上下文；
            - 重命名/移动：在 Update 段里加 `*** Move to: 新路径`；
            - Add 的新文件不能已存在；Delete 的文件必须存在；
            - 缩进/行尾空白有细微差异会自动宽松匹配（结果里会注明，建议随后复核）；
            - 整份补丁原子生效：任何一处解析/定位失败都不会改任何文件。

            Args:
                patch: V4A 补丁全文（必须以 `*** Begin Patch` 开头、`*** End Patch` 结尾）
            """
            ops, errors = _parse_apply_patch(str(patch or ""))
            if errors:
                return "补丁解析失败（未做任何改动）：\n- " + "\n- ".join(errors[:8])
            # ── 阶段 1：全部在内存里计算（黑名单/越界/定位失败 → 整体放弃）──
            plans: list[dict] = []
            problem: list[str] = []
            fuzzy_used = False
            for op in ops:
                p = _target(op["path"])
                err = _guard(p)
                if err:
                    problem.append(err)
                    continue
                if not is_within(WS, p):
                    problem.append(f"（{_rel(p)} 在工作区外，已拒绝）")
                    continue
                plan = {"op": op, "path": p, "move": None,
                        "before": None, "before_missing": False,
                        "after": None, "enc": "utf-8", "nl": "\n"}
                if op["kind"] == "add":
                    if p.is_file():
                        problem.append(f"{_rel(p)} 已存在（Add 只能建新文件；改已有文件用 Update）")
                        continue
                    add_lines = [t for k, t in op["hunks"][0]["lines"] if k == "+"]
                    plan["before_missing"] = True
                    plan["after"] = "\n".join(add_lines) + ("\n" if add_lines else "")
                elif op["kind"] == "delete":
                    if not p.is_file():
                        problem.append(f"{_rel(p)} 不存在，无法删除")
                        continue
                    got = _read_raw(p)
                    if got is None:
                        problem.append(f"{_rel(p)} 无法读取（二进制/超大/特殊编码），拒绝打补丁")
                        continue
                    plan["before"] = got[0]
                else:   # update（可含 move）
                    if not p.is_file():
                        problem.append(f"{_rel(p)} 不存在，无法 Update")
                        continue
                    got = _read_raw(p)
                    if got is None:
                        problem.append(f"{_rel(p)} 无法读取（二进制/超大/特殊编码），拒绝打补丁")
                        continue
                    text, nl, enc = got
                    plan["before"], plan["nl"], plan["enc"] = text, nl, enc
                    flines = text.replace("\r\n", "\n").split("\n")
                    pos = 0
                    ok = True
                    for hunk in op["hunks"]:
                        if hunk["anchor"]:
                            key = hunk["anchor"]
                            idx = next((j for j in range(pos, len(flines))
                                        if key in flines[j]), -1)
                            if idx < 0:
                                idx = next((j for j in range(0, len(flines))
                                            if key in flines[j]), -1)
                            if idx < 0:
                                problem.append(f"{_rel(p)}：找不到锚点 `{key}`")
                                ok = False
                                break
                            pos = idx
                        old_block = [t for k, t in hunk["lines"] if k in (" ", "-")]
                        new_block = [t for k, t in hunk["lines"] if k in (" ", "+")]
                        if not old_block:
                            if hunk["anchor"]:
                                pos += 1        # 锚点行之后插入
                                flines[pos:pos] = new_block
                                pos += len(new_block)
                            else:
                                problem.append(f"{_rel(p)}：纯新增内容缺少定位（带上下文行或 @@ 锚点）")
                                ok = False
                                break
                            continue
                        m = _match_line_block(flines, old_block, pos,
                                              fallback_start=not hunk["anchor"])
                        if m is None:
                            problem.append(
                                f"{_rel(p)}：上下文定位失败（未找到补丁里那几行；"
                                f"请先 read_file 核对原文）")
                            ok = False
                            break
                        start, end, how = m
                        if how != "精确":
                            fuzzy_used = True
                        flines[start:end] = new_block
                        pos = start + len(new_block)
                    if not ok:
                        continue
                    if op["move_to"]:
                        d = _target(op["move_to"])
                        err = _guard(d)
                        if err:
                            problem.append(err)
                            continue
                        if not is_within(WS, d):
                            problem.append(f"（{_rel(d)} 在工作区外，已拒绝）")
                            continue
                        if d.exists():
                            problem.append(f"移动目标 {_rel(d)} 已存在")
                            continue
                        plan["move"] = d
                    updated = "\n".join(flines)
                    plan["after"] = (updated.replace("\n", "\r\n")
                                     if plan["nl"] == "\r\n" else updated)
                plans.append(plan)
            if problem:
                return ("补丁未应用（原子回滚，未改任何文件）：\n- "
                        + "\n- ".join(problem[:8]))
            # ── 阶段 2：落盘 + 留底（每文件一条，可单独回退）──
            summary: list[str] = []
            for plan in plans:
                op, p, after = plan["op"], plan["path"], plan["after"]
                try:
                    if op["kind"] == "delete":
                        p.unlink()
                        _record_change("apply_patch", p, plan["before"], False, None)
                        summary.append(f"D {_rel(p)}")
                        continue
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if op["kind"] == "add":
                        with open(p, "w", encoding="utf-8", newline="") as f:
                            f.write(after)
                        _record_change("apply_patch", p, None, True, after)
                        summary.append(f"A {_rel(p)}")
                        continue
                    d = plan["move"]
                    if d is not None:
                        d.parent.mkdir(parents=True, exist_ok=True)
                        with open(d, "w", encoding=plan["enc"], newline="") as f:
                            f.write(after)
                        p.unlink()
                        _record_change("apply_patch", p, plan["before"], False, None)
                        _record_change("apply_patch", d, None, True, after)
                        summary.append(f"R {_rel(p)} → {_rel(d)}")
                    else:
                        with open(p, "w", encoding=plan["enc"], newline="") as f:
                            f.write(after)
                        _record_change("apply_patch", p, plan["before"], False, after)
                        summary.append(f"M {_rel(p)}"
                                       + (f"（{len(op['hunks'])} 处）" if op["hunks"] else ""))
                except OSError as exc:
                    return (f"落盘失败：{exc}（已写入的文件不回滚，请用「变更」tab 核查/回退）")
            tail = "；部分使用了宽松匹配（缩进/行尾空白有差异，建议复核）" if fuzzy_used else ""
            return (f"补丁已应用（{len(summary)} 个文件{tail}）：\n"
                    + "\n".join(summary))

        @tool(parse_docstring=True)
        def write_file(path: str, content: str) -> str:
            """在工作区里新建或覆盖一个文本文件（需要用户审批）。

            Args:
                path: 文件路径（工作区相对或绝对路径）
                content: 要写入的完整内容（会覆盖已有文件）
            """
            p = _target(path)
            err = _guard(p)
            if err:
                return err
            from .changes import snapshot_before
            before, before_missing, no_snap = snapshot_before(p)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(str(content))
            except OSError as exc:
                return f"写入失败：{exc}"
            after = str(content)
            _record_change("write_file", p, before, before_missing, after,
                           no_snapshot=no_snap)
            return f"已写入 {_rel(p)}（{len(str(content))} 字符）。"

        @tool(parse_docstring=True)
        def edit_file(path: str, old_string: str, new_string: str,
                      replace_all: bool = False) -> str:
            """在工作区里对文件做精确替换（需要用户审批）。

            old_string 必须与原文逐字匹配（含缩进）；匹配多处时默认报错，
            确需全部替换才传 replace_all=true。**缩进/行尾空白有细微差异时会自动
            宽松匹配兜底**（结果里会注明，建议随后 read_file 确认落盘结果）。
            CRLF/LF 按原文语义匹配与保留。

            Args:
                path: 文件路径
                old_string: 要被替换的原文本（逐字匹配，含缩进）
                new_string: 替换后的新文本
                replace_all: true＝替换全部匹配；默认 false（不唯一时报错）
            """
            p = _target(path)
            err = _guard(p)
            if err:
                return err
            if not p.is_file():
                return f"文件不存在：{_rel(p)}"
            got = _read_raw(p)
            if got is None:
                return "无法读取（可能是二进制、超大或特殊编码）。"
            text, nl, enc = got
            norm = text.replace("\r\n", "\n")
            old_n = str(old_string).replace("\r\n", "\n")
            new_n = str(new_string).replace("\r\n", "\n")
            if not old_n:
                return "old_string 不能为空。"
            count = norm.count(old_n)
            if count == 0:
                # 精确匹配 0 次 → 宽松兜底（opencode 同款策略：行级 trim / 首尾行锚定）
                fz = _fuzzy_edit_span(norm, old_n)
                if fz and fz.get("start") is not None:
                    updated = norm[:fz["start"]] + new_n + norm[fz["end"]:]
                    out = updated.replace("\n", nl) if nl == "\r\n" else updated
                    try:
                        with open(p, "w", encoding=enc, newline="") as f:
                            f.write(out)
                    except OSError as exc:
                        return f"写入失败：{exc}"
                    _record_change("edit_file", p, text, False, out)
                    return (f"已更新 {_rel(p)}（1 处；**宽松匹配**：{fz['how']}）。"
                            "建议用 read_file 复核落盘结果。")
                if fz and fz.get("count", 0) > 1:
                    return (f"old_string 未逐字匹配；按宽松匹配出现 {fz['count']} 次（不唯一）。"
                            "请补充上下文让它唯一，或先 read_file 看原文后重试。")
                return (f"没有找到要替换的内容（未改动 {_rel(p)}）。"
                        "请先读文件确认原文（含缩进/空行），再重试。")
            if count > 1 and not replace_all:
                return (f"old_string 在 {_rel(p)} 中出现 {count} 次，不唯一。"
                        "请补充上下文让它唯一，或传 replace_all=true 全部替换。")
            updated = (norm.replace(old_n, new_n) if replace_all
                       else norm.replace(old_n, new_n, 1))
            out = updated.replace("\n", nl) if nl == "\r\n" else updated
            try:
                with open(p, "w", encoding=enc, newline="") as f:
                    f.write(out)
            except OSError as exc:
                return f"写入失败：{exc}"
            _record_change("edit_file", p, text, False, out)
            return f"已更新 {_rel(p)}（替换 {count if replace_all else 1} 处）。"

        @tool(parse_docstring=True)
        def create_dir(path: str) -> str:
            """在工作区里新建目录（需要用户审批；已存在则无副作用）。

            Args:
                path: 目录路径（父目录会自动创建）
            """
            p = _target(path)
            err = _guard(p)
            if err:
                return err
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return f"创建失败：{exc}"
            return f"目录已就绪：{_rel(p)}/"

        @tool(parse_docstring=True)
        def move_file(src: str, dst: str) -> str:
            """移动/重命名工作区里的文件（需要用户审批）。

            Args:
                src: 源文件路径
                dst: 目标路径（父目录会自动创建）
            """
            import shutil
            s, d = _target(src), _target(dst)
            for q in (s, d):
                err = _guard(q)
                if err:
                    return err
            if not s.is_file():
                return f"源文件不存在：{_rel(s)}"
            from .changes import snapshot_before
            src_before, _, src_no_snap = snapshot_before(s)
            dst_before, dst_missing, _ = snapshot_before(d)
            try:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(s), str(d))
            except OSError as exc:
                return f"移动失败：{exc}"
            # 两条留底：源文件消失（可恢复） + 目标文件出现（回退时删除）
            _record_change("move_file", s, src_before, False, None,
                           no_snapshot=src_no_snap)
            _record_change("move_file", d, dst_before, dst_missing,
                           src_before if not src_no_snap else None,
                           no_snapshot=src_no_snap)
            return f"已移动：{_rel(s)} → {_rel(d)}"

        @tool(parse_docstring=True)
        def delete_file(path: str) -> str:
            """删除工作区里的一个文件（需要用户审批；不删目录）。

            Args:
                path: 文件路径
            """
            p = _target(path)
            err = _guard(p)
            if err:
                return err
            if p.is_dir():
                return f"{_rel(p)} 是目录，本工具只删文件；请确认后手动处理目录。"
            if not p.is_file():
                return f"文件不存在：{_rel(p)}"
            from .changes import snapshot_before
            before, _, no_snap = snapshot_before(p)
            try:
                p.unlink()
            except OSError as exc:
                return f"删除失败：{exc}"
            _record_change("delete_file", p, before, False, None,
                           no_snapshot=no_snap)
            return f"已删除：{_rel(p)}"

        @tool(parse_docstring=True)
        def glob(pattern: str, path: str = "") -> str:
            """按通配符查找工作区里的文件（如 src/**/*.py）。

            Args:
                pattern: 通配模式（** 表示递归）
                path: 起始目录（留空＝工作区根）
            """
            raw = str(path or "").strip()
            base = _target(raw) if raw else CWD
            err = _guard(base)
            if err:
                return err
            if not base.exists():
                return f"目录不存在：{base}"
            try:
                hits = sorted(base.glob(str(pattern or "*")), key=lambda q: str(q).lower())
            except (ValueError, NotImplementedError) as exc:
                return f"模式无效：{exc}"
            files = [q for q in hits if q.is_file()]
            if not files:
                return f"没有匹配 {pattern!r} 的文件。"
            shown = files[:200]
            head = (f"匹配 {len(files)} 个文件"
                    + ("（显示前 200 个）：" if len(files) > len(shown) else "："))
            return head + "\n" + "\n".join(_rel(q) for q in shown)

        @tool(parse_docstring=True)
        def grep(pattern: str, include: str = "", path: str = "",
                 max_results: int = 50) -> str:
            """在工作区的文本文件里按正则搜索内容，返回 文件:行号:内容。

            Args:
                pattern: 正则表达式
                include: 只搜匹配该通配符的文件（如 *.py；留空＝全部文本文件）
                path: 起始目录（留空＝工作区根）
                max_results: 最多返回的命中行数（1~200，默认 50）
            """
            import re as _re
            raw = str(path or "").strip()
            base = _target(raw) if raw else CWD
            err = _guard(base)
            if err:
                return err
            if not base.exists():
                return f"目录不存在：{base}"
            try:
                rx = _re.compile(str(pattern))
            except _re.error as exc:
                return f"正则无效：{exc}"
            limit = max(1, min(200, int(max_results or 50)))
            inc = str(include or "").strip()
            matches: list[str] = []
            truncated = False
            for q in sorted(base.rglob("*"), key=lambda x: str(x).lower()):
                if len(matches) >= limit:
                    truncated = True
                    break
                if not q.is_file() or is_blocked(q):
                    continue
                if inc and not q.match(inc):
                    continue
                try:
                    if q.stat().st_size > GREP_MAX_BYTES:
                        continue
                except OSError:
                    continue
                try:
                    with open(q, "r", encoding="utf-8", errors="strict",
                              newline="") as f:
                        text = f.read()
                except (UnicodeDecodeError, OSError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{_rel(q)}:{i}: {line.strip()[:300]}")
                        if len(matches) >= limit:
                            truncated = True
                            break
            if not matches:
                return f"没有找到匹配 {pattern!r} 的内容。"
            tail = "（已截断，缩小范围或调大 max_results 可看更多）" if truncated else ""
            return (f"命中 {len(matches)} 行（上限 {limit}）：\n"
                    + "\n".join(matches) + tail)

        @tool(parse_docstring=True)
        def run_command(command: str, timeout: int = 120) -> str:
            """在当前工作目录执行一条命令（**每条命令都需要用户逐条审批**），返回退出码与输出。

            用途：跑项目自带测试（`pytest -q`、`npm test`）、构建、类型检查等**验证**命令。
            破坏性命令（递归删除、格式化磁盘、下载即执行、强推远端等）会被硬拒绝；
            被用户拒绝的命令不要反复重试；改完代码尽量跑相关测试验证。

            Args:
                command: 要执行的命令，如 `pytest -q tests/`
                timeout: 超时秒数（1~600，默认 120）；超时会终止整棵进程树
            """
            import subprocess
            cmd = str(command or "").strip()
            if not cmd:
                return "（命令为空）"
            reason = blocked_command_reason(cmd)
            if reason:
                return f"（命令命中保护规则「{reason}」，已拒绝）"
            timeout = max(1, min(600, int(timeout or 120)))
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("MPLBACKEND", "Agg")   # matplotlib 无界面：plt.show() 不至于卡住
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = subprocess.Popen(
                    cmd, shell=True, cwd=str(CWD),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=flags, env=env)
            except OSError as exc:
                return f"（无法启动命令：{exc}）"
            if session is not None:
                session._run_proc = proc
            try:
                try:
                    out, _ = proc.communicate(timeout=timeout)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    _kill_process_tree(proc)
                    return f"（命令超时（{timeout}s），已终止：{cmd[:160]}）"
            finally:
                if session is not None:
                    session._run_proc = None
            text = ""
            if out:
                try:
                    text = out.decode("utf-8")
                except UnicodeDecodeError:
                    text = out.decode("gbk", "replace")
            head = f"$ {cmd[:200]}\n（退出码 {code}）"
            body = text.strip("\n")
            if not body:
                return head + "\n（无输出）"
            return head + "\n" + _offload_if_large(body, session, "run_command")

        _IMAGE_CT_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                         "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp"}

        def _queue_image(path_str: str, rel: str, caption: str) -> None:
            if session is not None:
                try:
                    session._pending_images.append(
                        {"path": str(path_str), "rel": rel,
                         "caption": str(caption or "").strip()[:200]})
                except Exception:
                    pass

        def _persist_image(src: _Path) -> str | None:
            """展示前把图片复制到 data/images/ 持久副本（内容哈希命名，重复展示去重）。

            历史重建（/history）按原路径重新拉图——工作区原文件之后被删/改/移动，
            历史里的图片就会「加载失败」（用户实测：简历预览图被清理后回看报错）。
            /file 的允许根含 data/images，展示时先落副本，原文件怎么变都不影响回看。
            """
            import hashlib
            try:
                data = src.read_bytes()
                root = _Path(getattr(session, "data_root", "") or ".")
                cache = root / "images"
                name = hashlib.sha1(data).hexdigest()[:16] + src.suffix.lower()
                dest = cache / name
                if not dest.exists():
                    cache.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                return str(dest)
            except Exception:
                return None

        def _show_remote_image(url: str, caption: str) -> str:
            """网络图片：下载到 data/images/ 缓存后展示（外链失效/防盗链时历史仍可看）。"""
            import hashlib

            import httpx
            try:
                with httpx.stream("GET", url, headers=_WEB_HEADERS, timeout=20,
                                  follow_redirects=True) as resp:
                    if resp.status_code != 200:
                        return f"（图片下载失败：HTTP {resp.status_code}）"
                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    ext = _IMAGE_CT_EXT.get(ctype) or ""
                    if not ext:
                        # content-type 不明确时按 URL 后缀兜底
                        suffix = _Path(url.split("?")[0].split("#")[0]).suffix.lower()
                        if suffix in IMAGE_EXTS:
                            ext = ".jpg" if suffix == ".jpeg" else suffix
                    if not ext:
                        return f"（不是图片（content-type: {ctype or '未知'}）：{url[:120]}）"
                    data = bytearray()
                    for chunk in resp.iter_bytes():
                        data.extend(chunk)
                        if len(data) > IMAGE_MAX_BYTES:
                            return "（图片过大（>15MB），已放弃下载）"
            except Exception as exc:
                return f"（图片下载失败：{exc}）"
            try:
                root = _Path(getattr(session, "data_root", "") or ".")
                cache = root / "images"
                cache.mkdir(parents=True, exist_ok=True)
                name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ext
                dest = cache / name
                dest.write_bytes(bytes(data))
            except Exception as exc:
                return f"（图片缓存失败：{exc}）"
            _queue_image(str(dest), url, caption)
            return f"已展示网络图片给用户：{url[:160]}（{len(data) // 1024}KB）。"

        @tool(parse_docstring=True)
        def show_image(source: str, caption: str = "") -> str:
            """把一张图片直接展示给用户（显示在对话里）。

            两种来源：
            - **工作区内的图片文件**：matplotlib 保存的图表（用 `plt.savefig("xxx.png")`，
              不要 `plt.show()`）、程序生成的示意图、截图等；
            - **网络图片直链**（http/https）：先用 `web_search` / `fetch_web` 找到图片
              直链（通常是 .png/.jpg 结尾的地址，或页面里的图片链接），再交给本工具——
              会下载到本地缓存后展示，外链失效也不影响已展示过的历史。
            支持 png/jpg/jpeg/gif/webp/bmp，≤15MB。展示时会自动把图片复制到本地
            持久缓存（data/images/）——之后原文件被删除/修改/移动，历史回看不受影响。

            Args:
                source: 图片路径（相对当前工作目录）或 http(s) 图片直链
                caption: 配一句话说明（可选，显示在图片下方）
            """
            src = str(source or "").strip()
            if not src:
                return "（source 不能为空）"
            if src.lower().startswith(("http://", "https://")):
                return _show_remote_image(src, caption)
            p = _target(src)
            err = _guard(p)
            if err:
                return err
            if not is_within(WS, p):
                return f"（只能展示工作区内的图片：{_rel(p)} 在工作区外，已拒绝）"
            if not p.is_file():
                return f"（图片不存在：{_rel(p)}）"
            if p.suffix.lower() not in IMAGE_EXTS:
                return f"（不是支持的图片格式（{'/'.join(sorted(IMAGE_EXTS))}）：{_rel(p)}）"
            try:
                size = p.stat().st_size
            except OSError as exc:
                return f"（读取失败：{exc}）"
            if size > IMAGE_MAX_BYTES:
                return f"（图片过大（{size // 1024}KB > 15MB），已拒绝：{_rel(p)}）"
            # 先落持久副本再展示：之后原文件被删/改/移动不影响历史回看
            persisted = _persist_image(p) if session is not None else None
            _queue_image(persisted or str(p), _rel(p), caption)
            return f"已展示图片给用户：{_rel(p)}（{size // 1024}KB）。"

        code_tools = [get_cwd, apply_patch, write_file, edit_file, create_dir,
                      move_file, delete_file, glob, grep, run_command, show_image]

    return code_tools


def all_tool_schemas() -> list[dict]:
    """OpenAI function-calling schema 列表（测试/文档用）。"""
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return [convert_to_openai_tool(t) for t in build_tools(None)]


# 低质量结果过滤：工具导航站 / 广告大全 / 纯目录站，对“找干货”帮助不大
_LOW_QUALITY_DOMAINS = {
    "ai-bot.cn", "aigc.cn", "toolify.ai", "top10.com", "futurepedia.io",
    "thereisanaiforthat.com", "aixploria.com", "aitoolnet.com", "aigcbest.com",
    "aitoolhub.com", "aitoolsdirectory.com", "aitoolz.com", "aixploria.com",
    "aigc.cn", "ai-bot.cn", "jimeng.jianying.com",
    # 搜索引擎自家的导航/跳转页（不是内容）
    "hao.360.com", "www.360.cn", "so.com", "www.so.com",
    # 360 的内容聚合站（其它结果的转载，无增量信息）
    "360kuai.com", "www.360kuai.com",
}
_LOW_QUALITY_KEYWORDS = (
    "AI工具", "工具导航", "工具集", "AI创作", "创作平台", "免费AI",
    "AI工具集", "工具大全", "AI网站汇总", "AI Directory", "AI Tools",
    "Top 10", "Best AI", "导航大全",
)


def _is_low_quality(title: str, url: str) -> bool:
    """判断搜索结果是否属于低质量的工具导航/广告聚合站。"""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain in _LOW_QUALITY_DOMAINS:
            return True
    except Exception:
        pass
    low = (title or "").lower()
    return any(kw.lower() in low for kw in _LOW_QUALITY_KEYWORDS)


def _parse_bing_rss(xml_text: str, limit: int) -> list[dict]:
    """解析必应 RSS 搜索结果 → [{title, url, snippet}]（比 HTML 稳定）。"""
    import re as _re
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        def _text(tag):
            el = item.find(tag)
            return el.text or "" if el is not None and el.text else ""
        title = _text("title").strip()
        url = _text("link").strip()
        snippet = _text("description").strip()
        snippet = _re.sub(r"(?s)<[^>]+>", "", snippet).strip()
        if not title or not url.startswith("http"):
            continue
        if _is_low_quality(title, url):
            continue
        items.append({"title": title[:120], "url": url[:300], "snippet": snippet[:300]})
        if len(items) >= limit:
            break
    return items


def _parse_bing_results(html: str, limit: int) -> list[dict]:
    """解析必应搜索结果页 → [{title, url, snippet}]（容错：解析不到就返回空）。"""
    import re as _re

    items = []
    # 结果块：<li class="b_algo">...</li>
    for block in _re.findall(r'(?is)<li class="b_algo".*?</li>', html):
        m_title = _re.search(r'(?is)<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m_title:
            m_title = _re.search(r'(?is)<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m_title:
            continue
        url = m_title.group(1)
        title = _re.sub(r"(?s)<[^>]+>", "", m_title.group(2)).strip()
        if not title or not url.startswith("http"):
            continue
        if _is_low_quality(title, url):
            continue
        m_snip = _re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
        snippet = _re.sub(r"(?s)<[^>]+>", "", m_snip.group(1)).strip() if m_snip else ""
        items.append({"title": title[:120], "url": url[:300], "snippet": snippet[:300]})
        if len(items) >= limit:
            break
    return items

# 模拟浏览器请求头（降低被 412 反爬挡掉的概率）
_WEB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


# ---------------------------------------------------------------- 无头渲染截图
# render_page 用：本机 Chromium 系浏览器（Edge/Chrome）无头渲染网页 → PNG。
# 开发网页/前端时的"眼睛"：改完 → 渲染 → 看图 → 再改。

def _find_browser() -> str | None:
    """找本机可用的 Chromium 系浏览器（Edge/Chrome）用于无头渲染截图。"""
    import shutil as _shutil

    for name in ("msedge", "chrome", "chromium"):
        p = _shutil.which(name)
        if p:
            return p
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.isfile(p):
            return p
    return None


def _prune_screenshots(out_dir, keep: int = 30) -> None:
    """只保留最近 keep 张渲染截图（防 data 目录膨胀）。"""
    try:
        shots = sorted(out_dir.glob("shot_*.png"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in shots[keep:]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------- 搜索引擎
# 优先 DeepSeek 官方 Search/Fetch（2026-10 上线，自研引擎，按次计费 5 元/千次，
# QPS 5）；无官方 Key 或调用失败时回退内置爬虫链。
#
# 内置链：主引擎用 360 搜索（so.com）。实测 cn.bing.com 对**多词/长尾查询**会大量丢词，
# 退化成「只匹配头词」，返回厂商官网首页、百科词条、游戏门户：
#   「喵吉托 萌爪大乱斗 Steam 上线」→「喵（汉语汉字）_百度百科」
#   「小米 语音合成 TTS 模型」     →「小米商城/小米招聘」
# 同一查询换 360 立刻命中（Steam 商店页 / MiMo-V2-TTS 发布稿）。360 对纯英文
# 查询也给得出 arXiv、官方文档级别的结果，所以统一走 360，必应留作兜底。
# 注：百度相关性同样好，但连续几次请求就弹「百度安全验证」，不能用作数据源。
_SO_URL = "https://www.so.com/s"
_BING_URL = "https://www.bing.com/search"
_SEARCH_TIMEOUT = 15

# DeepSeek 官方 Search/Fetch（Base URL 与 chat 相同；Bearer 鉴权）
_DS_SEARCH_PATH = "/v1/tools/search"
_DS_FETCH_PATH = "/v1/tools/fetch"


def _deepseek_search_config(session) -> tuple[str, str]:
    """搜索/抓取用的 DeepSeek 官方配置 → (base_url, api_key)；无 Key 返回 ("", "")。

    Key 来源（与 LLM 的 DeepSeek 分服务商解析同序）：settings.llm_api_key_deepseek
    > 旧字段 llm_api_key > env LLM_API_KEY。base 默认官方端点，可用
    settings.llm_base_url_deepseek 覆盖（与 chat 保持同一端点配置）。
    """
    s = getattr(session, "settings", None) or {}
    base = (str(s.get("llm_base_url_deepseek") or "").strip()
            or "https://api.deepseek.com")
    key = (str(s.get("llm_api_key_deepseek") or "").strip()
           or str(s.get("llm_api_key") or "").strip()
           or os.getenv("LLM_API_KEY", "").strip())
    return (base, key) if key else ("", "")


def _deepseek_search(base: str, key: str, query: str, limit: int) -> list[dict]:
    """DeepSeek 官方 Search → [{title, url, snippet(=summary), date, site}]。

    summary（600~800 字，官方为"给模型阅读"设计）优先于 snippet 作摘要；
    date 统一截成 YYYY-MM-DD 便于展示。
    """
    import httpx

    r = httpx.post(
        base.rstrip("/") + _DS_SEARCH_PATH,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "count": max(1, min(50, limit)), "need_content": False},
        timeout=_SEARCH_TIMEOUT,
    )
    r.raise_for_status()
    items: list[dict] = []
    for it in (r.json().get("results") or []):
        title = str(it.get("title") or "").strip()
        url = str(it.get("url") or "").strip()
        if not title or not url.startswith("http"):
            continue
        items.append({
            "title": title[:120],
            "url": url[:300],
            "snippet": str(it.get("summary") or it.get("snippet") or "").strip()[:800],
            "date": str(it.get("date") or "")[:10],       # RFC3339 → YYYY-MM-DD
            "site": str(it.get("site_name") or "").strip()[:60],
        })
    return items


def _deepseek_fetch(base: str, key: str, url: str) -> tuple[str, str]:
    """DeepSeek 官方 Fetch → (title, markdown 正文)；失败抛异常由调用方兜底。"""
    import httpx

    r = httpx.post(
        base.rstrip("/") + _DS_FETCH_PATH,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"url": url},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    return str(d.get("title") or "").strip(), str(d.get("content") or "")


def _strip_tags(s: str) -> str:
    """去标签 + 反转义 + 压缩空白（搜索结果里有 <em> 高亮标签）。"""
    import re as _re
    from html import unescape as _unescape

    return _re.sub(r"\s+", " ", _unescape(_re.sub(r"(?s)<[^>]+>", "", s))).strip()


def _parse_so_results(html: str, limit: int) -> list[dict]:
    """解析 360 搜索结果页 → [{title, url, snippet}]（解析不到就返回空）。

    结果块形如：
      <h3 class="res-title"><a href="<跳转链>" data-mdurl="<真实URL>">标题</a></h3>
      …<span class="res-list-summary">摘要</span>  或  <p class="res-desc">摘要</p>
    data-mdurl 就是真实地址，省掉跟一次 302。
    """
    import re as _re
    from html import unescape as _unescape

    marks = list(_re.finditer(r'(?is)<h3[^>]*class="res-title[^"]*"[^>]*>(.*?)</h3>', html))
    if not marks:
        return []
    items: list[dict] = []
    for i, m in enumerate(marks):
        head = m.group(1)
        url_m = _re.search(r'(?i)data-mdurl="([^"]+)"', head)
        if url_m:
            url = _unescape(url_m.group(1)).strip()
        else:
            href_m = _re.search(r'(?i)href="([^"]+)"', head)
            url = _unescape(href_m.group(1)).strip() if href_m else ""
        a_m = _re.search(r"(?is)<a[^>]*>(.*?)</a>", head)
        title = _strip_tags(a_m.group(1) if a_m else head)
        if not title or not url.startswith("http"):
            continue
        if _is_low_quality(title, url):
            continue
        # 摘要在标题之后、下一条标题之前
        seg_end = marks[i + 1].start() if i + 1 < len(marks) else m.end() + 6000
        seg = html[m.end():seg_end]
        snippet = ""
        for pat in (r'(?is)class="res-list-summary"[^>]*>(.*?)</span>',
                    r'(?is)class="res-desc"[^>]*>(.*?)</p>',
                    r'(?is)class="res-comm-con"[^>]*>(.*?)</div>'):
            sn = _re.search(pat, seg)
            if sn:
                snippet = _strip_tags(sn.group(1))
                break
        items.append({"title": title[:120], "url": url[:300], "snippet": snippet[:300]})
        if len(items) >= limit:
            break
    return items


def _fetch_so360(query: str, limit: int) -> list[dict]:
    """360 搜索（主引擎）。"""
    import httpx

    r = httpx.get(_SO_URL, params={"q": query}, headers=_WEB_HEADERS,
                  timeout=_SEARCH_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return _parse_so_results(r.text, limit)


def _fetch_bing(query: str, limit: int) -> list[dict]:
    """必应搜索（兜底）。RSS 结构化输出更稳，解析不到再回退 HTML。"""
    import re as _re

    import httpx

    # 多要一些候选，过滤低质量后仍能保留足够结果
    params = {"q": query, "format": "rss", "count": str(max(10, min(20, limit * 3)))}
    # 纯英文查询强制英文搜索结果，避免被中文市场带偏到导航站
    if not _re.search(r"[\u4e00-\u9fff]", query):
        params["ensearch"] = "1"
    r = httpx.get(_BING_URL, params=params, headers=_WEB_HEADERS,
                  timeout=_SEARCH_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    items = _parse_bing_rss(r.text, limit)
    if not items:
        items = _parse_bing_results(r.text, limit)
    return items


def _search_web(query: str, limit: int, session=None) -> tuple[list[dict], str, str]:
    """依次尝试各引擎，返回 (结果列表, 引擎名, 错误信息)。

    有 DeepSeek 官方 Key 时先走官方 Search（自研引擎、摘要长且带日期/站点），
    无 Key 或调用失败时回退内置爬虫链（360 → bing）。只要有一个引擎返回了结果
    就用它；全都没结果且全都报错时 err 非空，让 web_search 能区分「搜索失败」
    和「真的没有结果」。
    """
    errors: list[str] = []
    base, key = _deepseek_search_config(session)
    if key:
        try:
            items = _deepseek_search(base, key, query, limit)
            if items:
                return items, "DeepSeek", ""
        except Exception as exc:                    # 官方失败不致命，回退爬虫链
            _logger.warning("[web_search] DeepSeek 官方搜索失败：%s", exc)
            errors.append(f"deepseek: {exc}")
    for name, fetcher in (("360", _fetch_so360), ("bing", _fetch_bing)):
        try:
            items = fetcher(query, limit)
        except Exception as exc:                    # 单引擎失败不致命，换下一个
            _logger.warning("[web_search] %s 引擎失败：%s", name, exc)
            errors.append(f"{name}: {exc}")
            continue
        if items:
            return items, name, ""
    return [], "", "；".join(errors)


def _extract_links(html: str, base_url: str, limit: int = 20) -> list[tuple[str, str]]:
    """从 HTML 提取页面链接（相对路径补全为绝对地址），返回 [(锚文本, URL)]。

    过滤导航噪音：锚文本过短（<2 字）或过长（>60 字）、纯符号链接、
    javascript:/#/mailto/tel: 链接；按锚文本长度排序（短标题优先，像导航/栏目）。
    """
    import re as _re
    from html import unescape as _unescape
    from urllib.parse import urljoin

    seen = set()
    out: list[tuple[str, str]] = []
    for m in _re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html):
        href = m.group(1).strip()
        text = _re.sub(r"(?s)<[^>]+>", "", m.group(2)).strip()
        text = _re.sub(r"\s+", " ", _unescape(text)).strip()
        low = href.lower()
        if low.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        if not low.startswith(("http://", "https://")):
            href = urljoin(base_url, href)      # 相对路径补全
        if not href.startswith("http"):
            continue
        if not (2 <= len(text) <= 60):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((text, href))
        if len(out) >= limit * 2:
            break
    out.sort(key=lambda x: len(x[0]))           # 短锚文本（栏目/导航）优先
    return out[:limit]
