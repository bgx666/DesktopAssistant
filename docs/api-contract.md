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
role ∈ user / assistant / **thinking**（AI 消息的思考过程，独立条目、位于其正文之前；
持久化在 buffer 的 `additional_kwargs.reasoning_content`，不回传模型端点）/ tool_call / tool_result / memory。

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

## 代码工作区（审批制）

工作区为空时这些能力全部关闭（不注册代码工具、prompt 不注入代码段）。

### GET /workspace
`{workspace, cwd, recent[], permission_mode, enabled, rules[]}`（`rules` = `data/permissions.json`
里「记住并允许」的规则概要）。

### POST /workspace
`{path}`：设置 / 清空（空串）代码工作区；**变更即清空授权规则**并把 cwd 归零。

### POST /workspace/cwd
`{path}`：设置当前工作目录（必须在工作区内且存在；空串 = 根）；不清规则。

### GET /workspace/dirs?path=
工作目录面板：列子目录（相对 cwd）。

### GET /workspace/files?q=
`@` 文件引用补全：以 cwd 为根 rglob（跳过 node_modules 等、≤50 条、q 子串过滤）→
`{ok, files: ["相对路径", ...]}`。

### POST /approval
`{id, decision: allow_once|always|deny, scope?: "file"|"dir"|"workspace"|"cmd"|"cmd_prefix"}`
→ 回填挂起的审批；`always` 时按 `scope` 落规则（省略 scope 保持旧行为）。挂起期间事件为
`approval_request`（含 `scopes` 候选与 `timeout`），回填后推 `approval_resolved`。

### POST /question
`{id, answer?, selected?}`（至少一个有值）→ 回填 `ask_user` 的提问。挂起期间事件
`question_request`（含 `question / options / timeout`），回填/超时/停止后推 `question_resolved`
（带 `status: answered|timeout|stop`）；超时=跳过（模型自行继续），不是拒绝。

### POST /permissions/delete | /permissions/clear
`{index}`（delete）→ 删除单条规则；clear 清空全部。响应带最新 `rules`；**立即生效**（不随保存设置）。

### GET /changes?limit=
文件变更历史（代码工作区写操作留底，`data/changes.db`；新→旧）。
`{ok, changes: [{id, ts, tool, path, rel, before_size, after_size, before_missing, after_missing, no_snapshot, reverted}]}`。

### POST /changes/rollback
`{id}` → **回退到该条之前**（`id ≥` 且未回退的条目新→旧恢复文件）：
`{ok, reverted: [id...], failed: [{id, reason}...]}`；成功后推事件 `changes_update`。

### POST /changes/clear
清空变更历史（**不动任何文件**）。

### GET /llm/providers → GET /llm/models?provider=
服务商预设（`deepseek` / `opencode`）与当前生效配置；有 key 时拉实时模型清单（失败回退预设，无 key 不发网络请求）。

### GET /llm/stats
模型用量/缓存命中统计（最近 50 轮生成）：
`{ok, summary: {count, window, cache_rate|None, avg_tps, avg_in, avg_out, last}, records: [{ts, in, out, hit, cache_known, ms, tps, provider, model, rounds, mock}]}`
（`cache_known=false` = 端点未报缓存字段，不进命中率分母）。

### GET /file?path=
图片展示（`show_image` 工具配套）：只允许**工作区根 / `data/tool_results` / `data/images`**
三个根内的图片（png/jpg/jpeg/gif/webp/bmp，≤15MB），越界 403、防路径穿越。
响应为图片字节 + 正确 Content-Type。

### 事件补充
`changes_update`（变更历史发生变化）、`workspace_changed`、`approval_request` / `approval_resolved`、
`question_request` / `question_resolved`（ask_user 提问弹窗）、
`thinking_stream`（模型思考过程逐块推送 `{content, msg_id}`，**瞬态仅展示**：不落 buffer/历史，
面板重开不重放）、
`stats_update`（一轮生成结束、统计入库后推 `{summary}`）、
`tool_result` 在写/编辑/删除/移动文件时额外带 `diff`（「修改前后」unified diff 文本，数据来自
`changes.db` 留底；move 为说明句，过大/未留底为替代说明）——`/history` 的同名条目也带该字段、
`image` 事件（show_image 展示图片：`{url: "/file?path=…", caption, id}`；`/history` 里有
同构的 `role=image` 条目，重开面板/重启后图片仍在）、
`error`（生成前预检/模型报错 → 前端红色气泡，不静默）。

### view_image（模型读图，无前端事件）
`view_image(source, note)` 把**本地图片**读进模型上下文：工作区内自由、区外走读审批；
图片经 `session._pending_screenshots`（`{url, text, viewed}`）在下一轮注入为 user 消息的
image_url 块（与截屏共用管线）。注入消息带 `additional_kwargs.viewed_image`（normcase 路径，
用于去重；白名单转换不会发给端点），文本以「（」开头 → `/history` 自动过滤、不显示成对话。
