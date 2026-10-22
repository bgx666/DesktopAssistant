# prompts/ —— 提示词清单与约定

所有**文件类**提示词都在这里，通过 `src/planner/prompt_loader.py` 的注册表
（`PROMPTS`）按逻辑名加载：

```python
from . import prompt_loader
prompt_loader.load("runtime/heartbeat_wake")          # 原文
prompt_loader.render("runtime/calendar_due", time=..., items=...)  # 填占位符
```

- 新增/改名提示词 → **同时**登记到 `prompt_loader.PROMPTS` 和本文表格；
  `tests/test_prompts.py` 会校验注册表里的文件都存在且非空。
- 占位符用 `{name}`；`render()` 缺 key 会抛 `PromptRenderError`（不会静默漏值）。
  含 JSON 花括号的文本（压缩骨架）**不要**走 `render()`，用 `load()` 原样拼接。
- 工具 docstring 仍是提示词的一部分，但由 LangChain 从 `@tool` 函数签名生成，
  不能外置——见文末。

## 1. 系统提示词（每次模型调用作为 SystemMessage）

| 逻辑名 | 文件 | 说明 |
|---|---|---|
| `system` | `system.md` | 人格/职责/说话规约/记忆一致性（12.5k 字符，小助的全部人设） |
| `workspace` | `workspace.md` | 启用代码工作区时追加；占位符 `{workspace}` `{cwd}` `{permission_mode}` |

组装：`session._build_system_prompt()` → `create_agent(system_prompt=...)`。
除这两份外，还会动态追加工作区根 / 当前目录的 `AGENTS.md`（各限 8KB）与当前
目录清单（最多 40 项）；**切工作区/工作目录/权限模式**时重建（前缀缓存清零）。

## 2. 运行时注入（HumanMessage 进 recent_buffer，会持久化、会进压缩）

`_receive(..., injected=True, kind=...)` 的产品都带 `metadata.injected=True`；
`/history` 靠它隐藏内部消息（旧 buffer 仍按 `（` / `[当前` 前缀兜底）。

| 逻辑名 | 文件 | 注入点 / 触发 | kind（回合边界？） |
|---|---|---|---|
| `runtime/player_message` | `runtime/player_message.md` | `enqueue_player_message`：`[HH:MM]（距上次说话 X）用户对你说：…` | 不是注入（真实用户消息） |
| `runtime/heartbeat_wake` | `runtime/heartbeat_wake.md` | `_fire_heartbeat` 到点唤醒 | `heartbeat`（是） |
| `runtime/calendar_due` | `runtime/calendar_due.md` | `_check_calendar_due` 日历到点提醒 | `calendar_due`（是） |
| `runtime/calendar_overdue` | `runtime/calendar_overdue.md` | `_check_calendar_overdue` 逾期追问（跳过已提醒） | `calendar_overdue`（是） |
| `runtime/calendar_edit` | `runtime/calendar_edit.md` | 用户在日历页增改取消（`trigger=False`） | `calendar_edit`（否） |
| `runtime/attachments_header` | `runtime/attachments_header.md` | `_render_attachments`：`【拖入的文件】` | 随用户消息 |
| `runtime/attachment_text` | `runtime/attachment_text.md` | 文本文件直注 | 随用户消息 |
| `runtime/attachment_doc` | `runtime/attachment_doc.md` | PDF/Word 解析 + 预览 | 随用户消息 |
| `runtime/attachment_doc_fail` | `runtime/attachment_doc_fail.md` | 文档解析失败 | 随用户消息 |
| `runtime/attachment_image` | `runtime/attachment_image.md` | 视觉模型：原图直发 | 随用户消息 |
| `runtime/attachment_image_ocr` | `runtime/attachment_image_ocr.md` | 非视觉模型：OCR 文字 | 随用户消息 |
| `runtime/attachment_image_ocr_fail` | `runtime/attachment_image_ocr_fail.md` | OCR 无结果 | 随用户消息 |
| `runtime/attachment_other` | `runtime/attachment_other.md` | 其它文件仅路径 | 随用户消息 |
| `runtime/screenshot` | `runtime/screenshot.md` | `capture_screen` 的图片注入文本 | `image`（否，`ScreenShotInjectMiddleware`） |
| `runtime/view_image` | `runtime/view_image.md` | `view_image` 的图片注入文本 | `image`（否） |
| `runtime/limit_reached` | `runtime/limit_reached.md` | 单轮调用上限保险丝（替换英文注入） | AI 消息 |
| `runtime/ask_user_selected` | `runtime/ask_user_selected.md` | ask_user 选项结果 | 工具结果 |
| `runtime/ask_user_selected_note` | `runtime/ask_user_selected_note.md` | 选项 + 补充回答 | 工具结果 |
| `runtime/ask_user_answer` | `runtime/ask_user_answer.md` | 自由文本回答 | 工具结果 |
| `runtime/ask_user_stop` | `runtime/ask_user_stop.md` | 用户停止本轮 | 工具结果 |
| `runtime/ask_user_timeout` | `runtime/ask_user_timeout.md` | 提问超时 | 工具结果 |
| `runtime/white_finished` | `runtime/white_finished.md` | White 完成/失败/停止 → 通知主助手验收 | `white`（注入） |

> `/history` 的回合边界只认：真实用户消息、`kind ∈ {heartbeat, calendar_due,
> calendar_overdue}`、旧 buffer 的 `（系统：` 前缀；其它注入不切块。

## 2.5 子代理 White（后台、无长期记忆）

| 逻辑名 | 文件 | 说明 |
|---|---|---|
| `agents/white` | `agents/white.md` | White 的角色/工作方式/报告要求（无记忆、不能问用户、不能执行命令） |
| `agents/white_compact` | `agents/white_compact.md` | White 上下文超预算时的"工作记忆"压缩指令 |
| `runtime/white_finished` | `runtime/white_finished.md` | 任务结束通知主助手验收（status/summary/files） |

White 不使用记忆树/压缩节点；上下文由 `WhiteContextMiddleware` 做"摘要 + 滑动窗口"。
任务/结果协议走 `white_task` / `finish_task` 的 pydantic tool schema（改动文件与用量由运行器收集）。

## 3. 压缩提示词（独立摘要模型，不是主会话）

每次叶子/父层压缩 = `SystemMessage(主会话 system_prompt)` + 带标记的上下文 +
以下指令（中/英 × 叶子/分片/父层）；分片指令末尾拼 `skeleton_*`（JSON 形状）。

| 逻辑名 | 文件 |
|---|---|
| `compress/leaf_zh` / `compress/leaf_en` | 叶子压缩指令（占位符 `{scope}{cwd_line}{budget_line}`） |
| `compress/leaf_scope_zh` / `_en` | 整批/独立分片的范围行（`{marker}{start}{end}`） |
| `compress/leaf_scope_chunk_zh` / `_en` | 渐进前缀分片的范围行（头/尾锚点） |
| `compress/parent_zh` / `_en` | 父层合并指令（`{n}{ids}{marker}{budget_line}`） |
| `compress/skeleton_zh` / `_en` | JSON 形状示例（`load()` 原样追加） |
| `compress/budget_zh` / `_en` | 长度预算行（`{chars}{summary}{rest}`） |

预算口径：中文设置 N ≈ 整节点 N 字；`_apply_budget` 另有总量硬兜底（≤1.1×预算）。

## 4. 工具 schema（在代码里，不在 prompts/）

`src/planner/tools.py` 的 26 个 `@tool` 函数 docstring + pydantic 参数描述
会由 LangChain 生成 JSON schema，随每次请求发送给模型；代码工作区工具仅在
`workspace_root` 非空时注册。修改工具说明请直接改 docstring。
