"""进程内工具注册表（langchain @tool 工厂，参照 yaya backend 选型）。

工具集：任务录入/拆解/跟进/改期 + 记忆树翻阅 + heartbeat + 免打扰。
输入模型用 pydantic（多参数 + 枚举/范围约束），闭包捕获 session。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

from . import config as _config
from .store.tasks_db import TasksDb

_logger = logging.getLogger("planner.tools")

_TZ = timezone(timedelta(hours=8))

TASK_STATUSES = ("todo", "in_progress", "done", "abandoned")


def _today() -> str:
    return date.today().isoformat()


def _now_str() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")


# ── 输入模型 ──────────────────────────────────────────────────

class PlanItemInput(BaseModel):
    """拆解后的一天条目。"""
    date_offset: int = Field(default=0, description="距阶段开始的第几天（0=阶段第一天）", ge=0)
    content: str = Field(description="这一天具体做什么")
    est_minutes: int = Field(default=0, description="预计耗时（分钟），不确定填 0", ge=0)


class PhaseInput(BaseModel):
    """拆解出的一个阶段。"""
    title: str = Field(description="阶段名称，如「概念梳理」「习题巩固」")
    description: str = Field(default="", description="阶段说明（可选）")
    days: int = Field(default=1, description="这个阶段计划持续几天", ge=1)
    items: list[PlanItemInput] = Field(default_factory=list, description="阶段内的逐日条目")


class BreakDownInput(BaseModel):
    task_id: int = Field(description="要拆解的任务 id")
    phases: list[PhaseInput] = Field(description="任务拆解出的全部阶段（按先后顺序）")


class HeartbeatInput(BaseModel):
    """heartbeat 输入：分钟级（支持小数，0.2 = 12 秒），clamp 到护栏范围。"""
    minutes: float = Field(description="多少分钟后再醒来做下一件事（支持小数：0.2 = 12 秒，聊天中可用 0.17~1 的秒级短心跳）")
    note: str = Field(default="", description="接下来想做的事（可选），如「到点提醒用户做习题」")

    @field_validator("minutes", mode="before")
    @classmethod
    def clamp_minutes(cls, v):
        return max(_config.PLANNER_HEARTBEAT_MIN_MINUTES,
                   min(_config.PLANNER_HEARTBEAT_MAX_MINUTES, float(v)))


class DndInput(BaseModel):
    enabled: bool = Field(description="是否开启免打扰")
    until_hour: int | None = Field(default=None, description="免打扰到几点（0-23，可选）；不填用默认窗口（22-8）")

    @field_validator("until_hour", mode="before")
    @classmethod
    def clamp_hour(cls, v):
        if v is None:
            return None
        return max(0, min(23, int(v)))


# ── 工具工厂 ──────────────────────────────────────────────────

def build_tools(session) -> list[BaseTool]:
    """闭包捕获 session 的工具工厂。session 可为 None（仅生成 schema 用）。"""
    db: TasksDb | None = getattr(session, "db", None)

    def _require_db():
        if db is None:
            return "（任务库不可用）"
        return None

    @tool(parse_docstring=True)
    def create_task(title: str, description: str, due_date: str, priority: str) -> str:
        """记录一个新任务。

        Args:
            title: 任务名称
            description: 任务描述、背景或目标
            due_date: 截止日期，格式 YYYY-MM-DD
            priority: 优先级 low / normal / high
        """
        err = _require_db()
        if err:
            return err
        if priority not in ("low", "normal", "high"):
            return f"priority 必须是 low/normal/high，收到 {priority!r}"
        tid = db.create_task(title, description, due_date, priority)
        session.push_log(f"新任务 #{tid}：{title}")
        return f"已记录任务 #{tid}「{title}」，截止 {due_date}，优先级 {priority}。"

    @tool(args_schema=BreakDownInput)
    def break_down_task(task_id: int, phases: list[PhaseInput]) -> str:
        """把任务拆解成阶段 + 待办条目（接下来要做什么）。拆解不排固定日期——
        每天做什么由 get_next_actions 按紧急度动态安排。"""
        err = _require_db()
        if err:
            return err
        task = db.get_task(task_id)
        if task is None:
            return f"找不到任务 #{task_id}。"
        if not phases:
            return "phases 不能为空。"
        total_items = 0
        lines = [f"「{task['title']}」拆解完成："]
        for seq, ph in enumerate(phases):
            title = str(ph.title or "").strip()
            days = max(1, int(ph.days or 1))
            if not title:
                return f"第 {seq + 1} 个阶段缺少 title。"
            pid = db.add_phase(task_id, seq, title, str(ph.description or ""), days)
            lines.append(f"- 阶段{seq + 1}《{title}》（约{days} 天）：")
            for it in ph.items:
                content = str(it.content or "").strip()
                if not content:
                    continue
                est = max(0, int(it.est_minutes or 0))
                # 动态待办：不排固定日期，由优先级/截止时间决定先后
                db.add_plan_item(task_id, pid, None, total_items, content, est, priority=0)
                lines.append(f"  · {content}" + (f"（约{est}分钟）" if est else ""))
                total_items += 1
        if total_items == 0:
            return "拆解失败：没有任何待办条目。"
        db.update_task_status(task_id, "in_progress", f"拆解为 {len(phases)} 个阶段、{total_items} 条待办")
        if db.get_phases(task_id) and len(db.get_phases(task_id)) >= 1:
            db.set_phase_status(db.get_phases(task_id)[0]["id"], "active")
        session.push_log(f"任务 #{task_id} 已拆解：{len(phases)} 阶段 / {total_items} 条待办")
        session.push_event({"type": "plan_update", "date": _today()})
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def list_tasks(status: str) -> str:
        """查看任务列表。

        Args:
            status: 按状态过滤：todo / in_progress / done / abandoned，全部填 all
        """
        err = _require_db()
        if err:
            return err
        if status in ("all", "", "全部"):
            status = None
        tasks = db.list_tasks(status)
        if not tasks:
            return "目前没有任务。"
        lines = ["任务列表："]
        for t in tasks:
            lines.append(
                f"- #{t['id']}「{t['title']}」[{t['status']}] {t['priority']} "
                f"截止 {t['due_date'] or '未定'} "
                f"（进度 {t['plan_done']}/{t['plan_total']}，{t['phase_count']} 阶段）"
            )
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def get_task(task_id: int) -> str:
        """查看一个任务的详情（含阶段与日计划）。

        Args:
            task_id: 任务 id
        """
        err = _require_db()
        if err:
            return err
        task = db.get_task(task_id)
        if task is None:
            return f"找不到任务 #{task_id}。"
        lines = [f"#{task['id']}「{task['title']}」[{task['status']}] 截止 {task['due_date'] or '未定'}"]
        if task["description"]:
            lines.append(f"描述：{task['description']}")
        for ph in task["phases"]:
            lines.append(f"· 阶段{ph['seq'] + 1}《{ph['title']}》[{ph['status']}]（{ph['days']} 天）")
        if task["plan_items"]:
            lines.append("待办：")
            for p in task["plan_items"]:
                mark = "✓" if p["status"] == "done" else "○"
                d = p["date"] or "（动态安排）"
                lines.append(f"  {mark} {d} #{p['id']} {p['content']}")
        return "\n".join(lines)

    @tool(args_schema=HeartbeatInput)
    def heartbeat(minutes: float, note: str = "") -> str:
        """设置下次定时唤醒（定时任务）的时间，然后停下来休息。到点后你会醒来检查任务进度、主动和用户说话。

        心跳是分钟级定时任务，不是对话跟进：minutes 最小 10 分钟（10~720）。
        即使刚回答完用户，也不要设置低于 10 分钟的间隔去跟进；
        用户说话后不要重置心跳——保持你原来定的时间（一人一句）。"""
        session.set_heartbeat_state(float(minutes), str(note))
        return f"（我先歇 {session._fmt_duration(float(minutes))}，{note or '到点再醒'}。）"

    @tool(parse_docstring=True)
    def mark_plan_done(plan_id: int) -> str:
        """勾选完成一条待办（用户确认做完了才调用——不做就不推进进度）。
        如果任务的所有待办都完成，任务自动标记为 done。

        Args:
            plan_id: 待办条目 id
        """
        err = _require_db()
        if err:
            return err
        items = db.list_pending()
        target = next((p for p in items if p["id"] == plan_id), None)
        if target is None:
            return f"找不到待办 #{plan_id}。"
        if not db.set_plan_status(plan_id, "done"):
            return f"勾选 #{plan_id} 失败。"
        # 阶段/任务自动推进
        task = db.get_task(target["task_id"])
        if task is not None:
            for ph in task["phases"]:
                if ph["id"] == target.get("phase_id"):
                    ph_items = [p for p in task["plan_items"] if p.get("phase_id") == ph["id"]]
                    if ph_items and all(p["status"] == "done" for p in ph_items):
                        db.set_phase_status(ph["id"], "done")
            if task["plan_items"] and all(p["status"] == "done" for p in task["plan_items"]):
                db.update_task_status(target["task_id"], "done", "全部待办完成")
                session.push_log(f"任务 #{task['id']}「{task['title']}」已完成！")
        session.push_event({"type": "plan_update", "date": _today()})
        return f"已勾选完成：「{target['content']}」。"

    @tool(args_schema=DndInput)
    def set_do_not_disturb(enabled: bool, until_hour: int | None = None) -> str:
        """开启或关闭免打扰。开启后这段时间内你不会主动打扰用户（用户找你仍然回复）。"""
        session.set_dnd(enabled, until_hour)
        if enabled:
            desc = f"直到 {until_hour}:00" if until_hour is not None else "（默认 22:00-08:00 窗口）"
            return f"已开启免打扰{desc}。期间我不会主动打扰你。"
        return "已关闭免打扰，恢复正常提醒。"

    @tool(parse_docstring=True)
    def reschedule(task_id: int, new_due_date: str) -> str:
        """调整任务的截止日期（进度落后或提前完成时用）。

        Args:
            task_id: 任务 id
            new_due_date: 新截止日期 YYYY-MM-DD
        """
        err = _require_db()
        if err:
            return err
        if not db.update_task(task_id, due_date=new_due_date):
            return f"找不到任务 #{task_id}。"
        db.add_review(task_id=task_id, summary=f"改期至 {new_due_date}")
        return f"任务 #{task_id} 截止日期调整为 {new_due_date}。"

    @tool(parse_docstring=True)
    def update_task_status(task_id: int, status: str) -> str:
        """更新任务状态（todo/in_progress/done/abandoned）。

        Args:
            task_id: 任务 id
            status: 新状态
        """
        err = _require_db()
        if err:
            return err
        if status not in TASK_STATUSES:
            return f"状态必须是 {'/'.join(TASK_STATUSES)}，收到 {status!r}"
        if not db.update_task_status(task_id, status, f"状态更新为 {status}"):
            return f"找不到任务 #{task_id}。"
        session.push_log(f"任务 #{task_id} 状态 → {status}")
        return f"任务 #{task_id} 状态已更新为 {status}。"

    @tool(parse_docstring=True)
    def get_next_actions() -> str:
        """查看接下来该做什么：按 紧急度（新任务插队、deadline 临近）动态排序的待办队列。
        这是你安排用户行动的依据——每次规划都先调它。"""
        err = _require_db()
        if err:
            return err
        s = db.summary(_today())
        queue = s["queue"]
        lines = [f"动态待办队列（共 {len(queue)} 项未完成）："]
        if not queue:
            lines.append("（目前没有待办，可以问问用户最近想做什么。）")
        for i, p in enumerate(queue, 1):
            due = p.get("task_due") or ""
            due_txt = f"，截止 {due}" if due else ""
            prio_txt = f"，权重 {p['priority']}" if p["priority"] else ""
            lines.append(f"  {i}. #{p['id']} {p['content']}（{p['task_title']}{due_txt}{prio_txt}）")
        if s["overdue_tasks"]:
            lines.append("已逾期任务（优先处理）：")
            for t in s["overdue_tasks"]:
                lines.append(f"  ! #{t['id']}「{t['title']}」截止 {t['due_date']}")
        if s["tasks"]["in_progress"] or s["tasks"]["todo"]:
            lines.append(f"进行中任务 {s['tasks']['in_progress']} 个，待开始 {s['tasks']['todo']} 个。")
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def prioritize(plan_id: int) -> str:
        """把某条待办提到队列最前（突发要紧事插队用，如用户说「先做这个」）。

        Args:
            plan_id: 待办条目 id（get_next_actions 里的 #id）
        """
        err = _require_db()
        if err:
            return err
        items = db.list_pending()
        target = next((p for p in items if p["id"] == plan_id), None)
        if target is None:
            return f"找不到待办 #{plan_id}。"
        db.bump_item_priority(plan_id)
        db.add_review(task_id=target["task_id"], summary=f"优先处理：{target['content']}")
        session.push_log(f"待办 #{plan_id} 已插队到最前")
        session.push_event({"type": "plan_update", "date": _today()})
        return f"「{target['content']}」已提到队列最前，接下来先做它。"

    @tool(parse_docstring=True)
    def explore_memory_tree(node_id: str) -> str:
        """翻开记忆笔记，查看以前和用户说过的话、做过的决定。

        Args:
            node_id: 笔记编号——对话里出现的 [node0_001] 这种
        """
        tree = session.get_memory_tree()
        info = tree.get_node_children_info(node_id)
        if info is None:
            return f"找不到第 {node_id} 页的记录。"
        return json.dumps(info, ensure_ascii=False)

    # ── 文件读取（只读；本工具集不存在任何写文件工具）──────────

    FILE_READ_LIMIT = 4000        # 单次读取字符上限（约 2000 token）
    FILE_MAX_BYTES = 50 * 1024 * 1024  # 超过该大小的文件拒绝读取

    @tool(parse_docstring=True)
    def list_dir(path: str) -> str:
        """查看一个目录下有什么（文件和子目录），探索用户电脑上的文件用。
        只读操作。

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
    def read_file(path: str, start: int = 0, limit: int = 4000) -> str:
        """读取电脑上的文本文件（只读，不能修改任何文件）。
        大文件分段读：先用默认 start=0 读开头，再调大 start 继续读后面。

        Args:
            path: 文件绝对路径，如 D:\\project\\main.py
            start: 从第几个字符开始读（分段用），默认 0
            limit: 本次最多读取的字符数（1~8000），默认 4000
        """
        import os
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return f"无法访问文件：{exc}"
        if size > FILE_MAX_BYTES:
            return f"文件过大（{size / 1024 / 1024:.0f}MB），拒绝读取。"
        # 先探测二进制（null 字节特征），再只读解码
        try:
            with open(path, "rb") as f:
                head = f.read(2048)
            if b"\x00" in head:
                return "这是二进制文件，无法读取。"
        except OSError as exc:
            return f"无法读取文件：{exc}"
        raw = None
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                with open(path, "r", encoding=enc, errors="strict") as f:
                    raw = f.read()
                encoding = enc
                break
            except (UnicodeDecodeError, OSError):
                continue
        if raw is None:
            return "无法解码为文本（可能是二进制或特殊编码）。"
        limit = max(1, min(8000, int(limit or FILE_READ_LIMIT)))
        start = max(0, int(start or 0))
        total = len(raw)
        if start >= total:
            return f"（start={start} 超出文件长度 {total}，已到结尾）"
        chunk = raw[start:start + limit]
        head_info = f"{path}（共 {total} 字符，本次显示 {start}-{start + len(chunk)}）"
        if start + len(chunk) < total:
            head_info += "，如需继续可用 start 参数读取后续部分"
        return f"{head_info}：\n{chunk}"

    @tool(parse_docstring=True)
    def capture_screen() -> str:
        """截取当前主屏的屏幕截图，用 OCR 识别屏幕上的文字并返回。

        用户让你"看一下屏幕 / 看看我正在做什么 / 屏幕上有什么"时使用。
        只返回识别出的文字（不包含图片本身），来源标注为屏幕截图。
        """
        import mss

        from .ocr import ocr_png_from_screen
        try:
            with mss.MSS() as sct:
                shot = sct.grab(sct.monitors[1])   # 主屏
            text = ocr_png_from_screen(shot)
            if not text:
                return "（屏幕截图已获取，但 OCR 未识别到文字）"
            return f"【屏幕截图 OCR 识别文字】\n{text}"
        except Exception as exc:
            return f"（屏幕截图失败：{exc}）"

    return [create_task, break_down_task, list_tasks, get_task, heartbeat,
            mark_plan_done, set_do_not_disturb, reschedule, update_task_status,
            get_next_actions, prioritize, explore_memory_tree,
            list_dir, read_file, capture_screen]


def all_tool_schemas() -> list[dict]:
    """OpenAI function-calling schema 列表（测试/文档用）。"""
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return [convert_to_openai_tool(t) for t in build_tools(None)]
