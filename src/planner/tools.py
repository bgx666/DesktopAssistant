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
    """heartbeat 输入：分钟级，clamp 到护栏范围。"""
    minutes: int = Field(description="多少分钟后再醒来做下一件事")
    note: str = Field(default="", description="接下来想做的事（可选），如「到点提醒用户做习题」")

    @field_validator("minutes", mode="before")
    @classmethod
    def clamp_minutes(cls, v):
        return max(_config.PLANNER_HEARTBEAT_MIN_MINUTES,
                   min(_config.PLANNER_HEARTBEAT_MAX_MINUTES, int(v)))


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
        """把任务拆解成阶段 + 逐日计划（每天做什么）。拆解从今天开始排期，phase 的 days 决定阶段跨度。"""
        err = _require_db()
        if err:
            return err
        task = db.get_task(task_id)
        if task is None:
            return f"找不到任务 #{task_id}。"
        if not phases:
            return "phases 不能为空。"
        today = _today()
        cursor = 0
        total_items = 0
        lines = [f"「{task['title']}」拆解完成："]
        for seq, ph in enumerate(phases):
            title = str(ph.title or "").strip()
            days = max(1, int(ph.days or 1))
            if not title:
                return f"第 {seq + 1} 个阶段缺少 title。"
            pid = db.add_phase(task_id, seq, title, str(ph.description or ""), days)
            lines.append(f"- 阶段{seq + 1}《{title}》（{days} 天）：")
            for it in ph.items:
                offset = max(0, int(it.date_offset or 0))
                content = str(it.content or "").strip()
                if not content:
                    continue
                est = max(0, int(it.est_minutes or 0))
                d = TasksDb.add_days(today, cursor + offset)
                db.add_plan_item(task_id, pid, d, total_items, content, est)
                lines.append(f"  · {d} {content}" + (f"（约{est}分钟）" if est else ""))
                total_items += 1
            cursor += days
        if total_items == 0:
            return "拆解失败：没有任何逐日条目。"
        db.update_task_status(task_id, "in_progress", f"拆解为 {len(phases)} 个阶段、{total_items} 条日计划")
        if db.get_phases(task_id) and len(db.get_phases(task_id)) >= 1:
            db.set_phase_status(db.get_phases(task_id)[0]["id"], "active")
        session.push_log(f"任务 #{task_id} 已拆解：{len(phases)} 阶段 / {total_items} 条日计划")
        session.push_event({"type": "plan_update", "date": today})
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
            lines.append("日计划：")
            for p in task["plan_items"]:
                mark = "✓" if p["status"] == "done" else "○"
                lines.append(f"  {mark} {p['date']} #{p['id']} {p['content']}")
        return "\n".join(lines)

    @tool(args_schema=HeartbeatInput)
    def heartbeat(minutes: int, note: str = "") -> str:
        """做完事、回答完用户之后必须调用它停下来。它会让你在指定分钟后再醒来查看情况。不调用你会一直想事情、停不下来。"""
        session.set_heartbeat_state(int(minutes), str(note))
        return f"（我先歇 {minutes} 分钟，{note or '到点再醒'}。）"

    @tool(parse_docstring=True)
    def mark_plan_done(plan_id: int) -> str:
        """勾选完成一条日计划。如果任务的所有条目都完成，任务会自动标记为 done。

        Args:
            plan_id: 日计划条目 id
        """
        err = _require_db()
        if err:
            return err
        items = db.get_plan()
        target = next((p for p in items if p["id"] == plan_id), None)
        if target is None:
            return f"找不到日计划条目 #{plan_id}。"
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
                db.update_task_status(target["task_id"], "done", "全部日计划完成")
                session.push_log(f"任务 #{task['id']}「{task['title']}」已完成！")
        session.push_event({"type": "plan_update", "date": _today()})
        return f"已勾选完成：{target['date']}「{target['content']}」。"

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
    def get_today_plan() -> str:
        """查看今天的计划：今天该做什么、完成多少、有没有逾期未做的。"""
        err = _require_db()
        if err:
            return err
        s = db.summary(_today())
        lines = [f"今天是 {s['today']}："]
        if s["today_plan_undone"]:
            lines.append(f"今日计划 {s['today_plan_done']}/{s['today_plan_total']} 完成，待做：")
            for p in s["today_plan_undone"]:
                lines.append(f"  ○ #{p['id']} {p['content']}")
        else:
            lines.append(f"今日计划 {s['today_plan_total']} 项，全部完成。")
        if s["overdue"]:
            lines.append("逾期未做：")
            for p in s["overdue"][:5]:
                lines.append(f"  ! #{p['id']} {p['date']} {p['content']}")
        if s["tasks"]["in_progress"] or s["tasks"]["todo"]:
            lines.append(f"进行中任务 {s['tasks']['in_progress']} 个，待开始 {s['tasks']['todo']} 个。")
        return "\n".join(lines)

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

    return [create_task, break_down_task, list_tasks, get_task, heartbeat,
            mark_plan_done, set_do_not_disturb, reschedule, update_task_status,
            get_today_plan, explore_memory_tree]


def all_tool_schemas() -> list[dict]:
    """OpenAI function-calling schema 列表（测试/文档用）。"""
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return [convert_to_openai_tool(t) for t in build_tools(None)]
