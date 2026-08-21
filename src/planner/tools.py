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
        """把任务拆解成阶段 + 待办条目（接下来要做什么）。

        每个待办都要给出具体日期：date_offset 是距阶段开始日的第几天
        （0=当天、1=明天…），系统会换算成具体日期写入队列——不要在内容里
        写"明天/明晚/后天"这类相对时间，它们会随时间失效。"""
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
        acc_days = 0   # 阶段起始日 = 拆解日 + 之前各阶段天数累计
        for seq, ph in enumerate(phases):
            title = str(ph.title or "").strip()
            days = max(1, int(ph.days or 1))
            if not title:
                return f"第 {seq + 1} 个阶段缺少 title。"
            pid = db.add_phase(task_id, seq, title, str(ph.description or ""), days)
            lines.append(f"- 阶段{seq + 1}《{title}》（约{days} 天）：")
            phase_start = date.today() + timedelta(days=acc_days)
            for it in ph.items:
                content = str(it.content or "").strip()
                if not content:
                    continue
                est = max(0, int(it.est_minutes or 0))
                item_date = (phase_start + timedelta(days=int(it.date_offset or 0))).isoformat()
                # 待办带建议日期：跨天后 LLM/前端可见日期已过，不会把
                # "明晚"这类内容当成永远的未来
                db.add_plan_item(task_id, pid, item_date, total_items, content, est, priority=0)
                lines.append(f"  · {content}（{item_date}）" + (f"约{est}分钟" if est else ""))
                total_items += 1
            acc_days += days
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
    def continue_speaking() -> str:
        """需要分点、分段描述时调用本工具：每调用一次，会暂停片刻，然后继续说下一点（一句一句地说）。

        用法：每说一点之前调用一次；说完这一点、还想继续说下一点时再调用。
        所有点都说完了就不要再调用，正常收尾（如调用 heartbeat 结束本轮）。
        不要为了一句普通的话调用它。"""
        session.pause_before_continue()
        return "（收到，继续。请接着说下一点。）"

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
        lines = [f"今天是 {_today()}。动态待办队列（{len(queue)} 项未完成）："]
        if not queue:
            lines.append("目前没有待办，可以问问用户想做什么。")
        for i, p in enumerate(queue, 1):
            due = p.get("task_due") or ""
            due_txt = f"，截止 {due}" if due else ""
            prio_txt = f"，权重 {p['priority']}" if p["priority"] else ""
            date_txt = f"（计划 {p['date']}）" if p.get("date") else "（日期待安排）"
            lines.append(f"  {i}. #{p['id']} {p['content']}{date_txt}（{p['task_title']}{due_txt}{prio_txt}）")
        if s["overdue_tasks"]:
            lines.append("以下任务已逾期，优先处理：")
            for t in s["overdue_tasks"]:
                lines.append(f"  ! #{t['id']}：{t['title']}，截止 {t['due_date']}")
        if s["tasks"]["in_progress"] or s["tasks"]["todo"]:
            lines.append(f"进行中 {s['tasks']['in_progress']} 项，待开始 {s['tasks']['todo']} 项。")
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
    def explore_memory_tree(node_id: str = "") -> str:
        """翻阅记忆树，查看某个历史节点下的记录（对话浓缩摘要）。回忆过去说过的话、做过的决定时用。

        搜索策略：先想清楚要找的内容大概发生在什么时间；如果对单次搜索结果
        不够自信，不要只查一次就放弃——从根节点（node2_xxx）开始逐层往下
        多搜几次，或直接选一个时间范围覆盖目标时段的中间节点（node1_xxx）
        展开；每看一层概要，再决定继续展开哪个子节点，直到找到对应的
        叶子（node0_xxx，含原文）。节点都带时间范围，可按时间缩小范围。

        Args:
            node_id: 节点编号（node0_xxx / node1_xxx / node2_xxx）。
                     留空或传 "root" 时返回根节点概览（整棵树的浓缩与各分支摘要）。
        """
        tree = session.get_memory_tree()
        if not node_id or node_id.strip().lower() in ("root", "根"):
            node_id = tree.get_root_id() or ""
            if not node_id:
                return "记忆树还没有节点（对话尚未触发压缩）。"
        info = tree.get_node_children_info(node_id)
        if info is None:
            return f"找不到节点 {node_id} 的记录。"
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
                session._pending_screenshots.append(url)
                return "（屏幕截图已截取，画面已注入本次对话，请查看截图后回答。）"
            _logger.warning("[screen] 截图转 data URL 失败，回退 OCR")
        # 回退：OCR 文字识别（无视觉模型 / 转换失败）
        from .ocr import ocr_png_from_screen
        text = ocr_png_from_screen(shot)
        if not text:
            return "（屏幕截图已获取，但 OCR 未识别到文字）"
        return f"【屏幕截图 OCR 识别文字】\n{text}"

    @tool(parse_docstring=True)
    def web_search(query: str, limit: int = 5) -> str:
        """搜索互联网，返回相关网页的标题、链接和摘要。需要查最新信息、新闻、资料时使用。

        Args:
            query: 想搜索的内容（原样交给搜索引擎处理）
            limit: 返回结果数（1~8），默认 5
        """
        import re as _re

        import httpx

        q = str(query).strip()
        if not q:
            return "（搜索关键词不能为空）"
        limit = max(1, min(8, int(limit or 5)))
        # 多请求一些候选，过滤低质量后仍能保留足够结果
        params = {"q": q, "format": "rss", "count": str(max(10, min(20, limit * 3)))}
        # 纯英文/非中文查询强制英文搜索结果，避免被中文 Bing 带偏到导航站
        if not _re.search(r"[\u4e00-\u9fff]", q):
            params["ensearch"] = "1"
        try:
            r = httpx.get(
                "https://www.bing.com/search",
                # format=rss 返回结构化 XML，比解析 HTML 更稳定，能拿到多条结果
                params=params,
                headers=_WEB_HEADERS,
                timeout=15,
                follow_redirects=True,
            )
            r.raise_for_status()
        except Exception as exc:
            return f"（搜索失败：{exc}）"
        items = _parse_bing_rss(r.text, limit)
        if not items:
            items = _parse_bing_results(r.text, limit)   # RSS 失败时回退 HTML 解析
        if not items:
            return "（没有搜到结果，换个关键词试试）"
        lines = [f"「{q}」搜索结果（{len(items)} 条）："]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['title']}\n   {it['url']}\n   {it['snippet']}")
        return "\n".join(lines)

    @tool(parse_docstring=True)
    def fetch_web(url: str, max_chars: int = 6000) -> str:
        """抓取一个网页的正文文本，并附上页面里的链接列表（可顺着链接继续找）。
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

    return [create_task, break_down_task, list_tasks, get_task, heartbeat,
            continue_speaking, mark_plan_done, set_do_not_disturb, reschedule,
            update_task_status, get_next_actions, prioritize, explore_memory_tree,
            list_dir, read_file, capture_screen, web_search, fetch_web]


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
