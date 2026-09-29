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
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config as _config
from .store.calendar_db import CalendarDb

_logger = logging.getLogger("planner.tools")

_TZ = timezone(timedelta(hours=8))

TASK_STATUSES = ("todo", "in_progress", "done", "abandoned")


def _today() -> str:
    return date.today().isoformat()


def _now_str() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")


# ── 输入模型 ──────────────────────────────────────────────────







class HeartbeatInput(BaseModel):
    """heartbeat 输入：**下一次醒来的间隔**——短用 seconds，长用 minutes，二选一。

    间隔由 LLM 按"接下来要做什么 + 用户当前状态（从时间信息判断）"自主决定
    （session.set_heartbeat_state 统一 clamp 到
    [PLANNER_HEARTBEAT_MIN_SECONDS, PLANNER_HEARTBEAT_MAX_MINUTES]）。
    """
    minutes: float | None = Field(
        default=None,
        description="多少分钟后醒来（空闲待命用这个：自己判断用户在忙还是想聊，最大 720 分钟）")
    seconds: float | None = Field(
        default=None,
        description="多少秒后醒来（过一小会儿要回来看看时用；最短 1 分钟 = 60 秒）")
    note: str = Field(default="", description="接下来想做的事（可选），如「接着读下一个文件」")

    @model_validator(mode="after")
    def require_one(self):
        if self.minutes is None and self.seconds is None:
            raise ValueError("minutes 与 seconds 至少填一个：短间隔填 seconds，长间隔填 minutes")
        return self


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
    db: CalendarDb | None = getattr(session, "db", None)

    def _require_db():
        if db is None:
            return "（日历库不可用）"
        return None





    @tool(args_schema=HeartbeatInput)
    def heartbeat(minutes: float | None = None, note: str = "",
                  seconds: float | None = None) -> str:
        """设置「下一次醒来」的间隔，然后结束本轮。到点后你会被唤醒。

        心跳是个**定时器**，不是继续干活的手段——同一轮里要继续做事，
        直接接着调工具就行（循环会一直跑到你不再调工具为止）。每轮收尾都要调用它。
        间隔按"接下来要做什么 + 用户现在什么状态"自己判断：
        - **一批事做完了**，没什么要紧的 → 用 `minutes` 设几十分钟到几小时
          （用户在忙或空闲时更长，最多 720 分钟 = 12 小时）；
        - **过一小会儿要回来看看**（如稍后再查一次）→ 用 `seconds` 设很短；
        - **要定时提醒**（如"晚上 8 点提醒我"）→ **用日历的 `add_schedule`（time 填 "20:00"）**，
          到点系统会自己叫你；心跳不做这件事，别再把时钟折算成分钟数。

        用户状态靠**时间信息**判断：每条用户消息带 `[HH:MM]` 和"距上次说话多久"，
        心跳提示里也会给你"现在几点"和"距用户上次说话多久"。聊得热络就设短些，
        明显在忙自己的事就设长些——没有固定规则，你自己定。
        不要因为"刚跟用户说过话"就设短心跳去跟进。"""
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


    @tool(args_schema=DndInput)
    def set_do_not_disturb(enabled: bool, until_hour: int | None = None) -> str:
        """开启或关闭免打扰。开启后这段时间内你不会主动打扰用户（用户找你仍然回复）。"""
        session.set_dnd(enabled, until_hour)
        if enabled:
            desc = f"直到 {until_hour}:00" if until_hour is not None else "（默认 22:00-08:00 窗口）"
            return f"已开启免打扰{desc}。期间我不会主动打扰你。"
        return "已关闭免打扰，恢复正常提醒。"



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

        关键词要**短而准**：用 2~5 个「专有名词/术语」拼起来，别写成一整句自然语言问题。
        长句会被搜索引擎拆词降权，反而搜回一堆泛泛的官网首页和百科词条。
        例：查小米的语音模型用「小米 MiMo 语音 模型」，不要用「小米公司有没有自己的语音大模型」。
        摘要只是节选，判断某条是否真有用要再用 fetch_web 打开正文。

        Args:
            query: 短关键词组合（专有名词优先）
            limit: 返回结果数（1~8），默认 5
        """
        q = str(query).strip()
        if not q:
            return "（搜索关键词不能为空）"
        limit = max(1, min(8, int(limit or 5)))
        items, engine, err = _search_web(q, limit)
        if not items:
            if err:
                return f"（搜索失败：{err}）"
            return f"（没有搜到「{q}」的结果，换个更短的关键词试试）"
        lines = [f"「{q}」搜索结果（{len(items)} 条，来源 {engine}）："]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['title']}\n   {it['url']}\n   {it['snippet']}")
        lines.append("（摘要只是节选，想确认某条是否真有用，用 fetch_web 打开正文。）")
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

    return [heartbeat, continue_speaking, set_do_not_disturb,
            list_schedule, add_schedule, update_schedule, cancel_schedule,
            explore_memory_tree, list_dir, read_file, capture_screen,
            web_search, fetch_web]


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


# ---------------------------------------------------------------- 搜索引擎
# 主引擎用 360 搜索（so.com）。实测 cn.bing.com 对**多词/长尾查询**会大量丢词，
# 退化成「只匹配头词」，返回厂商官网首页、百科词条、游戏门户：
#   「喵吉托 萌爪大乱斗 Steam 上线」→「喵（汉语汉字）_百度百科」
#   「小米 语音合成 TTS 模型」     →「小米商城/小米招聘」
# 同一查询换 360 立刻命中（Steam 商店页 / MiMo-V2-TTS 发布稿）。360 对纯英文
# 查询也给得出 arXiv、官方文档级别的结果，所以统一走 360，必应留作兜底。
# 注：百度相关性同样好，但连续几次请求就弹「百度安全验证」，不能用作数据源。
_SO_URL = "https://www.so.com/s"
_BING_URL = "https://www.bing.com/search"
_SEARCH_TIMEOUT = 15


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


def _search_web(query: str, limit: int) -> tuple[list[dict], str, str]:
    """依次尝试各引擎，返回 (结果列表, 引擎名, 错误信息)。

    只要有一个引擎返回了结果就用它；全都没结果且全都报错时 err 非空，
    让 web_search 能区分「搜索失败」和「真的没有结果」。
    """
    errors: list[str] = []
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
