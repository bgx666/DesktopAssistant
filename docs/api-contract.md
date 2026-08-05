# 小助前后端契约（v1）

唯一事实源。后端 `http://127.0.0.1:18771`（HTTP/1.0，JSON，CORS `*`）。

## 事件协议（GET /dequeue）

`GET /dequeue`：一次性 drain 事件队列，响应：

```json
{
  "ok": true,
  "events": [
    {"type": "text", "content": "…", "from": "assistant"},
    {"type": "log", "text": "…"},
    {"type": "thinking", "value": true},
    {"type": "dnd", "enabled": true},
    {"type": "plan_update", "date": "2026-08-05"}
  ],
  "state": { "...": "见 GET /state" }
}
```

事件类型：
- `text`：小助的发言（流式：每轮 LLM 返回即推送，不等整个 ReAct 循环结束）
- `log`：系统日志（任务建立/状态变更/免打扰跳过等）
- `thinking`：生成中状态（前端驱动"思考中"动画）
- `dnd`：免打扰状态变化
- `plan_update`：计划/任务数据变化（前端触发刷新）

## 端点

### GET /init
后端标识 + 角色 + 状态。

```json
{"ok": true, "backend": "planner/1", "mode": "mock|llm",
 "char": {"name": "assistant", "display_name": "小助"},
 "state": {…}}
```

### GET /state
```json
{"ok": true, "state": {
  "mode": "mock|llm",
  "thinking": false,
  "heartbeat": {"in_minutes": 60, "note": "到点提醒用户做习题"},
  "dnd": {"enabled": true, "in_dnd": false, "until": null},
  "plan": {"today": "2026-08-05",
           "tasks": {"todo": 2, "in_progress": 1, "done": 0, "abandoned": 0},
           "today_plan_total": 3, "today_plan_done": 1,
           "overdue_count": 1},
  "activity": ""
}}
```

### POST /chat
`{"message": "…"}` → `{"ok": true}`（异步生成，回复走 /dequeue 流式）。

### POST /task（结构化录入，不经 LLM）
`{"title": "…", "description": "…", "due_date": "YYYY-MM-DD", "priority": "low|normal|high"}`
→ `{"ok": true, "id": 1}`。

### GET /tasks?status=all
任务列表（含 `plan_total/plan_done/phase_count`）。

### GET /plan?date=YYYY-MM-DD
当日计划条目（含 `task_title/phase_title` 联查）。

### POST /plan/done
`{"plan_id": 1}` → 勾选完成（任务所有条目完成时任务自动 done），并通知 agent 跟进（agent 会主动回复）。→ `{"ok": true}`。

### POST /dnd
`{"enabled": true, "until_hour": 14}`（until_hour 可选）→ `{"ok": true, "dnd": {…}}`。

### POST /nudge
手动戳一下：注入一条消息并触发一次自主生成 → `{"ok": true}`。

### POST /toggle_mock
运行时切换 Mock/真实 LLM → `{"ok": true, "mode": "mock|llm"}`。

## 主动互动机制

- 后端调度线程（daemon，启动即运行）管理三类触发：
  1. **LLM 自主心跳**：agent 调 `heartbeat(minutes, note)` 决定下次醒来（后端 clamp
     10~720 分钟；LLM 忘调时兜底 60 分钟）；
  2. **定时触发点**：早晨 `PLANNER_MORNING_HOUR`（默认 8 点）播报今日计划、晚间
     `PLANNER_EVENING_HOUR`（默认 21 点）回顾（每天一次，跨天重置）；
  3. **逾期提醒**：到期未完成的计划条目（每 10 分钟检查，去重）。
- **玩家消息优先**：`_inbox` 有排队消息时 agent 提前结束自主回合让位；生成期间到达的
  消息最多补 3 轮。
- **免打扰**：`in_dnd` 时段内自主触发被 `DndGuardMiddleware` 拦截（玩家消息不受限）。
- **计划快照注入**：每次生成开始时，任务库指纹变化才注入 `[当前计划]` 文本（省 token）。
