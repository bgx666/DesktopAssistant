# 小助代码 Agent — 第一期实施计划

> 主题：把 PC 版小助从「只读陪伴型助手」扩展为「本地代码助手（读写 + 检索）」。
> 状态：**P0 / P1 / P1.5 / P2 / P3 已实现（2026-10-17，测试 273 全绿）**，回填见「十三、实现记录」。
> 相关：[`README.md`](README.md)、[`tour.md`](tour.md)、`../AGENTS.md`、`../android/app/src/main/assets/prompts/README.md`、
> **第二轮方案：[`代码Agent-界面升级方案.md`](代码Agent-界面升级方案.md)**（工作目录/权限模式/审批交互/代码可读性）

## 一、背景与目标

小助现为**只读、陪伴型**助手：

- `src/planner/tools.py:387` 明确「本工具集不存在任何写文件工具」；
- `src/planner/prompts/system.md:59` 写死「只读；永远不能改」；
- 全仓库无 `workspace` / `cwd` 概念（grep 无命中）。

本期把它在 **PC 端** 扩展为**本地代码助手**：在**审批制**下，对**工作区**内的文件进行读写与代码检索。

### 已定决策（不再讨论）

| 决策 | 取值 |
|---|---|
| 目标端 | **仅 PC 端**（安卓端完全不动） |
| 权限模型 | **审批制**（写/删/执行前需用户确认） |
| 第一期范围 | **本地代码助手：读写 + 检索**（不含 shell / git） |
| 工具实现 | **planner 自研工具集**（走 `tools.py`，不引入 MCP） |
| 读取范围 | **读也限工作区**（工作区外的读需显式批准） |
| 无人响应 | **默认拒绝**（窗口隐藏 / 超时 → deny） |
| 记忆 schema | **任务型**：`summary/task/decisions/lessons/context/user` + 时间范围（见「六」） |
| 压缩机制 | 两阶段（原文→节点、节点→上层节点）；沿用现有替换/存储/时间规则；保留的尾部**不进压缩输入**、普通对话照常保留（见「七」） |
| 压缩模型 | 与主对话**同一模型**（轻量模型仅作可选项） |

## 二、P0-a 模型升级（独立小改动，先合）

DeepSeek 官方已将 V4.1-Flash 的 API 模型名定为 **`deepseek-flash`**（正式名 DeepSeek-V4.1-Flash）；
旧的 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 已**退役但临时路由**到 V4.1-Flash。

| 改动 | 文件 | 说明 |
|---|---|---|
| 默认模型 → `deepseek-flash` | `src/planner/config.py:43`（`PLANNER_DEFAULT_MODEL`） | 显式化 + 面向未来 |
| 视觉识别补 `deepseek-flash` | `src/planner/llm.py:47`（`is_vision_model`） | ⚠️ **否则改默认模型后 `capture_screen` 会静默降级为 OCR**（现有判定只认 `vision/-vl/-omni/...`，`deepseek-flash` 不含） |
| 文档说明同步 | `AGENTS.md` / `README.md` | 默认模型描述 |

> 说明：`llm.py:59 is_deepseek_v4` 已识别 `deepseek-flash`（思考开关不受影响），本条无需改动。

## 三、P0-b 工作区与审批地基

### 3.1 配置（`src/planner/settings.py`）

`DEFAULT_SETTINGS` 新增：

- `workspace_root: ""` —— 代码工作区绝对路径；**空 = 代码能力关闭**（不注册代码工具、prompt 不注入代码段）
- `approval_timeout_seconds: 120` —— 审批等待上限（走 `_LIMITS`，60~600）
- `code_read_outside_workspace: false` —— 是否允许发起「工作区外读」的审批

固定常量（不可配，防误改）：可写/可读**黑名单**

```
**/.env*   **/*.key   **/*.pem   **/id_rsa*   .git/config
（以及系统目录，如 C:\Windows 等——实现时集中定义一处常量）
```

### 3.2 权限规则持久化

新增运行时文件 `data/permissions.json`（随 `PLANNER_DATA_ROOT` 隔离；git 忽略）：

```json
[{"tool": "write_file", "path_glob": "src/**", "decision": "allow"}]
```

- 用户在审批弹窗勾选 **always** 时落一条规则；
- **workspace 变更时清空**（路径语义已变，旧规则不应误放行）。

### 3.3 审批中间件（`src/planner/middleware.py` 新增 `ApprovalMiddleware`）

注册进 `src/planner/agent.py:50` 的中间件链。`wrap_tool_call` 流程：

1. 工具有限集合内（`WRITE_TOOLS` / `READ_TOOLS`）才处理，否则直接放行。
2. 解析目标路径 → **黑名单命中 = 硬拒**（返回提示串，不弹审批）。
3. 判定：
   - 路径在 workspace 内 且 **只读** → 放行；
   - **写操作**、或**工作区外读** → 查 `permissions.json`，命中 `allow` → 放行。
4. 否则**挂起**：生成请求 id，`session` 记 pending，push 事件 `approval_request`，
   `threading.Event.wait(timeout)` 等待 `/approval` 回填结果。
5. 结果处理：
   - `allow_once` → 放行；
   - `always` → 落规则后放行；
   - `deny` / 超时 / 窗口隐藏 → 返回 `"（用户拒绝了此操作，已取消。）"`。

**可中断性**：等待期间 `StopRequestMiddleware` / session close 必须能立即唤醒
（复用 `session.pause_before_continue` 的 `Event` 模式）。

### 3.4 会话与端点

- `src/planner/session.py` 新增：
  - `workspace_root` 状态、`pending_approvals: dict[str, threading.Event]`
  - `resolve_approval(id, decision)`、`request_workspace(path)`
  - **workspace 变更后重建 agent**（复用 `toggle_mock` / `set_chat_model` 的重建路径，
    因为 `build_tools` 需按 workspace 条件注册工具）
- `src/planner/server.py` 新端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workspace` | 当前工作区、是否启用、规则概要 |
| POST | `/workspace` | `{path}`：校验并设置 / 清空 |
| POST | `/approval` | `{id, decision}`：`allow_once` / `always` / `deny` |

- 新事件：`approval_request`、`approval_resolved`、`workspace_changed`。

## 四、P1 读写 + 检索工具（`src/planner/tools.py::build_tools`）

**仅当 `workspace_root` 非空时注册**（与安卓「不承诺不可用工具」一致；PC prompt 相应只在启用时注入代码段）。

| 工具 | 签名 | 审批 |
|---|---|---|
| `write_file` | `(path, content)` 新建 / 覆盖 | 需 |
| `edit_file` | `(path, old_string, new_string, replace_all=False)` 精确替换；老串非唯一且非 `replace_all` → 报错 | 需 |
| `create_dir` | `(path)` | 需 |
| `move_file` | `(src, dst)` | 需 |
| `delete_file` | `(path)` | 需 |
| `glob` | `(pattern, path="")` → 匹配文件列表 | 免（区内） |
| `grep` | `(pattern, include="", path="", max_results=50)` → 命中行 | 免（区内） |

约束：

- 路径解析后**必须在 workspace 内**（否则走审批流程）；
- 结果截断（`glob` / `grep` 上限 + 「共 N 条，已截断」提示）；
- `read_file` / `list_dir` 改为**工作区外需审批**；
- `edit_file` 对 CRLF/LF 按**原文精确匹配**，失败返回上下文片段帮助定位；
- `apply_patch`（unified diff）本期**不做**。

## 五、P2 提示词（与 P1 同步）

- `src/planner/prompts/system.md` 增「代码工作区（仅启用时注入）」段：
  先读后改、最小改动、`edit_file` 精确替换、批量改动说明、**不擅自 commit / 不改工作区外文件**、
  危险操作先说明、遵守仓库 `AGENTS.md`。
- **AGENTS.md 自动发现**：进入 workspace 时读根目录 `AGENTS.md` 注入（长度上限，如 8KB；缓存至 workspace 变更）。
- 工作区上下文：cwd + 顶层文件树（git 分支可选，轻量）。
- 记忆与压缩规则段（见 6.5）：随「P1.5 记忆与压缩改造」一起做。
- ⚠️ 安卓侧**不搬**这些 PC 专有段（按 `android/app/src/main/assets/prompts/README.md` 的同步规程）。

## 六、记忆 schema（代码模式：任务记忆）

> 现状 schema 是**情感/情节型**：`ProfileInfo`（`middleware.py:294` 的 preferences/personality/habits/goals）
> 加两条指令通篇提炼「用户是个怎样的人」。代码 Agent 需要的是**任务/语义型**。调研（详见 6.5 依据）
> 结论一致：压缩应保留**目标与进度、下一步、决策及理由（WHY）、错误与修法、文件/符号引用、约束**，
> 并丢弃冗余工具输出。

### 6.1 节点字段（替换 `ProfileInfo` 四维度）

| 字段 | 内容 | 层级合并语义 |
|---|---|---|
| `summary` | 这段时间**发生了什么**（叙述；每层都记，粒度逐层递减） | 分层摘要 |
| `task` | `{goal, status, todo[], next_steps[], blockers[]}` | **覆盖**：最新为准；未决 todo/blocker 保留，已完成坍缩 |
| `decisions` | `[{what, why}]` | **累积**去重；**WHY 永不随层级丢失** |
| `lessons` | `{errors:[{symptom, cause, fix}], patterns[]}` | **抽象**：具体错误 → 可复用规则/模式 |
| `context` | `{env[], commands[], artifacts[], constraints[]}` | 累积；冲突取新；**constraints 钉住（不随层级丢弃）** |
| `user` | 用户性格/偏好/习惯（**收成一个字段**） | 累积合并归纳 |
| 时间范围 | 起止 | 沿用现有机制（见 6.3）；父 = 子并集 |

- 原 `ProfileInfo` 去向：`preferences/personality/habits` → 并入 `user`；`goals`（目标动机）→ 并入 `task`。
- **不做**：`future_notes`；**不保留用户原话逐字**（用户说了什么 → 进 `summary` / `user`，一并压缩）。

### 6.2 指令与标记

- **叶子指令**（原文 → 节点）：抽取 + 去噪。直接写明「这次压缩的是**原文**（原始对话），**不压缩节点**，输出 JSON」；
  工具结果一律不复述原文，但关键值（命令、报错、路径）可逐字。
- **父层指令**（节点 → 上层节点）：合并 + 抽象。按 6.1 的合并语义逐字段写规则；正向表述 + **现场点名**目标节点。
- **节点标记**（「不需要压缩」）：
  - 节点在上下文里**常驻**标记：`[node0_001 · L0 · 第0-19条 · 时间] [已压缩 · 不需要压缩]`；
  - 节点压缩时：**目标节点去掉标记**（模型据此知道「这几条要压」）+ **指令点名**（双保险）；其他节点保留标记 = 别动；
  - 去标记只发生在**拼这次压缩输入**时；子节点消息随后从 buffer 替换出去、父节点带新标记进来。

### 6.3 完全沿用现有机制（不重写）

| 机制 | 位置 |
|---|---|
| 全局序号 `_compressed_total`（节点范围 = 第 a-b 条） | `middleware.py:570` |
| 不切开 `ai(tool_calls)` 与配对 `tool`（防 400） | `_extend_batch_for_tool_pairs:484` |
| buffer 替换：`RemoveMessage` 删旧 + 节点消息入 buffer（`node_start` 定位） | `middleware.py:620` / `:695` |
| 持久化：`buffer_state.recent_buffer`（重启恢复） | `sqlite_memory_tree.py:416` |
| DB：`nodes` 表；叶子存原文快照 `details`；父节点写 `parent_id`、子节点 `is_active=0` | `add_leaf:265` / `compact:313` |
| 节点 ID 自带层级 `node{level}_{seq}`；层级计数从 ID 恢复 | `_next_id:164` |
| 时间范围：叶子取批次首/末 `metadata.ts`；父节点取子节点 `time_start` min / `time_end` max；渲染用 `get_effective_time_range` 兜底历史节点 | `middleware.py:582` / `sqlite_memory_tree.py:345` / `middleware.py:689` |
| 异步压缩线程 + 空闲时原子应用 | `session.py:787` |
| `explore_memory_tree` 下钻 | `tools.py:364` |
| 压缩参数（`compress_token_budget` / `compress_keep` / `compact_threshold` / `compact_factor`）走 settings | `settings.py` |

### 6.4 预算与迁移

- **整节点 ~1000 token**（`summary` ~400–600）；新增设置项 `node_token_budget`；
- 实现：`max_tokens` 设**略高于**预算（卡满会截断 JSON → 解析失败），再校验 / 按字段截断；
- 存储零迁移：内容字段存 JSON（`profile`/`details`/`meta`），加删字段改 pydantic + 提示词即可；
- 旧节点兼容：`_node_full_text` 保留旧 persona 标签的兼容渲染分支。

### 6.5 系统提示词里写清压缩规则

压缩调用默认带**同一份主 system prompt**（`_call_compress:718`），所以在 `prompts/system.md` 里说明规则即「提前通知」：

- 上下文变长时，较早的消息会被压缩成 `[nodeX]` 节点（= 自己的过往对话）；
- 节点「不需要压缩」标记的含义；被压缩时目标节点会去掉标记且在指令里点名；
- 会被保留的内容（状态 / 决策及理由 / 经验 / 环境约定 / 用户偏好）→ **关键决策、理由要在对话里说清楚**；
- 回忆细节用 `explore_memory_tree` 下钻。

> 调研依据（2026-10）：OpenAI Codex `codex-rs/prompts/templates/compact/prompt.md`（progress / decisions /
> constraints / next steps）；Claude Code 压缩九段模板（错误与修法、待办、当前工作、下一步）；
> Gemini CLI 的 state-manager 压缩（整体目标 / 文件系统状态 / current_plan）；Anthropic《Effective context
> engineering for AI agents》（保留架构决策、未解决 bug，丢弃冗余工具输出）。

## 七、上下文与压缩策略

### 7.1 触发与热上下文

- **稀有 compaction**：叶子压缩按 **token 预算**触发（`compress_token_budget`，唯一条件；`compress_keep` 控制保留条数）；
- **追加为主、不重排**：压缩后节点**插回对应区间原位置**，不再让 `_reorder_node_messages`（`session.py:1389`）
  把节点搬到最前——现状每次压缩都重写前缀 → 整段缓存失效；
- **工具结果卸载**（类比 Claude Code 的 microcompact）：超阈值的大输出落盘（随 `DATA_ROOT` 隔离），
  上下文只留「路径 + 大小 + 头尾/摘要」指针，需要时用工具重读。

### 7.2 压缩输入边界（「未来信息」不发送）

- 保留的尾部原始消息**照常留在普通对话上下文**（不是删掉），但**不放进压缩输入**；
- **不加**「未来信息」标记，`future_notes` 不做（代价：失去「依据未来消息修正摘要」的能力，接受）；
- 输入形态：

| 压缩 | 输入 | 指令 |
|---|---|---|
| 叶子（原文 → 节点） | `[system]` + `[带标记的节点]` + `[中间原始消息]` + `[指令]` | 「这次压的是**原文**，**不压节点**，输出 JSON」 |
| 父层（节点 → 上层节点） | `[system]` + `[节点：目标去标记、其他保留]` + `[指令]` | 「压缩这**几个**节点（点名），其他不要动，输出 JSON」 |

- **不再**把待压缩正文嵌进指令重复一遍（现状 `_LEAF_INSTRUCTION` 内嵌 `original_text`，`middleware.py:338`；
  父层同理重复 `child_summaries`）。

### 7.3 模型与缓存

- **压缩与主对话同一模型**（轻量模型仅作可选项，先不用）；
- 缓存按模型分；同模型下：
  - 叶子压缩输入前缀 `[system] + [节点] + [中间原文]` 与主上下文逐字一致 → **大量命中**；
  - 节点压缩因改的是靠前节点的标记，前缀从那里分叉 → 命中少些（此时上下文小、触发不频繁，可接受）；
- 真正要保的是**主对话每轮前缀稳定**（见 7.1）。

## 八、P3 前端（`frontend/renderer/`）

- **审批弹窗**：收到 `approval_request` → 卡片 / 模态（工具名、路径、**diff 预览**）→ 三按钮回 `POST /approval`。
  - `edit_file`：用 Python `difflib` 生成统一 diff 放进事件；
  - `write_file`：显示新内容摘要 / 大小。
- 工作区选择器（设置页）+ 当前工作区 / 变更文件展示。
- 工具结果卡片沿用 `renderer/app.js:417 addToolCard` / `setToolResult` 扩展（diff 卡、终端样式卡）。
- `frontend/main.js`：透传 `/workspace` / `/approval`（沿用现有 HTTP 代理模式）。

## 九、测试（`tests/`，mock LLM + `tmp_path` 隔离）

- **纯函数**：路径校验（越界 / 黑名单）、审批决策、diff 生成。
- **工具**：write / edit / create / move / delete / glob / grep（tmp workspace；含越界拒绝、edit 非唯一报错）。
- **中间件**：审批挂起 → allow / deny / 超时默认拒绝；`always` 落规则后二次调用免审批。
- **记忆/压缩**：压缩输出 schema 校验（字段齐全 + 预算超长截断）；「只压目标」回归（带标记节点的内容不得被并入新节点）；叶子压缩输入不含保留的尾部；节点压缩时目标去标记 + 指令点名；时间范围不回归（叶子取首末 `ts`、父节点 = 子节点并集）。
- **端点**：`/workspace`、`/approval` 集成。
- **回归**：默认（无 workspace）时**不注册**代码工具、prompt 不含代码段。

## 十、文件改动清单

| 文件 | 改动 |
|---|---|
| `src/planner/config.py` | 默认模型 → `deepseek-flash` |
| `src/planner/llm.py` | `is_vision_model` 补 `deepseek-flash` |
| `src/planner/settings.py` | 新增 workspace / 审批配置、`node_token_budget` |
| `src/planner/tools.py` | 新增读写 / 检索工具 + 路径校验；大工具结果卸载（落盘 + 指针） |
| `src/planner/middleware.py` | 新增 `ApprovalMiddleware`；记忆 schema + 叶子/父压缩指令重写 + 节点标记（渲染/去标记）+ 压缩输入拼装 |
| `src/planner/agent.py` | 注册审批中间件 |
| `src/planner/session.py` | 审批状态、workspace、重建 agent；压缩后节点**插回原位**（去 `_reorder_node_messages` 搬前）、压缩预算读取 |
| `src/planner/server.py` | `/workspace`、`/approval` 端点与事件 |
| `src/planner/prompts/system.md` | 代码工作区段 + AGENTS.md 发现；记忆与压缩规则段 |
| `src/planner/memory/sqlite_memory_tree.py` | **不改**（存储零迁移；时间范围沿用现有） |
| `frontend/renderer/{app.js,settings.js,settings.html,style.css}` | 审批弹窗、diff、工作区选择 |
| `frontend/main.js` | 透传新端点 |
| `tests/*` | 上述测试 |
| `AGENTS.md` / `README.md` | 模型说明等 |
| `data/permissions.json`（运行时） | 权限规则（git 忽略） |

## 十一、分期路线

- **P0** 工作区 + 审批地基（先无写工具跑通审批链路）+ 模型升级
- **P1** 读写 + 检索工具
- **P1.5** 记忆与压缩改造（任务型 schema、节点标记、叶子/父指令、预算、追加为主、工具结果卸载；见「六」「七」）
- **P2** 提示词（代码段 + AGENTS.md + 工作区上下文）
- **P3** 前端（diff / 审批弹窗 / 工作区选择）
- **后续（不在本期）**：shell / `bash`、git / PR、多会话、子代理、MCP；**安卓端完全不动**。

## 十二、风险与开放问题

1. **审批阻塞 ↔ 流式 / 中断**：等待需可被 stop / close 立即打断，否则关窗会挂住 worker 线程。
2. **`edit_file` 精确匹配**：中文 / 缩进 / 换行敏感，失败要给出可定位的上下文。
3. **黑名单 vs 工作区外读**：黑名单是硬拒、工作区外读是「需审批」，两者优先级需在 3.3 写死。
4. **工具注册条件化**：workspace 变更需重建 agent（避免模型调用到未注册工具 / 承诺失效）。
5. **`permissions.json` 失效**：workspace 变更即清空，避免旧规则在新根目录误放行。
6. **新旧 schema 共存**：旧节点是 persona 字段、新节点是任务字段 → `_node_full_text` 保留兼容渲染；若混乱明显，再考虑按模式分角色 / 分库。
7. **标记可能被模型忽视**：去标记 + 指令点名双保险；加「只压目标、别压其他」的回归测试兜底。
8. **节点压缩缓存命中下降**（改动靠前节点标记所致）：预期内、可接受；勿为省缓存退回「重复正文」方案。
9. **预算截断 JSON**：`max_tokens` 略高于预算 + 校验 / 按字段截断，避免整段 JSON 解析失败。

## 十三、实现记录

### 2026-10-17 第一轮实现（273 测试全绿）

**P0-a 模型升级**

- 默认模型 → `deepseek-flash`（`config.py`）；`is_vision_model` 收录 `deepseek-flash`
  （防截屏静默降级 OCR）；`build_chat_model` 支持 `max_tokens`（压缩模型用）。
- 测试/文档同步：`test_vision.py`、`AGENTS.md`、`docs/tour.md`、`docs/面试深挖提纲.md`、`README.md`。

**P0-b 工作区与审批地基**

- `settings.py` 新增：`workspace_root` / `approval_timeout_seconds` / `code_read_outside_workspace`
  / `node_token_budget` / `compress_token_budget`（布尔字段统一归一，含 `tts_enabled`）。
- 新模块 `src/planner/workspace.py`：`resolve_target` / `is_within` / `is_blocked`
  （`.env*`、`id_rsa*`、`*.key/*.pem`、任意层级 `.git`、系统目录）、`tool_targets`、
  `PermissionStore`（`data/permissions.json`，workspace 变更清空）。
- `middleware.ApprovalMiddleware`（`wrap_tool_call`，注册在 HeartbeatTrack 之后）：
  黑名单硬拒 → 区内只读放行 → 写操作/区外读查规则 → 挂起审批（`session.request_approval`，
  0.5s 分片等待、stop/close 立即唤醒、超时默认拒绝）；拒绝时返回对齐 `tool_call_id` 的 ToolMessage。
- `session`：`workspace_root`、`pending_approvals`、`request_approval` / `resolve_approval`；
  workspace 变更 → 清规则 + 重建 agent + 重建系统提示词；事件 `approval_request` / `approval_resolved` / `workspace_changed`。
- `server`：`GET/POST /workspace`、`POST /approval`。
- 提示词：`prompts/workspace.md`（启用时追加，占位 `{workspace}`）+ 工作区根 `AGENTS.md`（8KB 上限）
  + 工作区顶层内容（最多 40 项）。

**P1 读写 + 检索工具**（`tools.build_code_tools`，仅 workspace 非空注册）

- `write_file` / `edit_file`（CRLF/LF 语义匹配并保留；非唯一报错；`replace_all`）/
  `create_dir` / `move_file` / `delete_file` / `glob`（≤200 条）/ `grep`（≤200 命中、跳过 >1MB/二进制）。
- `read_file` / `list_dir` docstring 更新（区外读需审批）；`test_security.py` 策略更新为
  「无命令执行；写能力只在 build_code_tools / _offload_if_large 两处入口」。

**P1.5 记忆与压缩改造**

- schema v2（任务型）：`summary / task / decisions / lessons / context / user`，
  落库 `meta.fields`（`schema_version=2`，存储零迁移）；`profile` 列仅旧节点兼容渲染。
- 叶子/父指令重写：正向表述 + 目标点名 + 逐字段合并语义（覆盖/累积去重/抽象/钉住）；
  英文指令同步；`future_notes` 不做。
- 节点保护标记 `[已压缩 · 不需要压缩]`：常驻对话上下文；节点压缩时**目标从树渲染（去标记）**、
  其他节点保留标记；节点文本带层级（`· L{level} ·`）。
- 压缩输入边界（计划 7.2）：叶子 = 带标记节点 + 待压原文（保留尾部不进）；
  父层 = 节点（目标去标记）+ 指令；删除 `original_text` / `child_summaries` 的指令内重复。
- 触发：**仅 token 预算**（`should_compress_raw`，`compress_token_budget` 默认 100000；2026-10-17 起移除条数触发与
  `compress_trigger` 设置——节点向上合并仍按 阈值 8 / 4 合 1）；连 keep 条都超预算时只保留最后 1 条（防卡死）；
  节点预算 `node_token_budget`（默认 1000）→ 压缩模型 `max_tokens = 1.5×` + `_apply_budget` 软裁剪。
- 追加为主：`_reorder_node_messages` → `_place_node_messages`（最小改动、已就位不动）。
- 工具结果卸载：`_offload_if_large` → `data/tool_results/`；审批中间件对该目录**读免审批**。
- 系统提示词：补「压缩机制」段、更新节点字段说明、去掉悬空 `search_memory` 引用。
- 测试：新增 `test_compression_contract.py`（8 用例）、重写 `test_profile_nodes.py`。

**P3 前端**

- 审批弹窗（`index.html` / `app.js` / `style.css`）：`approval_request` → 弹窗（工具名 + 路径 +
  **预览**：`edit_file` 统一 diff / `write_file` 内容摘要 + 允许一次 / 记住并允许 / 拒绝）→ `POST /approval`；
  `approval_resolved` 关闭。
- 设置页新增「代码工作区」tab：目录文本框 + 选择（主进程 `dialog.showOpenDialog`）+
  清除；保存走 `POST /workspace`。压缩面板补 `compress_token_budget` / `node_token_budget`。
- `main.js` 新增 `pick-directory` IPC；`preload.js` 暴露 `pickDirectory`。

### 与计划的偏差 / 说明

1. **工具结果卸载**：v1 只对 `explore_memory_tree` 生效（`read_file`/`glob`/`grep` 本就有输出上限）；
   落盘目录 `data/tool_results/` 在审批里视为助手自身数据（免审批读）。
2. **节点标记**：由渲染器按需添加（`_render_node_text(marker=...)`），树里不存标记——
   等价于"压缩时去掉标记"，且无需字符串手术。
3. **面板隐藏时收到审批**：事件按现有分发落在气泡（气泡暂不显示审批）→ 超时默认拒绝；
   如需气泡/托盘提醒另做。
4. **`future_notes`**：按计划放弃；旧库字段仅保留读取兼容（`_node_full_text`）。
5. **未做**：安卓端（计划：完全不动）；shell / git 工具（本期不做）。

### 2026-10-17 第五轮实现（变更留底/回退 + 授权规则管理；314 测试全绿）

> 前四轮明细见 [`代码Agent-界面升级方案.md`](代码Agent-界面升级方案.md)「实现记录」。本轮为
> P0 收尾：让"审批制"闭环——用户能**看见**给小助的记忆规则（并撤回），也能对它的文件改动**任意回退**。

- **`src/planner/changes.py`（新）**：`ChangesDb`（`data/changes.db`，WAL）+
  `snapshot_before` / `rollback_to`（回退到某条之前：新→旧逐条恢复；`before_missing` = 删除；
  换工作区/黑名单/`no_snapshot` 跳过并报原因，成功标 `reverted`）。
- **`tools.py`**：`_record_change` 接入 write/edit/move/delete（`no_snapshot` 判定 + 相对路径）。
- **`session.py`**：`get_changes_db()`（懒建、单例）；**`server.py`**：`GET /changes`、
  `POST /changes/rollback`、`POST /changes/clear`、`POST /permissions/delete`、`POST /permissions/clear`，
  回退后推 `changes_update` 事件；`POST /approval` 补传 `scope`（修复"记住并允许"范围在真实
  UI 未生效）。
- **`workspace.py`**：`PermissionStore.save()`（规则删除/清空）。
- **前端**：`index.html`/`style.css`/`app.js` 新增「变更」tab（列表 + 回退 + 刷新/清空 +
  `changes_update` 事件 + 失败可见提示）；`settings.html`/`settings.js`「代码工作区」页
  新增授权规则列表（逐条删除 / 清空全部，立即生效）。
- **测试**：+4 变更留底/回退用例（含 move/delete、no_snapshot、换工作区拒绝）；+2 服务端端点
  （changes/permissions、`/approval` scope 经 HTTP）；隔离 CDP 实测变更 tab 回退 + 规则删除全通过。

