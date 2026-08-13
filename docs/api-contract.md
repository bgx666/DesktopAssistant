# 小助前后端契约（v1）

唯一事实源。后端 `http://127.0.0.1:18771`（HTTP/1.0，JSON，CORS `*`）。

## 事件协议（GET /dequeue）

`GET /dequeue`：一次性 drain 事件队列，响应：

- **长轮询**：`GET /dequeue?wait=N`（N 秒，0~30）——无事件时服务端挂起至多有事件/超时；
  有事件立即返回，实现接近零延迟的事件推送。不带 `wait` 则立即返回（测试/旧客户端兼容）。
- 前端主进程是 `/dequeue` 的唯一消费者，以 `?wait=25` 长轮询自调度（完成后立刻再拉），
  事件与状态统一由主进程按面板形态（气泡/面板）分发。

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
- `text_stream`：逐 token 流式文本（语音连续对话模式的流式 TTS 与面板逐字渲染用）
- `audio`：整句 TTS 合成完成（语音连续对话模式下被前端抑制，改由流式 TTS 接管）
- `tool_call` / `tool_result`：工具调用卡片（实时转圈 / 结果填充）

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

### GET /history
历史消息（重启恢复 / 面板补渲染）：`{"ok": true, "messages": [{role, content, id, …}]}`。

### POST /undo
`{"msg_id": "…"}` → 撤回该消息及其之后的对话 → `{"ok": true}`。

### POST /stop
打断当前生成（生成中才有效）→ `{"ok": true}`。
立即生效：流式循环内检测到停止标志即中止当前模型调用（后台关闭流），
已流式的半句留在面板、不落 buffer；`chat_lock`/生成状态立即释放给下一条消息。

### POST /typing
`{"typing": true|false}` → 用户输入框状态（瞬态，不持久化）。

### GET /next
动态待办队列（按紧急度排序）→ `{"ok": true, "queue": […]}`。

### GET/POST /settings
设置读写（压缩参数 / LLM API / TTS 开关音色，应用即生效、启动恢复）：
- GET → `{"ok": true, "settings": {…}}`
- POST `{"updates": {…}}` → `{"ok": true, "settings": {…}}`（校验失败 400）

### POST /asr
`Content-Type: audio/wav` 上传录音 → `{"ok": true, "text": "…"}`（SenseVoice 本地识别）。

### GET /tts/voices
可用音色列表（zf 女声 / zm 男声）→ `{"ok": true, "voices": [{id, label}]}`。

### GET /tts/say?text=…&voice=…
按需合成整句（voice 可选，临时音色）→ `{"ok": true, "url": "/tts/xxx.wav"}`。

### GET /tts/{32hex}.wav
合成音频下载（仅限白名单文件名，防路径穿越）→ audio/wav 或 audio/mpeg。

## 主动互动机制

- 后端调度线程（daemon，启动即运行）管理两类触发：
  1. **LLM 自主心跳**：agent 调 `heartbeat(minutes, note)` 决定下次醒来（后端 clamp
     10~720 分钟；LLM 忘调时兜底 30 分钟）；
  2. **逾期提醒**：到期未完成的计划条目（每 10 分钟检查，去重）。
- **打开时静默**：启动 2 分钟宽限期不触发任何自主行为；启动时心跳到期只顺延不补说。
- **玩家消息优先**：`_inbox` 有排队消息时 agent 提前结束自主回合让位；生成期间到达的
  消息最多补 3 轮。
- **免打扰**：`in_dnd` 时段内自主触发被 `DndGuardMiddleware` 拦截（玩家消息不受限）。
- **计划快照注入**：每次生成开始时，任务库指纹变化才注入 `[当前计划]` 文本（省 token）。
