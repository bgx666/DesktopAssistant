# 小助代码 Agent 界面与工作目录升级方案（第二轮）

> 主题：在第一轮（工作区 + 审批 + 任务型记忆，见 [`代码Agent-实施计划.md`](代码Agent-实施计划.md)）之上，
> 补齐「工作目录 / 权限模式 / 审批交互 / 代码可读性」——即 Codex / DeepSeek Harness / OpenCode
> 已验证的代码 Agent 交互骨架。
> 状态：**已实现（2026-10-17，pytest 288 全绿）**，回填见「八、实现记录」。
> 相关：[`README.md`](README.md)、[`tour.md`](tour.md)、`../AGENTS.md`

## 一、背景与决策

第一轮落地后剩三个缺口：**看不见**（不知道模型现在在哪个目录干活）、**切不了**（换目录要开设置页）、
**不够细**（工作区内写操作一律审批，没有"这个目录才是它的地盘"的概念）。

调研结论（Codex CLI / DeepSeek Harness / OpenCode 官方文档 + 源码/issue）：

| 主题 | 它们怎么做 | 我们照做/改造 |
|---|---|---|
| 权限可见性 | Codex `/permissions` 预设（Ask / Approve for me / Full Access）且**状态栏常驻显示**；OpenCode `--auto` 在提示符上有标记 | 标题栏放**权限模式切换按钮**（Ask / Auto / Read-only） |
| 工作目录 | Codex 状态栏 `current-dir` + `--cd`；DSH Workspaces；OpenCode 按项目目录起会话 | **工作区之下加 cwd**：显示 + 快速切换 + 权限联动 + 路径基准 |
| 审批粒度 | OpenCode `ask → once / always（模式化，如 `git status*`）/ reject` | 「记住并允许」给**范围选择**（文件/目录/工作区） |
| 审批可达性 | DSH `ui-approval` 接管输入框；OpenCode 失焦时系统通知 | **面板隐藏时自动展开 + 气泡提示**（补现有缺口） |
| 代码可读性 | Codex 代码块+diff 语法高亮 | 轻量高亮 + 代码态工具卡 + diff 渲染 |
| 变更可视化 | OpenCode 文件变更跟踪/undo；Codex `/diff` `/review` | 留到第三轮（见「七、后续可选」） |

### 已定决策（不再讨论）

| 决策 | 取值 |
|---|---|
| 会话列表 / fork / archive | **不做**（连续陪伴模型，记忆树即历史） |
| 当前工作目录 cwd | **做**。工作区（安全边界）之下的第二层；默认 = 工作区根；必须是工作区的子目录 |
| 权限联动 | **做切换按钮**（模仿 Codex `/permissions`）：`ask`（默认）/ `auto` / `read_only`；`auto` = **cwd 内写自动放行**，cwd 外（仍在区内）写仍需审批；黑名单恒硬拒 |
| 最近目录 | **持久化**（settings，≤8，去重）；点击：在当前工作区内 → 切 cwd；区外 → 整个切为工作区 |
| 压缩提取工作目录 | **做**：节点 `context.cwd` 记录"这段事发生在哪个目录"；叶子由代码填、父层从子节点合并 |
| 面板宽度 | **宽模式可切换**（350×520 ↔ 680×760，标题栏 ⤢） |
| 界面取舍 | 复用现有标题栏/Tab 结构，不引入侧边栏/多窗口（悬浮球形态是产品特色，不做 IDE 化） |

## 二、工作目录（cwd）

### 2.1 数据模型（`settings.py`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `workspace_cwd` | str | `""` | 当前工作目录（绝对路径）；空 = 工作区根；必须 `is_within(workspace_root)` |
| `workspace_recent` | list[str] | `[]` | 最近目录（绝对路径，≤8，最近在前，去重） |
| `permission_mode` | enum | `"ask"` | `ask` / `auto` / `read_only` |

- `save_settings` 需支持**列表字段**（新增 `_LISTS` 处理：只留字符串、去重、截断 8 个）；
  `permission_mode` 走 `_ENUMS`。
- `workspace_root` 变更时：`workspace_cwd` 归零（=根）、权限规则清空（现状）、**recent 保留**。

### 2.2 权限判定矩阵（`ApprovalMiddleware._authorize`）

先过**黑名单**（`.env*` / 私钥 / 任意层级 `.git` / 系统目录 → 硬拒，任何模式任何位置），再按下表：

| 操作 | 位置 | ask（默认） | auto | read_only |
|---|---|---|---|---|
| 读（read/list/glob/grep） | 工作区内 | 放行 | 放行 | 放行 |
| 读 | 工作区外 | 由 `code_read_outside_workspace` 决定（现状） | 同左 | 同左 |
| 写（write/edit/create/move/delete） | **cwd 内** | 审批 | **放行** | 拒绝 |
| 写 | 工作区内、cwd 外 | 审批 | 审批 | 拒绝 |
| 写 | 工作区外 | 审批 | 审批 | 拒绝 |

- 命中 `permissions.json` 的 allow 规则 → 一律放行（任何模式）。
- 超时/无人响应 → 默认拒绝（现状）。
- read_only 拒绝消息："（当前为只读模式，已取消。可在标题栏切换权限模式。）"

### 2.3 路径基准与提示词

- **相对路径解析基准**：`resolve_target(base, raw)` 的 base 从"工作区根"改为 **cwd**
  （`workspace.py` 的 `resolve_target` / `tool_targets` 签名加 `base`；工具与中间件统一传 `cwd or workspace`）。
- `glob` / `grep` 默认根：cwd（显式传 path 仍可搜全工作区）。
- 工具结果里的路径显示：相对 cwd（区外回退工作区相对/绝对）。
- `prompts/workspace.md` 占位符：`{workspace}` + **`{cwd}`**；说明"相对路径按当前目录"、
  "当前权限模式：{permission_mode}"（自 `_build_system_prompt` 注入）。
- 顶层文件树：从"工作区根"改为"**cwd**"。`AGENTS.md`：读工作区根 + cwd（两者不同则都注入，cwd 的在后）。
- 变更副作用：`workspace_cwd` / `permission_mode` 变化 → 重建系统提示词（树/模式段落）；agent 一并重建（沿用现有路径）。

### 2.4 端点与切换

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workspace` | 返回 `{workspace, cwd, recent, permission_mode, rules}`（扩展） |
| POST | `/workspace` | `{path}` 设/清工作区（现有；cwd 归零、规则清空） |
| POST | `/workspace/cwd` | `{path}` 设 cwd：空串 = 根；否则必须 `is_within(workspace)` 且存在；**不清规则** |
| POST | `/settings` | `{updates:{permission_mode}}`（复用；或前端走 `/permissions` 包装） |

- `workspace_recent` 更新规则：设工作区 / 切 cwd 成功时 `unshift(去重, ≤8)`。
- 新事件：`workspace_changed` 负载扩展为 `{workspace, cwd, permission_mode}`（前端刷新徽标）。

### 2.5 压缩提取工作目录（记忆记录"在哪个目录发生的"）

- schema：`ContextInfo` 新增 `cwd: str`（描述："执行这段对话时所在的工作目录"）。
- **叶子**：`_summarize_leaf` 由**代码**填 `out.context.cwd = session 当前 cwd`（不依赖模型，防幻觉）；
  压缩指令头部提示"（当前工作目录：{cwd}）"，让 summary 叙事里可以自然提到目录。
- **父层**：`_compact_level` 从子节点 `meta.fields.context.cwd` 合并——去重后 1 个则保留，
  多个则 `"A；B；C…"`（最多 3 个 + 省略号）。
- 渲染：`_render_fields_lines` 的 `[环境]` 行加 `目录：X`；`_apply_budget` 对 cwd 截断（≤300 字符）。
- 旧节点无 `cwd` → 渲染时省略（兼容）。

## 三、界面升级

### 3.1 标题栏（沿用现有结构 + 三个新元素）

```
┌────────────────────────────────────────────────────────┐
│ ● 小助   [📁 planner ▾]  [🛡 Ask ▾]     思考中… ⛁12k ♥15m ⚙ — │
└────────────────────────────────────────────────────────┘
```

- **📁 目录徽标**：显示 cwd 的目录名（悬停显示全路径）；无工作区时显示"未启用代码"（点击进设置）。
  点击 → 工作目录面板（3.2）。
- **权限模式按钮**：`🛡 Ask` / `⚡ Auto` / `👁 只读` 三选一（点击弹小菜单，Codex `/permissions` 风格）；
  Auto 时按钮高亮（对齐 opencode 的 muted auto 标记）；切换即 POST `/settings`。
- **待审批徽标**：有待批请求时持续闪烁（点击直接弹审批框）。
- 其余保留：思考中 / 上下文 token / 心跳倒计时 / 设置 / 收起。

### 3.2 工作目录面板（点 📁 徽标，面板内浮层）

- **面包屑**：`工作区根 > src > xiaob`（点任意一级跳过去；只允许工作区内）。
- **子目录列表**：当前 cwd 的直接子目录（点击进入；懒加载下一级）。
- **最近目录**（≤8）：区内 → 切 cwd；区外 → 提示"切换工作区到该目录？"。
- **按钮**：`回到工作区根` / `更换工作区…`（打开设置页的工作区 tab）。
- 目录不存在（被删/移动）→ 点击时提示并自动从 recent 移除。

### 3.3 审批弹窗升级（现有 + 补强）

- **diff 着色**：`+` 绿底 / `−` 红底 / `@@` 灰；行号；全宽；占面板高度 ~50% 可滚。
- **「记住并允许」范围选择**：仅此文件 / **此目录**（建议 `cwd/**` 相对 glob）/ 整个工作区（`**`）——
  三选一后再落 `permissions.json`（对齐 opencode 的模式化 always）。
- **超时倒计时**：显示剩余秒数（默认 120s，后端 `approval_timeout_seconds`）。
- **面板隐藏时**（P0 必修）：`main.js` 收到 `approval_request` → **自动 `morphIn()` 展开面板** +
  气泡闪提示；展开后前端自动弹出审批框（事件已在 pendingEvents 里）。

### 3.4 代码可读性

- `md.js`：代码块**轻量语法高亮**——本地 vendored 极简 tokenizer（关键字/字符串/注释/数字，覆盖
  py/js/ts/json/sh/yaml 常见即可，不引全量 highlight.js）；保持"先 escape 再着色"的 XSS 安全路径。
- **代码态工具卡**（`read_file / grep / glob / edit_file / write_file` 专用样式）：
  - 头部：工具图标 + **文件/目录路径**（相对 cwd）+ 行号范围（grep/read）；
  - 正文：等宽 + **不折行**（`white-space: pre` + 横向滚动）+ 行号栏；
  - `write_file` 参数折叠为"共 N 字符 + 前 5 行"摘要；`edit_file` 参数渲染成 diff。
- 结果超长：卡片内截断 + "展开全文"（对齐 Codex 的折叠风格）。

### 3.5 面板宽模式

- 标题栏 **⤢** 切换：350×520 ↔ **680×760**（屏幕不够时按可用区域自适应，最大 90%）。
- 尺寸三处参数化（现状写死）：`main.js:22-23`（PANEL_W/H）、`morph.js:11-12`（STAGE_W/H）、
  `style.css`（`#morph-stage`/`#panel-ui` 固定尺寸）；morph 动画本身已按 rect 驱动，改尺寸只需同步这三个常量。
- 宽模式下：消息气泡/工具卡最大宽度提升、日历格子增大；选择持久化（localStorage 即可，无需后端）。

### 3.6 上下文分解（点标题栏 token 数）

- 弹层列出：`system` / 记忆节点 / 原始消息 / 图片（估算）各自 token 与占比条
  （`context_stats().breakdown`；底部一行总 token 与消息条数）。
- 后端：`context_stats()` 扩展分解（纯统计，无新端点——并入 `/state`）。

### 3.7 `@` 文件引用补全

- 输入框键入 `@` → 弹出文件补全（新只读端点 `GET /workspace/files?q=`，服务端用 glob 以 cwd 为根，≤50 条）；
- 选中插入 `@src/foo.py`（相对 cwd）；`workspace.md` 约定"`@路径` 表示用户指定的文件引用，先读它"。
- 对齐 Codex `@` fuzzy file search / opencode `@file` 引用。

## 四、分期路线

- **P0（bug 级 + cwd 骨架）**
  1. 审批永不丢：面板隐藏自动展开 + 气泡闪 + 标题栏待批徽标；
  2. cwd 数据模型 + 权限判定矩阵 + 路径基准 + 提示词注入 + `/workspace/cwd` 端点 + recent 记录；
  3. 标题栏目录徽标 + 权限模式切换按钮；
  4. 压缩提取 cwd（叶子填、父层合并、渲染）。
- **P1（可读性与交互）**
  5. 审批弹窗升级（diff 着色 + 范围选择 + 倒计时）；
  6. 代码高亮 + 代码态工具卡；
  7. 面板宽模式。
- **P2（进阶）**
  8. 工作目录面板（面包屑 + 子目录树 + 最近目录）；
  9. 上下文分解弹层；
  10. `@` 文件引用补全。
- **第三轮（另议）**：`ask_user` 结构化提问（复用审批挂起机制）、变更视图 + 撤销（写前留底 `data/changes/`）、
  图片粘贴、主题。

## 五、文件改动清单

| 文件 | 改动 |
|---|---|
| `src/planner/settings.py` | 新增 `workspace_cwd` / `workspace_recent` / `permission_mode`；`_LISTS` 列表字段支持 |
| `src/planner/workspace.py` | `resolve_target`/`tool_targets` 加 base 参数；`normalize_cwd`（必须区内）；recent 维护纯函数 |
| `src/planner/middleware.py` | 权限矩阵（`permission_mode` + cwd 判定）；范围选择落规则；压缩提取 cwd（schema/填充/合并/渲染） |
| `src/planner/session.py` | `effective_cwd()`；workspace/cwd/权限模式变更 → 重建提示词+agent；`/state` 上下文分解 |
| `src/planner/server.py` | `/workspace` 扩展 + `POST /workspace/cwd` + `GET /workspace/files` |
| `src/planner/tools.py` | 工具路径基准改 cwd；代码态工具结果格式（路径头/行号）；`@` 引用的 `read_file` 相对路径 |
| `src/planner/prompts/workspace.md` | `{cwd}` / `{permission_mode}` 占位与规则说明 |
| `frontend/main.js` | `approval_request` 自动展开 + 气泡闪；`morphIn/Out` 尺寸参数化 |
| `frontend/renderer/{index.html,app.js,style.css}` | 标题栏徽标/模式按钮/待批徽标；工作目录面板；审批弹窗升级；代码态卡片；宽模式；上下文分解；`@` 补全 |
| `frontend/renderer/md.js` | 代码块轻量高亮 |
| `frontend/renderer/morph.js` | STAGE 尺寸参数化 |
| `tests/*` | 见下 |

## 六、测试计划（mock LLM + `tmp_path`，不碰真实 data/）

- **settings**：三个新字段的持久化/校验；列表字段去重与上限；非法 `permission_mode` 400。
- **workspace**：cwd 必须区内（区外 400）；`resolve_target` 基准=cwd；recent 维护（去重/≤8/删目录处理）。
- **权限矩阵**：表驱动测试（3 模式 × 读/写 × cwd 内/外）——重点：auto 在 cwd 内写放行、cwd 外仍审批；
  read_only 全拒；黑名单任何模式硬拒。
- **端点**：`POST /workspace/cwd`（含非法路径）；`GET /workspace` 新字段；`GET /workspace/files`。
- **压缩**：叶子 `context.cwd` = 当时 cwd；父层合并（相同/不同）；渲染含 `目录：`；预算截断。
- **前端回归**：扩展 `tests/cdp_panel_check.js`——标题栏按钮存在、权限模式点击循环、面板宽模式切换无异常。
- **默认回归**：未设工作区时行为与第一轮完全一致（不注册写工具、无 cwd 注入）。

## 七、后续可选（不在本轮）

- 图片粘贴（Ctrl+V 进输入框）、代码块主题（深浅）。

## 八、实现记录

### 2026-10-17 第二轮实现（pytest 288 全绿；release e2e 含面板检查）

**工作目录（cwd）+ 权限模式**

- `settings.py`：新增 `workspace_cwd` / `workspace_recent`（列表字段，去重≤8）/ `permission_mode`
  （枚举 ask/auto/read_only）；`_LISTS` 通用列表字段处理。
- `workspace.py`：`resolve_target`/`tool_targets` 基准参数化；`effective_cwd`（越界/失效回退根）；
  `latest_recent`（置顶去重≤8）。
- `middleware.ApprovalMiddleware._authorize`：权限矩阵落地（只读禁写；auto 时 **cwd 内写免审批**、
  外圈仍审批；黑名单任何模式硬拒）；「记住并允许」支持 scope=file/dir/workspace →
  `_scope_glob` 生成 `文件 / src/** / **` 规则；`_approval_scopes` 生成弹窗候选。
- `session.py`：`effective_cwd()`；`update_settings` 处理 cwd（**仅工作区实际变化才归零**）、
  recent 记录、权限模式变更重建提示词+agent；`request_approval` 事件带 `scopes/cwd/timeout`；
  `resolve_approval(id, decision, scope)`（always → always_file/dir/workspace）。
- `server.py`：`POST /workspace/cwd`（区内校验）、`GET /workspace` 扩展（cwd/recent/mode）、
  `GET /workspace/dirs`（子目录，工作目录面板用）、`GET /workspace/files?q=`（@ 补全）。
- 提示词：`workspace.md` 注入 `{cwd}` / `{permission_mode}` 与相对路径规则、`@路径` 引用约定；
  顶层文件树改为 **cwd** 的内容；`AGENTS.md` 读 工作区根 + cwd（不同则都读）。

**压缩提取工作目录**

- `ContextInfo.cwd` 字段；叶子由**代码**填当前 cwd（指令里提示但明确"系统会填，你不用填"）；
  父层由子节点 `context.cwd` 去重合并（≤3，多则省略）；`[环境]` 行渲染 `目录：…`；预算截断 300 字。

**界面（P0/P1/P2 一次做完）**

- 标题栏：`📁 目录徽标`（悬停全路径，点击开工作目录面板）/ `🛡 权限模式`（三选一菜单，
  Auto 高亮，对齐 Codex `/permissions`）/ 待批 `⚠` 徽标 / `⤢ 宽模式`。
- 审批：**面板隐藏时收到审批 → 自动展开**（main.js pollDequeue）；弹窗 diff **着色**（+/−/@@）、
  范围单选、**超时倒计时**。
- 工作目录面板：面包屑（点击上跳）+ 子目录列表（点击进入，懒加载）+ 最近目录
  （区内 → 切 cwd；区外 → 确认后切工作区）+ 「设为当前目录 / 回到根 / 更换工作区…」。
- 代码可读性：`md.js` 轻量语法高亮（py/js/ts/json/sh/yaml，token 化后再转义，XSS 安全）；
  代码态工具卡（路径头 + `read_file/grep/glob` 结果行号化 + 不折行横滚；write/edit 参数折叠摘要）。
- 面板尺寸：**默认大窗（680×760）**；`--stage-w/h` CSS 变量由渲染层跟随 `window.innerWidth/Height`
  实时同步（resize 事件），`morph.js` 球态 scale 按实际宽度动态计算。**支持拖拽边缘调整大小**：
  透明窗口不支持原生边框 resize，改为渲染层 8 个把手（`#resize-handles`）报边缘 +
  主进程按 `screen.getCursorScreenPoint()` 跟光标调 `setBounds`（16ms 轮询、2s 无移动兜底收尾、
  最小 320、超屏收缩），结束落 `ui-settings.json`（`panel_w/panel_h`）；另有
  `panel-size-set` 程序化接口（e2e 用）。
- 面板窗口行为（用户设定）：**不再 always-on-top**（可被其他窗口盖住）；**启动即展开**
  （默认大窗打开，仍可点「—」/Esc 变形回悬浮球）；托盘图标与重复启动 = **置前不折叠**
  （`focusPanel()`）；审批请求到达时：隐藏 → 自动展开，已展开但可能被压住 → 置前。
- 上下文分解：点 token 数弹层（system/节点/原文/图片 占比条；`context_stats` 增加 breakdown）。
- `@` 文件引用补全：输入框 `@` 触发（`GET /workspace/files`），↑↓/Enter/Tab/Escape 键盘操作，
  插入 `@相对路径`；workspace.md 约定语义。

**测试**

- 新增/更新（`test_code_workspace.py` +8、`test_compression_contract.py` +3、`test_server.py` +1）：
  cwd 基准与回退、recent 去重上限与持久化、**权限矩阵表驱动**（3 模式 × cwd 内外）、
  scope 规则落盘、审批 scope 事件、提示词注入 cwd/模式、压缩 cwd 填写与父层合并。
- `tests/cdp_panel_check.js` 扩展：目录徽标存在 + **宽模式真的把窗口变宽**（回归保护）。

### 2026-10-17 第三轮追加：`run_command`（审批制命令）+ 工具卡折叠组

- **`run_command(command, timeout=120)`**（`tools.build_code_tools`，仅工作区启用时注册）：
  在**当前工作目录**执行，返回退出码 + stdout/stderr（合并；超 12000 字符走 `_offload_if_large` 落盘）；
  UTF-8/GBK 双解码、`CREATE_NO_WINDOW`；超时/停止/关窗→ `_kill_process_tree`（`taskkill /F /T`）杀进程树。
- **权限**：命令走独立分支 `_authorize_command` —— 命令硬黑名单（递归删、格式化、磁盘、关机/重启、
  改注册表、结束进程、下载即执行、动态执行、混淆命令、强推远端、账号操作）**不弹审批直接拒**；
  `read_only` 模式拒；命中命令规则放行；否则**逐条审批**（Ask 与 Auto 都一样，命令能逃出工作区边界）。
- **「记住并允许」命令范围**：`仅此命令`（精确）/ `以「首词」开头`（存 `pytest *` 形式，
  无参数的原命令也命中）；`PermissionStore.add_command/match_command`，仍存 `data/permissions.json`。
- **前端**：`run_command` 归入代码态卡片（`$ 命令` 路径头 + 带行号输出）；权限菜单文案更新
  （Auto 标注"命令仍需逐条确认"；只读标注"禁止写入与命令"）。
- **工具卡折叠组**：相邻工具调用自动归入 `.tool-group`，**默认折叠**（组头 `N 个操作 · 名字…`，
  执行中显示"正在执行 X…"），点组头展开；普通消息（用户/回复/日志/记忆）出现即封组；
  单个操作不显示组头（保持旧观感）。新增 `updateToolGroup()`/`closeToolGroup()`；
  `loadHistory` 重建时同一套逻辑。
- **测试**：`test_code_workspace.py` +6（黑名单、逐条审批+真实执行、只读/黑名单拒、前缀规则、
  Auto 仍审批、超时杀进程）；`tests/cdp_panel_check.js` 增加工具组折叠/展开检查（tolerant：单工具直显）。

### 2026-10-17 第四轮追加：输入框下方状态条 + 模型服务商管理（DeepSeek 官方 / OpenCode Go）

- **状态条**（`#composer-bar`，输入框正下方）：`📁 目录` / `🛡 权限模式` / `🤖 模型` / `💭 思考强度`
  四个 chip（模仿代码 Agent 的底部状态行）；标题栏原来的目录/模式徽标移除（上下文 token、心跳保留），
  菜单从状态条上方弹出（`bottom: 104px`）。
- **模型服务商**：`llm_provider`（`deepseek` / `opencode`）+ 分服务商
  `llm_api_key_<p>` / `llm_model_<p>` / `llm_base_url_<p>`；解析在 `llm.resolve_llm_config`
  （旧字段 `llm_api_key` / `llm_model` 读取时**自动迁移**为 DeepSeek 分字段，仅 DeepSeek 兜底）。
  OpenCode Go 预设端点 `https://opencode.ai/inference/go/openai/v1`（models：
  `https://opencode.ai/inference/go/v1/models`），只收录 **chat/completions 兼容**模型
  （gpt/claude 走 responses/messages 端点，不进菜单）；调用必须带 `x-opencode-session` +
  自定义 User-Agent（`llm.provider_headers`，会话 ID 落 `llm_session_id`；缺失会被端点拒
  `400 MissingSessionID`——修复前实测踩到）。
- **端点**：`GET /llm/providers`（服务商预设 + 当前生效配置）、`GET /llm/models?provider=`
  （有 key 时拉端点 `/models` 实时清单，失败回退预设；无 key 不发起网络）。
- **设置页**：LLM 页签改为「服务商 / API Key / Model（datalist 建议）/ Base URL（留空用预设）」，
  切换服务商互不覆盖（未保存编辑暂存在草稿，切回来还在）。
- **测试**：新增 `test_llm_provider.py`（预设/解析/迁移/重建）+ `/llm/providers` 端点测试；
  `tests/cdp_panel_check.js` 增加状态条 chip 检查；隔离环境实测：模型菜单 2 分组 12 项、
  切 OpenCode Go / `deepseek-v4-flash` 后端生效、强度切 low、权限切 auto 全部通过。

### 2026-10-17 第五轮追加：变更留底/回退（变更 tab）+ 授权规则管理

- **变更留底**（新模块 `changes.py`，库 `data/changes.db`）：`write_file` / `edit_file` /
  `move_file` / `delete_file` 执行前由 `tools._record_change` 记一条
  `{tool, path, rel, before, after, before_missing, after_missing, no_snapshot}`；
  单侧快照上限 `MAX_SNAPSHOT_CHARS`（2M 字符），超限/二进制 → `no_snapshot=1`（列表标注、不可回退）；
  历史清空时**不动文件**。
- **回退语义**：`rollback_to(db, id, workspace)` = **回退到这条之前**——把 `id ≥ 该值`且未回退的
  条目按**新→旧**逐条恢复（`before_missing` → 删除当时不存在的文件）；路径必须仍在**当前工作区**内
  （被换掉/越界 → 跳过并报原因），黑名单拒绝；成功的条目标 `reverted`（幂等）。
- **端点**：`GET /changes?limit=`、`POST /changes/rollback {id}`、`POST /changes/clear`
  （回退后推事件 `changes_update`）。
- **前端「变更」tab**：标题栏第三个 tab（聊天/日历/变更）——列表（时间 / 工具 / 相对路径 /
  内容大小 / 新建/删除/未留底/已回退标记）+ 每行「回退到此条之前」（确认后执行，
  结果气泡报告成功条数与失败原因）+ 刷新/清空；切到该 tab 自动刷新，`changes_update` 事件
  在激活态时即时刷新；加载失败在列表内显示重试提示（不静默）。
- **授权规则管理**（设置页「代码工作区」新增节）：列出 `data/permissions.json` 规则
  （工具 + 路径 `/ 命令` 匹配），逐条删除、清空全部——删除**立即生效**（不随「保存设置」）；
  新端点 `POST /permissions/delete {index}` / `POST /permissions/clear`，响应带最新规则列表。
- **顺带修复**：`POST /approval` 之前**漏传 `scope`**（前端选「记住并允许·目录」在真实 UI 里
  退化成整工作区放行的近似行为）——现在 `resolve_approval(id, decision, scope)` 全链路生效，
  并有 HTTP 层测试覆盖。
- **测试**：`test_code_workspace.py` +4（写/编辑留底与两步回退、move/delete 留底回退、
  no_snapshot 跳过、换工作区拒绝）；`test_server.py` +2（changes/permissions 端点、
  `/approval` scope 经 HTTP 归一为 `always_dir`）；隔离环境 CDP 实测：变更 tab 渲染→点回退→
  后端标记 + 气泡、设置页规则删除生效（`check_changes_tab.js`，连续两轮全绿）。

### 2026-10-17 第六轮追加：`ask_user` 结构化提问（弹窗选项 / 自由输入）

- **工具**（`tools.build_tools`，常驻注册）：`ask_user(question, options[])` —— 决定权在用户
  手里时弹窗提问（选项 1~4 个 + 自由输入）；docstring 写明"什么时候用/不用"；
  options 清理（空白丢弃、≤4、每个 ≤80 字）；空问题直接返回错误串（不挂起）。
- **挂起**（`session.ask_user` / `resolve_question`）：复用审批挂起模式——`pending_questions`
  + `threading.Event` 0.5s 分片等待（停止/关窗立即唤醒）；**超时语义 = 跳过**（返回提示让模型
  自行判断/别反复追问），不是拒绝；工具结果文本：`用户选择了「X」` / `用户回答：…` /
  超时与停止各有兜底句。事件 `question_request`（question/options/timeout）与
  `question_resolved`（answer/selected/status）。
- **端点**：`POST /question {id, answer?, selected?}`（两者至少一个；否则 400）。
- **前端**：独立弹窗（问题文本 + 选项按钮 + 输入行 + 倒计时，`#question`；
  样式复用审批卡片视觉）；`main.js` 面板隐藏时对 `question_request` 自动展开（与审批同分支）。
- **mock 触发**：消息含「考我」→ mock 模型发 `ask_user` 调用（结果文本到了才收尾；
  只看**最后一条**工具结果，历史旧答案不会截胡后续提问）。
- **测试**：新增 `test_ask_user.py` 6 用例（挂起/回填选项、自由文本与混合、超时、停止唤醒、
  工具清理与校验、HTTP 端点含 400）；`tests/cdp_panel_check.js` 增加 ask_user 全链路检查
  （`e2e_release.ps1` 传后端端口，release mock 实测：弹窗→点选项→工具结果全通）。

### 2026-10-17 第七轮追加：思考过程流式显示（reasoning_content）

- **问题**：开着思考（`thinking_mode` 默认 adaptive）时，UI 只有「思考中…」转圈；
  思考内容被丢了两遍——① `langchain_openai 1.4.1` 的消息转换只挑白名单字段，
  流式 delta 的 `reasoning_content` **静默丢弃**（本地假端点实测：additional_kwargs 为空）；
  ② 就算拿得到，`session._run_agent` 也只转发 `chunk.content`。
- **透传 shim**（`llm.py::_install_reasoning_passthrough`，导入时装、幂等）：
  包装 `_convert_delta_to_message_chunk` / `_convert_dict_to_message`，把
  `reasoning_content` 塞进 `additional_kwargs`（其余路径不变）。
  安全性：`_convert_message_to_dict` 白名单式转换，additional_kwargs 不会发回端点
  （DeepSeek 要求 reasoning 不回传）；langchain 升级改名时假端点回归直接红。
- **后端**：`session._run_agent` 逐块推 `thinking_stream {content, msg_id}`——
  每轮模型调用一个新 `msg_id`（超步结束重置，多轮工具调用对应多个思考块）；
  **只展示不落 buffer**：写回 buffer 前剥掉 `reasoning_content`（不进历史/持久化）。
- **前端**：可折叠「思考过程」块（灰字斜体、限高滚动、实时逐字）——回答文本开始、
  轮次收束（`text` 事件）或生成结束（state.thinking=false）自动折叠；点标题展开/收起；
  `main.js` 把 `thinking_stream` 和 `text_stream` 一样排除出重放队列（避免面板重开重复渲染）。
- **mock 触发**：消息含「多想」→ 回复消息带上 mock 思考流（dev/e2e 用）。
- **测试**：新增 `test_thinking_stream.py` 4 用例（假端点 shim 透传、reasoning 不回传、
  mock 触发 thinking_stream 且 buffer 不落 reasoning、无触发词不产生事件）；
  `tests/cdp_panel_check.js` 增加思考流检查（块出现→流式文本→自动折叠→点击展开），
  release e2e 实测 `思考流: THINK_OK`。

### 2026-10-17 第八轮追加：缓存命中/TPS 统计 + 思考行窗口式折叠 + 工具调用单行化

- **缓存/用量统计**：`stream_usage=True` 让流式最终块带 usage；第二处 shim
  （`_create_usage_metadata` 包装）把 DeepSeek 顶层 `prompt_cache_hit_tokens` 映射进
  `input_token_details.cache_read`（OpenAI 风格 `prompt_tokens_details.cached_tokens` 本就能过）。
  `session._run_agent` 按轮累计（跨 ReAct 多次调用求和；耗时=整轮生成，TPS=输出/耗时）→
  `llm_stats.py`（`data/llm_stats.json`，环形 50 条，`cache_known` 区分"未报"与"0%"）→
  `stats_update` 事件 + `GET /llm/stats`。前端状态条新增 `📊 命中 x% · y t/s` chip，
  点开明细面板（汇总四项 + 每轮明细行）；mock 固定 usage（1200/900）供 dev/e2e 验证。
- **思考行窗口式折叠（用户要求）**：默认折叠，但那一行本身是"窗口"——正文不收起，
  新内容不断进入、旧内容向左移出（ticker 尾部对齐滚动 + 左缘渐隐 mask）；点行展开全文。
- **工具调用单行化（用户要求）**：废弃卡片式（含折叠组），改为与思考行同款单行：
  `🔧 工具 · 目标 · 状态（执行中转圈/✓）· 结果首行`；点行展开参数与完整结果
  （代码结果仍走行号/横滚渲染）；历史重建同一套。`test/cdp_panel_check.js` 相应改为
  "工具行存在 + 展开/收起 + 无卡片回归"检查。
- **测试**：新增 `test_llm_stats.py` 4 用例（DeepSeek 缓存字段映射、环形存储与聚合、
  `cache_known` 语义、session 记录 + `/llm/stats` 端点）；release e2e 面板检查新增
  `思考流/用量统计/工具行` 三项断言。

### 2026-10-17 第九轮追加：思考过程持久化（历史/重启可见）

- **问题（用户反馈）**：重开面板后，之前回答的思考过程全没了——第八轮把
  `reasoning_content` 写回 buffer 前剥掉，`/history` 也没有 thinking 条目，
  思考只活在实时流里。
- **改法**：不再剥除，思考**随消息持久化**（`additional_kwargs.reasoning_content`
  经 buffer_state 往返保留；chunk 聚合字符串自动拼接，多轮各附着在自己的 AI 消息上）；
  `/history` 对带思考的 AI 消息发独立 `thinking` 条目（位于其正文/工具调用之前）。
  回传安全不变：`_convert_message_to_dict` 是白名单转换，additional_kwargs **不会**
  发回端点（`test_reasoning_never_sent_back_to_endpoint` 守护）。
- **前端**：历史重建复用同一组件（`createThinkingBlock` + `appendThinking`），
  折叠单行窗口样式一致；bubble.js 只筛 role=assistant，thinking 条目无影响。
- **测试**：`test_thinking_stream.py` +2（重启往返思考仍在、`/history` thinking 条目
  顺序在 assistant 之前）+1 真实流式链路用例（假端点验证 chunk 聚合后的 AI 消息带完整
  reasoning、usage/缓存命中走统计——用户实机的 OpenCode Go 流式路径）；原有
  「buffer 不落 reasoning」断言反转为「必须落」；`cdp_panel_check.js` 在思考流检查后
  **重载页面**并断言历史里的思考行仍在（`THINK_OK+HIST`，release e2e 实测通过）。
- **说明**：改版前已存的旧对话没有思考（当时没存），此后每轮新回答都会带。

### 2026-10-17 第十轮追加：工具结果「修改前后」diff

- **动机（用户反馈）**：调用 edit/write 改文件时，工具行展开只有参数摘要 + 一句结果文本，
  看不到改了哪——要么像审批弹窗那样给 diff，要么像其他工具的结果一样直观。
- **数据源**：复用变更留底（`data/changes.db` 的 before/after 快照——比审批预览更全，
  覆盖"覆盖写已有文件"与"删除"）。
- **链路**：`tools._record_change` 把 change id 入队 `session._pending_changes` →
  `session._emit_tool_events` 在对应 tool_result 上按**工具名顺序消费**队列
  （move 的两条都被消费，不会漏给下一条工具）→ 事件带 `diff`（`changes.diff_text`
  unified diff，>200k 字符跳过、`no_snapshot` 给替代说明、move 给「a → b 内容未变」说明句）
  → 同时写进消息 `additional_kwargs.change_ids`，`/history` 重建同样带 diff。
- **前端**：工具行折叠预览改为 `+N −M` 统计；展开体在「参数」与「结果」之间插入
  「修改（前 → 后）」深色 diff 块（行号着色复用审批预览的 `.diff-line` 体系）。
- **mock**：消息带「改文件」→ 写 `e2e_note.txt`（需已启用工作区；后续轮由
  "最后一条是写文件结果"收尾，不循环）。放在「考我」分支之前（e2e 历史里的触发词不截胡）。
- **测试**：`test_code_workspace.py` +4（写/编辑/删除 diff 内容、move 说明句 + 队列消费、
  心跳结果不带 diff（不漏配）、`/history` 带 diff）；`cdp_panel_check.js` 新增工作区
  auto + mock 写文件的全链路断言（`工具diff: DIFF_OK`，release e2e 实测通过）。

### 2026-10-17 第十一轮追加：对话内图片展示（show_image）

- **动机（用户反馈）**：让它用 matplotlib 画图、或从网上"找一张图"，图片要能直接出现在
  对话里（不只是给个路径）——Electron 聊天窗口的天然优势。
- **工具**（`tools.build_code_tools`，工作区启用时注册）：`show_image(source, caption)`：
  - **本地**：工作区内图片文件（matplotlib 用 `plt.savefig`，提示词明确不要 `plt.show()`）；
  - **网络直链**：http(s) → httpx 下载到 `data/images/<sha1>.<ext>` 缓存（content-type
    白名单、≤15MB、跟随重定向）——外链失效/防盗链也不影响已展示历史；
  - 校验：工作区外/非图片/超大/空参都返回可读错误，不挂起。
- **链路**：`session._pending_images` 队列 → `_emit_tool_events` 推 `image` 事件
  （`url=/file?path=…`），并把路径/caption 写进消息 `additional_kwargs` → `/history`
  出 `role=image` 条目（重开面板/重启后图片仍在）。
- **端点**：`GET /file?path=`——只服务工作区 / `data/tool_results` / `data/images`
  三个根内的图片（403 兜底、防穿越）。前端经主进程 `api-image` IPC 下载成本地文件再
  `<img>`（同音频思路绕 file:// 的 CORS/PNA；缓存 100 张滚动清理）。
- **前端**：图片气泡（限宽 320px、点击放大/还原、caption 下方小字、加载中/失败态）。
- **其他**：`run_command` 环境注入 `MPLBACKEND=Agg`（防 `plt.show()` 卡死）；
  mock 触发词「画图」（工作区需已有 plot.png）。
- **测试**：新增 `test_show_image.py` 6 用例（本地成功/各类拒绝、网络下载缓存与非图拒绝、
  `/file` 服务与 403、`/history` 图片条目）；安全测试放行 show_image 的缓存写入
  （仅 `dest.write_bytes` 一行，带说明）；`cdp_panel_check.js` 新增真 png 全链路
  （图片加载 `naturalWidth>0` + 重载历史仍在，`图片: IMG_OK+HIST`）。

### 2026-10-17 第十二轮追加：自身看图（view_image，模型读图进上下文）

- **动机**：show_image 只解决"给用户看"；模型还需要**自己看**——自查刚画的图表、看用户
  指定的本地图片。`read_file` 对图片会命中二进制拒绝，之前唯一"看图"路径是截屏。
- **工具**（`tools.build_tools` 常驻注册）：`view_image(source, note)`——**只支持本地图片**
  （用户明确不要网络图）：工作区内自由；区外走 `read_file` 同款策略（`READ_TOOLS` +
  `tool_targets` 收录，代码读取开关控制、黑名单硬拒；无工作区时绝对路径沿用只读工具行为）。
- **注入**：`_pending_screenshots` 队列泛化为 `{url, text, viewed?}` 条目（截屏/读图共用），
  `ScreenShotInjectMiddleware` 下一轮注入 user 消息 image_url 块（tool 消息不能带图，
  DeepSeek 400）；**去重查 buffer**：注入消息 `additional_kwargs.viewed_image` 记录
  normcase 路径，重复读→提示"已在上下文"；被压缩掉后允许重读；重启随 buffer_state 恢复。
- **回退**：模型不支持视觉 → OCR 文字识别（`ocr.ocr_image_file` 新增文件入口；无文字/无依赖
  各有独立提示文案）。
- **提示词**：`workspace.md`/`system.md` 明确 "show_image 给用户看 / view_image 给自己看"；
  mock 触发词「看图」（分支顺序：看图→画图→改文件→考我→多想，防历史截胡）。
- **测试**：新增 `test_view_image.py` 8 用例（注入/去重与压缩后重读/绝对路径与相对路径提示/
  拒绝分支/黑名单/区外审批/`tool_targets`/OCR 两态）；旧测试适配 dict 队列格式；
  `cdp_panel_check.js` 新增看图检查（工具行完成 ✓、**不产生图片气泡**、`看图: VIEW_OK`）。

### 2026-10-17 第十三轮追加：单轮调用上限可配（10 → 默认 100）

- **动机**：用户实机撞上 `Model call limits exceeded: run limit (10/10)`——10 次是
  "闲聊助手"时期的硬保险，改干活模式后（多文件读写 + 跑测试）太少。
- **改法**：新设置 `model_call_limit`（默认 **100**，范围 10~500），
  `ModelCallLimitMiddleware(run_limit=…)` 构建 agent 时读取；`update_settings`
  检测到变化自动重建 agent。设置页「交互 → 工作循环」新增输入框。
- **顺带**：超限时中间件注入的英文 AI 消息（exit_behavior="end"）在写回 buffer 前
  由 `session._neutralize_limit_message` 替换成中文提示（"说『继续』可接着干 /
  去设置调大"）——之前这条英文会作为小助发言留在对话与历史里。
- **测试**：`test_settings.py` +3（默认值、10~500 校验、agent 构建读数与改设置重建、
  英文消息替换）。

### 2026-10-17 第十四轮追加：悬浮球交互调整（单击=放大）

- **改法（用户要求）**：单击悬浮球 = **放大**（展开面板，`togglePanel()`）；原来的
  "单击看最近回复气泡"移到**右键菜单新增项「显示最近的气泡」**（主进程
  `bubbleWebContents.send('show-reply')` → bubble.js `showLastReply`，翻页/过期重置
  逻辑不变）；长按（语音输入）、拖拽、拖文件挂载、右键菜单其余项都保持原样。
- **回归**：新增 `tests/cdp_bubble_check.js`（真·受信输入单击实测展开 + 右键菜单入口
  存在），并挂进 `e2e_release.ps1`。调试期发现测试要等收起动画稳定后再点
  （迟到的 morph-out-done 会覆盖新的 morph-in）——已在检查脚本里加了稳定等待。

### 2026-10-17 第十五轮追加：面板窗口进 Windows 任务栏

- **问题（用户反馈）**：Windows 任务栏里看不到小助——球/面板/气泡三个窗口全都
  `skipTaskbar: true`（早期"托盘型小部件"设计）。
- **改法**：只有**面板窗口**开任务栏标签：`skipTaskbar: false` + `icon: bubble.ico`
  + `app.setAppUserModelId('com.xiaozhu.planner')`（不设会被 Windows 归到 "Electron"）。
  悬浮球/气泡保持不进任务栏；面板收起（`morph-out-done → hide()`）后标签自动消失；
  鼠标移到标签上能预览、点击可切回。最小化仍是"收回成悬浮球"的既有语义。
- **验证**：隔离环境实测——面板展开时主进程 `MainWindowTitle=小助`（任务栏可见），
  hide 后仅剩球窗口（skipTaskbar）；release e2e 全绿。

### 2026-10-17 第十六轮追加：工具调用合并成一行组（回归并升级）

- **动机（用户反馈）**：单行化后每个工具各占一行仍占空间；相邻工具调用要**合并**
  成一行——收起时单行显示"调用了哪些工具"，展开后再逐行显示每个工具。
- **实现**（前端 `app.js`）：`addToolLine` 改为挂进当前 `.tool-group`（`closeToolGroup()`
  由普通消息/思考行/图片封组）；组头单行「🔧 N 个操作 · 名字去重列表（>4 截断）」，
  执行中显示「正在执行 X…（已完成 n/N）」；点组头展开/收起；**组内每行仍是可单独展开的
  工具行**（参数/结果/diff 逻辑复用）；仅 1 个工具时不显示组头（`.single`，观感同单行）。
  `loadHistory` 重建走同一套（历史里相邻 tool_call 自然成组）。
- **测试**：`cdp_panel_check.js` 第 12 步改为组优先（默认折叠 → 标题含"N 个操作" →
  展开后逐行且单行可独立展开 → 再点收起；无多工具组时退化为单行检查），实测 `GROUP_OK`。
- **第十六轮补充（用户反馈）**：组头工具名列表**取消"只显示 4 个"的固定截断**——名字
  **按调用顺序全量列出**（不去重、不加"×n"计数）；头部拆成
  `<span class="tg-count">N 个操作</span>`（常驻，running 时显示"正在执行 X…（a/b）"）+
  `<span class="tg-title">` 做成与思考行同款的**滑动窗口**（`overflow:hidden` + 尾部对齐
  `scrollLeft=scrollWidth` + 左缘渐隐 mask）——窗口放不下时只露最近的名字；
  回归检查加断言：名字文本不得出现截断省略号。

### 2026-10-17 第十七轮追加：对话内 HTML 预览（```html → 沙箱可交互卡片）

- **动机（用户要求）**：对话区要能显示 HTML，且里面的互动元素可操作。
- **渲染**：回复里的围栏 `html` 代码块 → `enhanceHtmlPreviews`（app.js）生成预览卡：
  iframe + 「查看源码（折叠）/放大」按钮；`loadHistory`（重开面板）走同一渲染路径。
- **安全设计（两道隔离，缺一不可）**：
  1. iframe `sandbox="allow-scripts"`——**绝不给 allow-same-origin**：不透明源，
     脚本能跑但拿不到父页面 DOM / `window.planner`（无任何应用权限）；
  2. 预览文档走后端 http（`POST /preview` 寄存 → `GET /preview/{id}`），由
     `_wrap_preview_html` 注入收紧版 CSP：`connect-src 'none'`（**断掉 preview 调本地
     后端 API 的路**）、form-action/frame-src/object-src/base-uri 全禁、仅放行内联
     script/style 与图片/媒体。
- **关键坑（实测）**：**不能用 srcdoc**——srcdoc 文档继承面板 CSP（`script-src 'self'`），
  内联脚本被静默拦掉（CDP 实测 onclick=null）；http 文档不继承 embeder CSP，才能自己定策略。
  另：沙箱 iframe 是 OOPIF（独立 CDP target），e2e 用 `Target.attachToTarget` 进它的上下文
  点按钮验证交互；面板 CSP 需放行 `frame-src http://127.0.0.1:*`。
- **测试**：`test_server.py` +2（/preview 寄存与 CSP 注入/404/400、无 head 包装）；
  `cdp_panel_check.js` 第 16 步（卡片出现、src 指向 /preview、sandbox 无 same-origin、
  iframe 内 CSP meta 存在、点按钮计数 → `HTML预览: HTML_OK`）；mock 触发词「做个网页」。

### 2026-10-17 第十八轮（热修）：OpenCode Go 思考参数 400

- **现象（用户实机）**：切到 opencode / `deepseek-v4.1-flash` 后每次请求 400：
  `thinking must be an object with type enabled or disabled`。
- **根因（真实端点实测）**：我们默认发 `thinking: {"type": "adaptive"}`（DeepSeek 官方
  实测接受），但 **OpenCode Go 网关只认 `enabled/disabled`**——同为这个 key：
  adaptive → 400，enabled + reasoning_effort → 200。
- **修复**：`llm.thinking_extra_body` 增加 `provider` 参数：`provider=opencode` 时
  **adaptive → enabled**（disabled/显式 enabled 不变；官方端点行为不变）；
  `build_chat_model` 透传 provider。两个调用点（主模型/压缩模型）本就带 provider。
- **测试**：`test_thinking_settings.py` +2（映射表 + `build_chat_model` 出口断言
  `extra_body.thinking`）。

### 2026-10-17 第十九轮追加：工作目录面板交互修复 + 快捷操作

- **Bug（用户报告）**：点面板里的子目录，整个浮层消失——目录项点击后同步重渲染把
  按钮从 DOM 移除，事件冒泡到 document 时 `e.target.closest('.popover')` 落空 →
  被判为"点了外面"→ `closePopovers`。修复：document click 里对
  `!document.body.contains(e.target)`（处理过程中被移除）直接放行。
- **「设为当前目录」/「回到根」动态化**：按浏览位置/当前 cwd 禁用与命名
  （`设为当前目录（sub）`；浏览位置=cwd 时禁用；cwd 已在根时禁用回根）。
- **新增「📁 打开文件夹」**：主进程 `shell.openPath`（preload `openInExplorer` →
  `open-path` IPC）在资源管理器中打开当前浏览目录。
- **「更换工作区」直改**：不再跳设置页——直接弹目录选择器（`pickDirectory`）并
  `POST /workspace` 切换，随后原地刷新面板；未启用工作区时点 📁 同样直接弹选择器。
- **回归**：`cdp_panel_check.js` 第 17 步——打开面板 → 点 sub 目录（面板保持打开、
  面包屑更新、按钮启用）→ 设为当前目录（后端 cwd=…/sub）→ 回根（cwd=工作区根）→
  重开断言按钮禁用态（`工作目录面板: WSPANEL_OK`）。

### 2026-10-17 第二十轮追加：任务栏最小化语义修复 + 面板页加载自愈（两处"卡死/点不进"）

- **问题 1（用户报告）**：切换当前工作目录后"输入框点不进、其他按钮能点"。
  **根因**：与问题 2 的卡死态同源——悬浮球是独立置顶小窗口，在"球与面板并存"的异常状态里
  它会**吞掉压住区域的点击**；复现排查确认 cwd 切换流程本身无问题（`elementFromPoint` 命中
  输入框、真实打字可输入）。另一个叠加面：`#morph-stage` 默认 `pointer-events: none`、
  `#panel-ui` 默认透明，可交互态只由 morph 动画事件点亮——**页面一旦刷新/异常重载就整页
  透明点不动**（测试重载后抓到这个状态）。修复：`morph.js` 加载时查 `getPanelState()`，
  已是 `shown` 则直接进入可交互态（自愈）。
- **问题 2（用户报告）**：点任务栏图标最小化后，又冒出一个悬浮球；再点任务栏图标面板
  回来了——两个窗口并存卡住。**根因**：旧 `minimize` 处理器直接 `hide()`（窗口仍处于
  最小化态）→ 留下任务栏**幽灵按钮**，点它把面板恢复回来，而球已经被显示，形成双窗口；
  且 `panelState` 与实际显示不一致，后续交互全乱。
  **修复**：稳定展开态**不干预最小化**（系统标准语义：点一下缩小、再点还原——正是用户
  期望）；只有变形动画进行中被最小化才干预：in→落到已展开终态；out→`restore()` 后延迟
  60ms 再 hide（与 `forceFinishOut` 同法，避免幽灵按钮）+ 显示球。
- **回归**：`cdp_panel_check.js` 第 17 步新增"切换 cwd 后输入框可聚焦且 `elementFromPoint`
  命中输入框"断言，release e2e `工作目录面板: WSPANEL_OK` 全绿。

### 2026-10-17 第二十一轮：切目录后"输入框点不进"真凶——面板盖住了输入区

- **如实复现（真实鼠标逐点 + 逐键）**：三条路径对比——点过子目录后面板保持打开、以及
  「更换工作区」弹框取消后，**首次点输入框 `activeElement=BODY`、打字无效，第二次点击才进
  输入态**；「设为当前目录」（面板会关）没有此问题。
- **根因**：工作目录面板按 **chip** 定位（`bottom = chip 顶 - 6px`），chip 在新版输入框
  **内部底行** → 面板整个盖在**输入框正上方**。面板操作后会保持打开，此时点"输入框"其实
  点在面板上：第一下只把面板关掉、焦点落到 BODY（无光标），打字自然没反应；
  任务栏缩小再放大触发窗口 resize → `closePopovers` 顺手关掉面板 → 再点就正常
  （用户观察到的"缩小再放大就能输入"由此而来）。
- **修复**：① `positionPopover('#ws-panel', $('#input-bar'))`——面板悬在**整个输入框上方**，
  不再盖输入区（点输入框第一下即聚焦，同时面板自动收起）；② 「更换工作区」选完即收起面板。
- **回归**：`cdp_panel_check.js` 第 17 步新增"面板开着时 `elementFromPoint(输入框中心)`
  必须命中输入框"，release e2e `WSPANEL_OK`；复现脚本三条路径全部首次点击即可输入。

### 2026-10-17 第二十二轮：切工作区确认框→键盘焦点丢失（原生 confirm 后遗症，实锤修复）

- **用户复现路径（截图确认）**：「最近目录」里点一个**工作区外**的目录 → 弹出的
  **原生 confirm**（"把工作区切换到：D:\future\interview-materials ?"）→ 确定后
  **鼠标能点、输入框打不了字**，任务栏缩小再放大才恢复。
- **根因**：Windows 上 Electron 的 JS 原生 `confirm()` 关闭后**键盘焦点可能不还给页面**
  （OS 级焦点丢失；`document.hasFocus()` 依然为 true，所以 DOM 各断言都测不出来）。
- **修复**：
  1. **全面替换原生 confirm**：面板新增应用内确认框 `#ui-confirm`（`uiConfirm()`，
     支持 Esc/Enter/遮罩同系样式），替换 `switchDir`（本次触发点）/变更回退/清空变更历史；
     设置页同款（清空授权规则）；
  2. **焦点兜底**：主进程 `refocus-window` IPC（`win.focus()+webContents.focus()`），
     面板输入区 mousedown 时无条件调用——即使再遇到任何"能点不能打字"的状态，
     点一下输入区即恢复；`pick-directory` 弹框关闭后同样显式还焦点。
- **回归**：`cdp_panel_check.js` 第 18 步完整复刻用户路径（A/B 两目录造 recent → 点 B →
  断言出现的是**应用内弹窗**而非原生 → 确定 → 后端工作区切换 → 真·受信输入第一下点击
  可聚焦可打字）；原生 confirm 若回归会挂住页面 → 受信超时报 `NATIVE_CONFIRM_HANG`。
  实测 `切换工作区: SWITCH_OK`。

### 2026-10-17 第二十三轮：统计可见性修复 + 实报 token + 思考 token

- **用户反馈**：问"token 用量准确吗、缓存命中率能否显示"。**查实**：命中率一直在记录
  （其 release 最近记录 705,280/708,408 ≈ **99.6%**），但 📊 chip 因初始化忘记摘 `hidden`
  类**从未显示**（e2e 读 textContent 所以没抓到）——已修：chip 始终可见（无数据显示 "📊 —"），
  e2e 增加"chip 不得 hidden"断言。
- **真实端点多字段实测（OpenCode Go）**：usage 含 `prompt_tokens`/`completion_tokens`/
  **`prompt_tokens_details.cached_tokens`**（缓存命中，OpenAI 风格，langchain 原生映射）/
  `completion_tokens_details.reasoning_tokens`（思考 token）。
- **token 显示更准**：标题栏优先显示**上一轮 API 实报输入**（`context_stats().real_tokens`，
  含系统提示/记忆节点/图片，最准；未生成过回退字符估算并标"约"）；分解面板脚注标注口径；
  统计记录新增 `reason`（思考 token）→ 面板"平均思考"行 + 口径说明行（输入输出为整轮合计、
  TPS 口径、命中率口径）。
- **测试**：`test_llm_stats.py` 断言 `reason`/`avg_reason`/`real_tokens==1200`（mock）；
  cdp 统计步加 chip 可见断言；全量 pytest **356 passed**。

### 2026-10-17 第二十四轮：`get_cwd` 工具 + 切目录的缓存代价说明

- **新工具**（`build_code_tools`，工作区启用时注册）：`get_cwd()` 实时返回
  当前工作目录 / 工作区根 / 权限模式（切 cwd 会重建 agent，工具闭包即新值）；
  提示词提示"平时看 代码工作区 段即可，不确定时用它"。测试 +1（含未启用不注册、
  切 cwd 后即新值）。
- **缓存口径（用户提问）**：system prompt 内嵌 `{workspace}/{cwd}/{permission_mode}`
  与 cwd 文件树/AGENTS.md → **切 cwd/工作区/权限模式会重建 prompt+agent，前缀缓存
  从 0 失效：紧随其后的那一次请求几乎全量未命中（全价），之后从新前缀恢复命中**
  （实测命中率 99%+）；不是"之后永远不命中"。也即：频繁切目录有真实成本，别乱切。

### 2026-10-17 第二十五轮：滚动不打断阅读（贴底才跟随）

- **用户反馈**：它在思考/流式输出时想翻看旧消息，被**反复自动拽回底部**。
- **根因**：思考流/正文流/工具行/图片/日志等每处追加都无条件
  `messages.scrollTop = messages.scrollHeight`。
- **修复**：统一为 `scrollMessagesToBottom(force)`——**只有用户当前"贴底"（距底 <100px）
  才跟随**；用户翻上去后追加内容不动滚动条（右下「↓ 回到最新」按钮出现，点击平滑回底）。
  `force=true` 仅用于用户自己的动作（发消息气泡、首次/正常重载）。`loadHistory` 重建
  （压缩后台重载、面板重显）会记录并恢复**距底距离**——正在读历史时重建也不跳位。
- **回归**：`cdp_panel_check.js` 第 19 步：滚到顶部 → 触发一轮生成（思考流+正文+工具行
  全部追加）→ 断言滚动条仍在顶部（未被拽走）、内容确有追加、「回到最新」按钮出现且
  点击后回到底部。实测 `滚动: SCROLL_OK`。

### 2026-10-17 第二十六轮：`read_file` 升级为按行读取（行号 + 按需精读）

- **用户提问**：像 OpenCode / Codex 那样读长文件应该按需读片段，我们支持吗？
  —— 之前支持"分段"但**按字符**（`start/limit` 字符数、无行号、整文件先读进内存），
  与 grep 给的行号对不上，模型很难精确跳读。
- **升级**（codex/opencode 同款交互）：`read_file(path, offset=行号, limit=行数)`
  —— 1 基行号、默认 400 行、上限 1500 行；**输出带行号前缀**（`42| ...`）；头部给
  "共 N 行，本次显示第 a-b 行，继续用 offset=b+1"；单行 >2000 字符截断并标注；
  总输出 60K 字符预算兜底；末尾换行不多算一行；空文件/offset 越界有明确提示。
  提示词同步：先 `grep` 定位行号 → `read_file(offset=…)` 精读，不要整文件硬读。
- **测试**：`test_read_file.py` 重写/新增（按行分块与行号、offset 越界、超长行截断、
  空文件、limit 钳制），全量 pytest **358 passed**。

### 2026-10-17 第二十七轮：写文件能力对齐 codex/opencode（edit_file 宽松匹配兜底）

- **用户提问**：写文件是否和 codex/opencode 一样？对比结论——
  - `write_file`：自动建父目录 + 覆盖，与 opencode `write` 一致 ✓
  - `edit_file`：逐字精确替换、**不唯一报错并给出处数**、`replace_all`、**CRLF 保留**、
    成功报告替换处数，与 opencode `edit` 一致 ✓
  - 差距（已补）：opencode 的**宽松匹配兜底**——精确 0 次时逐级尝试
    ① 行级 trim（缩进/行尾空白差异）→ ② 首尾行锚定（≥3 行块、中间行有差异）；
    命中会**注明"宽松匹配：原因"并建议复核**；多候选时报告"宽松匹配不唯一"（不猜）；
    实现为 `tools._fuzzy_edit_span`（纯函数，三级策略有独立测试）。
  - 仍有的差异（未做）：Codex 的 `apply_patch`（一次调用可跨多文件/多 hunk）。
- **测试**：`test_code_workspace.py` +3（行尾空白兜底、首尾行锚定、多候选不唯一保持不动），
  全量 pytest **361 passed**。

### 2026-10-17 第二十八轮：`apply_patch` 多文件补丁（Codex V4A 风格，原子生效）

- **动机**：上一轮对比后剩的最后差距——Codex 的 `apply_patch` 一次调用可跨多文件/多 hunk。
- **格式**（docstring 即规格，与 Codex 相同）：`*** Begin Patch` /
  `*** Update|Add|Delete File:` / `*** Move to:` / `@@ 锚点` + 上下文行（` `/`-`/`+`）。
- **实现**：
  - `tools._parse_apply_patch`（纯函数解析，错误全收集）；`_match_line_block`
    （逐行精确 → 行级 trim 宽松；有 `@@` 锚点时不做"从头再找"兜底）；
  - 工具 `apply_patch`：**两阶段**——先在内存里完成全部解析/定位/内容计算（黑名单、
    越界、Add 已存在/Delete 不存在/移动目标已存在/锚点缺失/上下文定位失败 →
    **整份补丁原子放弃**，报错清单返回给模型）；再统一落盘 + 每文件一条
    `_record_change`（move = 源删除 + 目标新增两条，可分别回退）；
  - 审批：`workspace.WRITE_TOOLS` 收录 + `tool_targets` 用正则从补丁抽全部路径
    （解析不出 → 回退 base，让工具自身报解析错误）；审批预览 = 补丁原文（截断）；
  - 前端多文件 diff：`session.change_diff` 对多行结果逐文件拼接 diff 段（≤8 个）。
- **测试**：`test_code_workspace.py` +5（多文件/留底/拼接 diff、原子失败回滚、
  黑名单与越界拒绝、Move+锚点+宽松匹配、解析错误与纯插入缺定位）+1 tool_targets；
  mock 触发词「打补丁」+ `cdp_panel_check.js` 第 20 步（多文件 diff 两个文件名都在，
  `多文件补丁: PATCH_OK`）。全量 pytest **367 passed**。

### 2026-10-17 第二十九轮：压缩指令空字段规则强化

- **用户反馈**：压缩时若某字段没有相关内容，不要让模型硬去总结/填充。
- **改法**：四份压缩指令（叶子/父层 × 中英）把原有"留空、不编造"强化为：
  **没有相关内容的字段可以直接整个省略（省略=空）**；**不许写「无 / 暂无 / 没有相关信息」**
  这类占位词；不许为填充硬凑空话（"字段少而实，好过看起来齐全"）。父层同样加了
  "可整个省略不写 + 不写占位词"。（渲染层 `_render_fields_lines` 本就跳过空字段，
  模型省略字段不会出现空段。）
- **测试**：`test_compression_contract.py` 增加叶子/父层指令的空字段规则断言。

### 2026-10-17 第三十轮：面板语音输入改点击开关 + 提问卡进对话区

- **用户反馈 1（面板语音）**：展开面板里 🎤 按钮点了没反应（那是"连续对话模式"开关，
  在用户环境里收不到/转不出文字）；且发送按钮的长按录音不好用。要求：**点一下开始录音、
  再点一下结束 → 转成文字填进输入框**。
  - `#voice-btn` 从 voiceMode 开关改为**录音开关**（`togglePanelVoice`）：开始录音先关掉
    连续对话模式并打断在播语音（防抢麦），结束后 `POST /asr` → 文本**追加到输入框**
    （不自动发送）。录音中红色呼吸（`#voice-btn.recording`）。
  - **发送按钮长按录音整体移除**（`pressMs/pressTimer/micStop/finishMic` 等），回归纯
    发送/停止；悬浮球长按录音保持不变。
  - 连续对话模式仍可从悬浮球右键菜单开关（面板按钮的 active 青色态即它）。
- **用户反馈 2（提问弹窗）**：ask_user 的全屏遮罩弹窗把背景调暗、挡住对话内容，
  "参考原本的信息"时没法看。要求：**当作对话区的一部分，不遮挡其它内容**。
  - 删除 `#question` 遮罩弹窗（index.html/CSS/app.js 旧逻辑），改为**内联提问卡**：
    `showQuestion` 在 `#messages` 尾部插入 `.question-inline`（标题 + 问题 + 选项按钮 +
    自由输入 + 倒计时），强制拉底提醒并聚焦小输入框；回答/超时/被取代 →`settleQuestionCard`
    把卡片收成一行状态（已选择/已回答/已跳过），不再有遮罩。
- **测试**：`cdp_panel_check.js` 第 10 步改为断言内联卡（找不到旧弹窗 DOM、
  卡片在 `#messages` 内、点选项后卡片 settled + 工具结果出现）。

### 2026-10-17 第三十一轮：修复"生成中收/展面板丢在途内容"

- **用户反馈**：发消息 → 生成中缩成悬浮球 → 再放大：**中间的工具调用和思考流
  全没了**（对话区只剩最终文本）。
- **根因**（两处叠加）：
  1. 面板**隐藏期间主进程不把事件投给面板窗口**（只给悬浮球冒气泡/音频），
     隐藏期间产生的 tool_call/tool_result/thinking_stream 没有进面板 DOM；
  2. 展开完成时 `onPanelShown` 无条件 `loadHistory(true)` **重建对话框**——而
     `/history` 只读 buffer，本轮在途的思考/工具消息要等 `_run_agent` 结束才回写，
     重建把它们整个抹掉（`thinking_stream` 连 buffer 都不进，丢了就是永久丢）。
- **修复**：
  - `main.js`：`/dequeue` 事件**实时投给面板窗口（含隐藏态）**（audio 除外，
    按形态单播给气泡/面板）；`pendingEvents` 收窄为"面板页未加载完"的暂存（容量 200）。
  - `app.js`：`applyState` 维护 `isGenerating`；`onPanelShown` **只在空闲时**
    `loadHistory(true)`（生成中 DOM 已是完整现场——事件隐藏期间也实时进 DOM）。
  - `llm.py` mock：「考我」分支带「多想」时附带思考流（e2e 造"隐藏期间在途思考+提问卡"场景）。
- **回归**：`cdp_panel_check.js` 第 21 步（HIDEGEN_OK）：隐藏 → 发「出题，多想一下」
  （mock 思考 + ask_user 挂起）→ 提问自动展开 → 断言思考块/提问卡/引导语仍在 →
  答完断言工具结果与收尾回复完整。全量 pytest 367 + e2e 全绿，release v0.1.223。
- **顺带揪出的两个潜伏 bug**：
  1. **morph 陈旧兜底定时器**（morph.js）：morph-out 经 `transitionend` 完成时，
     它的 590ms 兜底定时器没有取消——若 590ms 内又开始 morph-in（收起后马上弹
     审批/提问会这样），旧定时器把"out 完成"再发一次 → 主进程把刚展开的面板又
     隐藏（状态机错乱、e2e `RESHOW_FAIL` 的直接原因）。修复：每次 `runMorph`
     递增 `morphGen` 并清掉上一代的定时器/监听器，两处回调都校验代数；
     main.js 的 `morph-in/out-done` 处理器加状态守卫（非 morphing_* 忽略并记日志）。
  2. **mock 触发词按全历史扫描**：一个触发词用过就永远留在历史里，会被后续消息
     重复命中——所以**新触发词必须加在旧词之前**（「考我」放最前会劫持后面所有
     步骤、放最后会被「打补丁」劫持）。step 21 改用专属词**「出题」**（置于触发链
     最前），并在 `_generate` 里写清楚这条约定。

### 2026-10-17 第三十二轮：喇叭朗读按钮三态（播放中可取消）

- **用户反馈**：气泡旁的喇叭点了会朗读，但**播报过程中再点没法取消**。
- **改法**（`app.js` + `style.css`）：喇叭按钮做成三态——
  空闲点=合成并朗读（loading 脉冲）；**合成中再点=取消**；**播放中再点=停止播报**
  （图标变红色方块 + 标题「停止播报」，点击 pause + 清 src）。播放中按钮背景泛红，
  去掉 loading 的 `pointer-events:none`（要不没法点着取消）。
- **实现细节**：单音频元素 `ttsAudio` 被自动朗读/流式 TTS 共用——任何新播放开始前
  `clearSpeakPlaying()` 复位旧"播放中"按钮；旧异步流程按全局 `speakToken` 作废，
  按钮自身复位按 `dataset.gen`（别的按钮被点不影响本按钮复位）；`waitAudioEnded`
  监听 **ended/pause/emptied/error**（换 src 在 Chromium 不一定派发 pause）。
- **回归**：`cdp_panel_check.js` 第 22 步：点喇叭 → 等 `.playing` → 断言音频在播 →
  再点 → 断言按钮复位、标题回「朗读这条消息」、音频已停（SPEAKSTOP_OK）。
  环境播放失败（无音频设备/合成失败）软跳过 SKIP_PLAY，不判红。

### 2026-10-17 第三十三轮：修复"关掉语音播报仍在朗读"

- **用户反馈**：设置里已关语音播报并保存、甚至重启，回答还是会自动朗读。
- **排查**（读日志定位）：用户 23:39:17 在**运行中**保存了 `tts_enabled=false`
  （日志有「语音播报 关闭」），但 23:43-47 日志里仍有大量 `/tts/say` 合成——
  发声的是**面板 voiceMode 的流式 TTS**（普通模式也启用，text_stream 逐句合成），
  它只看前端 `ttsEnabled` 标志。
- **根因**：设置页保存后**从未调用 `window.planner.settingsSaved()`**——
  主进程的 `settings-saved → 重拉 /settings → 广播 bubble+panel` 链路是死的，
  面板 `onSettings` 永远收不到变更，`ttsEnabled` 一直是 attach 时拉的旧值
  （重启后才能通过 attach 再拉一次，所以"重启才停"）。后端侧其实正常
  （`_enabled` 立即关，只是流式 TTS 不经过后端整句合成）。
- **修复**：① 设置页保存成功后调 `settingsSaved()`；② `voiceMode.onSettings`
  收到 `tts_enabled=false` 时立即 `stopSpeakingNow()`（暂停正在播的 + 清队列/预取），
  不再"播完这句才停"；③ 抽出 `stopSpeakingNow()` 供 tts_mute/关闭开关共用，
  导出 `voiceMode.ttsEnabled()` 供 e2e 断言。
- **回归**：`cdp_panel_check.js` 第 23 步（TTSSYNC_OK）：面板确认基线 true →
  CDP 打开设置页 → 取消勾选「语音播报」→ 点保存 → 面板 `ttsEnabled()` 变 false。
- **后续追补（同上问题第二层）**：修完运行中同步后用户反馈"重启后仍播报"。
  拿真实启动时间线对账：面板 00:06:54.7 加载、后端 00:06:59 才就绪——
  **voiceMode attach 的一次性 `/settings` 拉取跑在后端就绪之前，静默失败，
  ttsEnabled 停在默认 true**，之后没人重拉（ADR：默认值与持久化设置不一致时，
  一次失败拉取 = 永久错值）。修复：`main.checkBackend` 成功（后端上线）时
  `fetchSettingsAndBroadcast()` 主动补发全量设置广播（与 `settings-saved`
  共用；渲染进程无论如何都晚于长轮询前收到真实设置）。验证方式：先起 electron
  再起后端 → 面板 `voiceMode.ttsEnabled() === false` 且发消息无任何合成。

### 2026-10-18 第三十四轮：网络搜索改走 DeepSeek 官方 Search/Fetch

- **用户要求**：DeepSeek 上线了官方搜索（`/v1/tools/search`、`/v1/tools/fetch`，
  自研引擎、按次计费 5 元/千次、QPS 5），把 web_search / fetch_web 改用它。
- **改法**（`tools.py`，聊天 provider 无关）：
  - `_deepseek_search_config(session)`：Key 取 `llm_api_key_deepseek` > 旧字段
    `llm_api_key` > env `LLM_API_KEY`（用 OpenCode 聊天也只需填 DeepSeek 分服务商
    Key），base 取 `llm_base_url_deepseek` > 官方端点；
  - `_deepseek_search`：POST search（`count` 1~50、`need_content=false`），
    摘要用官方 `summary`（600~800 字，为模型阅读设计）、附 `date`（截 YYYY-MM-DD）、
    过滤无 url 脏结果；
  - `_deepseek_fetch`：POST fetch → Markdown 正文（`【标题】+ 正文`，按 max_chars 截断）；
  - **回退链**：无 Key / 调用失败（429/5xx/404 等）→ 原内置爬虫链（360 → bing）
    与内置 HTML 抓取（附【页面链接】），老环境零回归；`_search_web` 增加 session 参数。
- **测试**：`test_web_tools.py` 加 autouse fixture 默认禁用官方链路（防 conftest
  的 `LLM_API_KEY=test-key` 让用例真联网），另加 4 个显式用例（官方搜索命中/回退、
  官方抓取命中/回退）。全量 pytest **371 passed**（+4）。

### 2026-10-18 第三十五轮：界面全量重做（中性灰企业风）

- **用户要求**：界面要更简洁、高级，像"真正企业开发的 app"，降低 AI 味。
- **设计语言**（新基线，写进 AGENTS）：中性灰 + 单一强调色 `#2563eb`（Linear /
  Vercel 气质）——去掉玻璃拟态（backdrop-filter）、光晕装饰、渐变按钮/气泡、
  emoji（图标改内联 SVG：设置齿轮、麦克风）；面=纯白 + 1px 实色边框，页底
  `#f6f7f9`，圆角 12/10/8，阴影只给浮层；用户气泡=浅灰、助手气泡=白底描边。
- **改动范围**：`style.css`（全量：令牌/标题栏/Tab/气泡/思考行/工具行/输入区/
  日历/审批/提问卡/浮层/chips/变更页，含状态条 chip 纯文字化）、`index.html`
  （SVG 图标 + 菜单/按钮文案去 emoji）、`app.js`（chip/思考行/工具行/预览卡/
  目录面板等所有文案设置点）、`settings.html`（同套令牌）、`bubble.css`/
  `toast.css`（球体统一品牌青绿、去霓虹、toast 实色白）。
- **行为不变**：只动视觉与文案，不改任何交互/事件/ID；e2e 第 4 步的模型/强度
  chip 断言改为等待异步填充后的文本（不再依赖 emoji）；全量 23 步 cdp 检查通过。
- **走查**：CDP 截图逐一核对面板主界面/强度菜单/日历页/设置页/悬浮球。

### 2026-10-18 第三十六轮：render_page —— 开发网页时的"视觉自查"能力

- **用户需求**：小助以后要自己开发项目，开发过程中需要"CDP 视觉能力"——
  渲染自己写的页面 → 截图 → 自己看效果（而不是看小助自己的界面）。
- **实现**（`tools.py` 常驻工具 `render_page`）：
  - `_find_browser()` 探测本机 Edge/Chrome（PATH → 常见安装路径）；
  - 无头渲染：`--headless=new --screenshot --window-size=W,H --virtual-time-budget=8000`
    （等页面脚本跑完再截），**独立临时 user-data-dir**（不碰用户浏览器配置、不与
    运行中的 Edge 冲突）；60s 超时兜底；
  - 输入：本地文件（相对路径按工作目录解析 + 黑名单拦截）或 http(s) 地址（dev server）；
  - 输出：PNG 存 `data/screenshots/`（保留最近 30 张）→ **与 view_image 同链路
    注入上下文**（`_pending_screenshots` → 下一轮 user 消息图片块）；非视觉模型返回路径；
  - `prompts/workspace.md` 加了使用习惯："改完 HTML/CSS 必须 render_page 看过截图再收工，
    发现问题就改、再渲染、迭代到满意"。
- **验证**：pytest +7（`test_render_page.py`：注入/http 直传/非视觉/无浏览器/路径校验/
  黑名单/无产出）；**真机用 Edge 渲染验证页并人工核对截图**（样式/脚本/CSS 全对）。
  全量 pytest **378 passed**。

### 2026-10-18 第三十七轮：buffer 持久化加固（收消息即落盘 + 断点续聊）

- **事故**：外部进程被强杀（AI 清理调试实例时无差别杀进程，误杀 release），
  用户"最后一轮消息"随内存丢失——之前 buffer 只在**生成结束**才写盘。
- **修复**：
  - `_receive` 收到任何消息（玩家/心跳/提醒）**立即 `_save_buffer_state()`**；
  - `buffer_state` 新增 **`inbox` 列**（生成进行中排队待处理的消息也落盘；
    老库打开时 compat 自动 `ALTER TABLE` 补列）；
  - `_load_buffer_state` 恢复 inbox → `pending_response=True` → `__init__`
    拉起 `_player_worker` **断点续聊**（重启自动把排队的消息接着处理）；
  - `_save_buffer_state` 在 buffer_lock 下快照 inbox，与 buffer 一并原子写入。
- **语义**：强杀/断电最多丢"正在生成、还没收束的半句"；用户消息与排队消息不再丢。
- **测试**：`tests/test_buffer_persistence.py`（记忆库 inbox roundtrip / 收消息即落盘
  / 排队消息恢复并续跑）+3；全量 pytest **381 passed**。
- **善后**：用户丢失的那轮从 `changes.db`/assistant jsonl 还原出上下文（在整理
  「行测」资料），已写出的文件（README/01-资料分析.md）都在磁盘上，让其重启后
  说"继续"即可接上。

### 2026-10-18 第三十八轮：上下文占用与用量统计合并

- **用户要求**：顶部栏的「上下文占用」和输入框里的「命中率 chip」内容相似，
  合并成一个；chip 显示"原始消息上下文长度 + 命中率 + Token 速度"（后两者取
  近 50 轮平均）；仍可点击展开详细页。
- **改法**：
  - 删除标题栏 `#ctx-tokens` chip 与 `#ctx-panel` 浮层（`.tb-chip` 样式随之移除）；
  - 状态条 chip（`#cb-stats`）改为**合并 chip**：`原始 17.0k · 命中 75% · 272 t/s`
    ——「原始」= `context_stats().breakdown.raw`（原始消息估算 token，压缩预算的
    判定对象；万位以上显示 k）；命中率/TPS 为近 50 轮平均（无数据显 —）；
    `max-width` 放宽到 260px 防截断；
  - 点击展开**合并面板**（`#stats-panel`，标题「上下文与用量」）：上半 = 上下文
    分解条（系统/节点/原始/图片 + API 实报输入脚注），分隔线，下半 = 用量明细
    （近 50 轮 + 口径注释 + 逐条记录）。
  - `applyState` 更新 `lastCtx` 后即刷新 chip（上下文变化要反映到"原始"分项）。
- **测试**：`cdp_panel_check.js` 第 11 步更新（chip 含 原始/命中/t/s；面板含
  ≥4 条 ctx 分解条 + 脚注 + 用量明细）；23 步全绿。截图走查 chip 与合并面板。
- **后续追补**：用户反馈"每次打开要点一下 chip 才显示命中率/TPS"——又是**启动竞态**
  （面板先于后端加载 → `/llm/stats` 首次拉取失败且无重试；点 chip 会触发
  `refreshStats()` 所以"点一下就好"）。修复：`setOffline` 恢复路径一并
  `refreshStats()`（与 refreshLlm 同处）。验证：预置 llm_stats.json → 先起界面
  再起后端 → chip 自动补齐「命中 75% · 300 t/s」无需点击。release v0.1.232。

### 2026-10-18 第三十九轮：修复压缩空转（raw 18 万不降）

- **用户反馈**：原始消息涨到 18 万（预算 15 万），发消息触发压缩（回得慢、
  该轮命中率 3%），但界面上「原始消息」一直不降——"触发了压缩结果没有替换？"
- **诊断**（用户日志）：`触发异步压缩（raw=542）→ with_structured_output 失败
  → JSON 解析失败 → 叶子摘要为空，跳过`，每次生成结束都重演，raw 只涨不降。
- **根因（三坑叠加）**：
  1. **摘要调用开着思考**：`_get_summary_model` 跟随用户 thinking 设置
     （OpenCode 上 adaptive→enabled），推理 token 吃光 `max_tokens`(1500)
     → 返回空内容 → 摘要为空；
  2. **超大批次不分片**：533 条/18 万 token 整批作为消息发给模型 → 超上下文/极慢
     → 调用失败；
  3. OpenCode 端点不支持 `with_structured_output(json_mode)`，每次压缩白等
     ~15s 才降级。
- **修复**：① 摘要模型显式 `thinking_mode="disabled"`（提取任务，质量不敏感）；
  ② `_split_batch_by_tokens` 分片（≤3 万 token/片、≤12 片/轮，只在 tool 配对
  安全边界切，片失败即停手防序号错位）；③ `_compress_no_structured` 标记：
  structured 首次失败后本进程直接走降级链；④ `_invoke_compress` 各降级分支
  日志带异常信息（这次排查的最大障碍就是日志只说"失败"不说为什么）。
- **测试**：`test_compression_contract.py` +5（分片大小 / tool 配对安全边界 /
  thinking=disabled / 降级标记 / 端到端多节点替换——后者还抓出测试消息缺 id
  导致 RemoveMessage 对不上的问题）。全量 pytest **386 passed**；release v0.1.233。

### 2026-10-18 第四十轮：压缩第二波修复 + 压缩指示灯

- **用户反馈**：① 后台压缩时想要可见提示（右上角呼吸灯）；② "还是没有替换"。
- **实况核对**：用户重启后压缩**真的在跑**（node0_158~163 落树），12:18:32
  「异步压缩完成：删除 546 条，插入 9 节点消息」——buffer 574 条 → 37 条
  （15 原始 + 22 节点）✓。用户看到"没替换"是因为那轮压缩跑了 **5 分钟**
  （每次分片浪费两次调用），查看时还没到"空闲替换"那步。
- **第二波根因**：`max_tokens=1500` 对任务型 JSON（6 字段）太小 → 输出被截断
  → JSON 解析失败 → **降级链第二跳把整段 JSON 原样存成摘要**（节点里全是
  `{"summary": ...}` 外壳；还漏了解析第二跳的 JSON）。
- **修复**：
  1. `max_tokens` 1.5× → **3×**（1000 预算 → 3000；软裁剪仍由 `_apply_budget` 兜底）；
  2. 降级链第二跳**先按 JSON 解析**，仍坏则 `clean_wrapped_summary` 抠出
     summary 字符串（完整 JSON→json.loads；截断 JSON→正则 + 基本反转义）；
  3. **渲染层剥壳**：`_render_node_text` / `_node_full_text` 都过
     `clean_wrapped_summary`（历史坏节点展示自动干净）；
  4. **启动自愈**：`_load_buffer_state` → `_heal_wrapped_node_summaries`
     原地剥掉缓冲里节点消息的 JSON 外壳；
  5. **压缩指示灯**（用户要求）：压缩开始/结束推 `compress_state` 事件 +
     `state_dict().compressing` 兜底；前端标题栏「压缩记忆中…」+ 琥珀色
     呼吸灯（`.dot.compressing`，与"思考中"蓝色区分）。
- **测试**：+4（剥壳/截断抢救/state 标志/自愈）。全量 pytest **390 passed**；
  release v0.1.234。

### 2026-10-18 第四十一轮：上下文展示改单条堆叠（固定 100 万口径）

- **用户要求**：四条分解条看着太多不好对比——改成**一条**：同一根条里用不同颜色
  表示各分项；总量口径固定 **100 万 token**（总窗口），显示各分项占 100 万的
  **绝对比例**（不再显示相对当前总量的比例）。
- **改法**（`app.js renderCtxPanel` + `style.css`）：
  - `.ctx-stack`：单条堆叠条（高 10px），每段宽度 = 分项 / 1,000,000；
  - `.ctx-legend`：一行图例（色点 + 名称 + token 数 + 占 1M 百分比，<0.1% 显示
    `<0.1%`）；颜色沿用：系统提示=蓝 / 记忆节点=绿 / 原始消息=浅蓝 / 图片=琥珀；
  - 脚注：`共 X token · 占 100 万窗口 Y% · N 条消息` + API 实报行；
  - 面板小节标题改「上下文占用（各分项占 100 万总窗口的比例）」。
- **测试**：`cdp_panel_check.js` 第 11 步断言改为 `.ctx-stack` 有分段 +
  `.ctx-legend-item` 恰 4 个 + 脚注含「100 万窗口」；e2e 全绿；截图走查。
  release v0.1.235。

### 2026-10-18 第四十二轮：压缩改"整批优先"（一次压缩 = 一个节点）

- **用户疑问**：L0 节点好几个、范围都很小，"感觉不是 15 万压一次"；
  并追问分片机制是哪来的（**是我修压缩空转时自己加的，未与用户商量**——教训：
  引入新机制要先说明）。
- **事实核对**（树数据）：node0_149~157（30~43 条/个）是 **9 月~10/17 旧版
  "60 条触发"**时代的遗留；node0_158~163（36~148 条）是今天**一次** 15 万
  触发的分片结果（范围 5203~5736 连续）。触发确实是"raw ≥ 15 万一次"。
- **改法**（`middleware._compress_batch`）：**默认整批一次调用 = 一个 L0 节点**；
  只有两种情况回退分片：① 批次估算 > `_ONE_SHOT_MAX_TOKENS`（30 万，防超大
  积压）；② 整批调用失败（如端点超窗）→ 分片重试（失败不落任何东西）。
  顺带抽出 `_estimate_message_tokens` 复用估算口径。
- **测试**：`test_compression_contract.py` 调整+新增（整批单节点 / 失败回退
  分片 / 端到端单节点替换）；全量 pytest **391 passed**；release v0.1.236。

### 2026-10-18 第四十三轮：截屏（右键菜单）+ 加号按钮（添加文件）

- **用户需求**：① 悬浮球右键菜单加"截屏"——类微信：框选、✓/✗、可涂鸦；
  **✓ 后不直接发送**而是进入待发送态（长按悬浮球语音发送会带上；展开面板
  在输入框上方显示，发消息一起发）。② 展开面板输入区加"+"按钮选文件。
- **实现**：
  - **截屏**：`main.startScreenshot()`——`desktopCapturer` 截光标所在屏幕
    （物理像素，记 `scale` 供裁剪换算）→ 藏气泡（不截自己）→ 全屏覆盖层
    窗口（`screenshot.html/js/css`；`#sel` 用 box-shadow 遮暗选区外；
    画笔/撤销，笔迹按 scale 一起裁进导出图；✓/✗ 工具条贴选区下方，
    放不下翻到上方；Esc/右键取消、Ctrl+Z 撤销、Enter 确认）→ 确认后 PNG
    存 `userData/screenshots/`（保留最近 50）→ **挂进 mountedFiles**——
    与拖拽/加号完全同一套"待发送"机制（面板输入框上方显示；悬浮球长按
    语音发送时自动带上），toast 提示"截图已挂载"。
  - **加号**：输入区 `+` 按钮 → `pick-files` IPC（系统多选对话框，分类
    口径与 fileDrop 一致）→ 挂载。
  - 坑：覆盖层事件用 window 级监听，合成事件 `e.target` 是 window——
    守卫须 `typeof e.target.closest === 'function'`（首测 NO_TOOLS 的原因）。
- **测试**：`cdp_panel_check.js` 第 24 步（SHOT_OK：模拟框选→✓→挂载数 +1
  且文件落盘、再截→✗→挂载数不变；无显示环境软跳过）；24 步全绿；
  真机截图核对（裁剪+笔迹）。release v0.1.237。
- **首版用户实测翻车与修复（同日）**：点「截屏」悬浮球消失了但覆盖层没出来，
  之后二次点击"没反应"（守卫拦住残影窗口）。根因：**`ready-to-show` 监听器
  挂在 `loadFile` 之后**——本地小页面加载极快时事件抢在监听器之前发出，窗口
  永远隐藏（e2e/CDP 驱动不受影响所以没抓到）。修复：监听**先挂再 loadFile** +
  `ready-to-show`/`did-finish-load`/600ms 定时器**三重兜底显示** +
  不可见残影窗口自动销毁重建 + 全程 appLog。验证：窗口枚举确认
  `[截屏] visible=True`。另按用户要求把「+」改成**菜单**（截屏 / 添加本地文件）。
  release v0.1.238。
- **第二波用户实测修复（同日）**：
  1. **笔只能画在选区外面**——`#sel`（显示选区原图）DOM 排在 `#ink`（画笔层）
     之后 → 画在选区里的笔迹被选区图盖住，只有选区外的可见（导出又被裁掉，
     所以"截屏效果不好"）。修复：`#ink` 移到 `#sel` 之后 + 显式 z-index
     （shot 0 / sel 1 / ink 2）。
  2. **确认可能静默失效**——底图（大屏 base64 十几 MB）解码慢时点 ✓ 会
     `!img.complete` 静默 return，用户以为要点 ✗ 才能退出 → 取消 → 什么都没挂载
     （"看不到待发送文件"）。修复：确认时等底图加载（最多 3s）+ reportLog。
  3. 复现验证：面板加号→截屏→确认 → 挂载条出现/× 可删除；悬浮球→截屏→
     展开面板 → 挂载条出现（两条流程 DOM 断言全过）。release v0.1.239。
  4. **挂载条位置**（用户要求）：从对话区顶部挪到**输入框正上方**（DOM 移到
     `#input-bar` 之前）——放顶部用户注意不到；`#jump-bottom` 相应下移避让。
     release v0.1.240。

### 2026-10-18 第四十四轮：记忆树翻阅增强（用户反馈"只查一次就放弃"）

- **用户反馈**：调 `explore_memory_tree` 只试一次就说找不到；问树结构有没有
  在系统提示词里讲清楚（上抽象/下具体、可逐层展开找回具体对话）。
- **诊断**（三个导航缺陷）：
  1. 留空调用只返回 `get_root_id()`（level 最高**单个**节点）的 children——
     树有多支并列时看不到其它支，概览不完整；
  2. 展开非叶节点的 children **没有时间/条数范围**（只有 node_id+summary）——
     docstring 说"可按时间缩小范围"但数据不支持，模型没法挑分支；
  3. 坏编号（拼错/已被合并的旧编号）只回"找不到节点 xxx 的记录"，读起来像
     "记忆里没有"——模型据此收工。
- **修复**：
  - `get_root_overview()`：所有 active 且无父节点的节点（森林根），带
    level/条数范围/时间范围 + 摘要截断 200 字；留空调用改为返回它（文本格式，
    每支一行 + "选一支展开"提示）；
  - `get_node_children_info()`：children 带 level/round_start/round_end/
    time_start/time_end，查询节点自身也带 id/level/范围；摘要过
    `clean_wrapped_summary`（顺手治 JSON 外壳）；
  - 坏编号提示改为：「该编号不存在（可能拼错或来自已被合并的旧消息），**不是
    记忆里没有**——留空看顶层概览，或改用上下文里的 node 编号再试」；
  - `system.md`：新增「记忆树的结构与翻法（逐层下钻）」bullet——下具体/上抽象、
    2~4 次调用的下钻循环、叶子展开=原文、坏编号≠没记忆；工具 docstring 同步重写。
- **测试**：+3（children 范围字段 / 顶层概览多支与合并后单支 / 工具层概览与
  坏编号提示）；全量 pytest **394 passed**；release v0.1.241。
- **同日补充（用户校正框架后，v0.1.242）**：
  - **用户指出**：① 根节点**本来就在上下文里**（每次压缩替换时把新节点放进去）
    ——所以"留空看概览"不是主路径，翻树起点就是上下文里的 node 消息；② 翻法 =
    展开 X（X 的摘要已知）→ 返回 X **下一层**子节点摘要 → 自己判断往哪支钻 →
    叶子展开返回其覆盖范围的原文——与实现一致，但提示词/docstring 此前把"留空
    概览"写成第一步，**改为主路径表述**（留空=备用视图）；③ 节点在上下文里本就
    带条数/时间范围（渲染头 `[nodeX · Lx · 第a-b条]` + `[时间]`），此前缺范围的是
    **工具返回的 children 条目**（已补，且展开节点自身也带摘要+范围，叶子返回
    同时给自身摘要——模型可能直接跳到叶子编号）。
  - **真实性强调（用户要求）**：system.md 节点消息 bullet 增加"**里面记的都是
    真实发生过的事，不是编造的故事**——只是摘要压缩，需要原始细节用工具找回"
    （原来只说"你的亲身经历"，未明说"真实/非编造"）。
  - 回归：`test_memory_tree.py` 叶子断言补摘要字段；全量 pytest **394 passed**；
    e2e 全绿。

### 2026-10-18 第四十五轮：视觉误判修复 + 输入框粘贴

- **用户实测问题 1**：让助手看目录里的证件照（view_image），工具回"当前模型
  不支持视觉；OCR 未识别到文字"——用户用的是 V4.1（多模态），为什么？
  **根因**：`is_vision_model` 按模型名子串判定，收录了官方名 `deepseek-flash`，
  但 **OpenCode Go 上同一模型叫 `deepseek-v4.1-flash`**（用户的服务商）——
  不含子串 `deepseek-flash` → `vision_capable=False` → view_image 与挂载图片
  全部静默降级 OCR。**修复**：收录 `v4.1-flash` 子串（`deepseek-v4-flash`/
  `deepseek-v4-pro` 仍判纯文本——v4 老版/Pro 无视觉，test_vision 断言保护）；
  修复后 view_image/挂图/截图发送都走原图注入。
- **用户实测问题 2**：输入框不能 Ctrl+V 粘贴（图片/文档）。**实现**：
  textarea paste 事件——纯文本不拦截；图片/文件 → `handleClipboardFiles` →
  挂载（同拖拽/截图/加号机制）。**剪贴板位图没有磁盘路径**（截图工具复制后
  直接粘贴）→ 新增 `save-pasted-file` IPC 落盘 `userData/pasted/`（保留 50）
  → 按图片挂载；资源管理器复制的文件有路径走原路。
- **回归**：test_vision +1 断言；`cdp_panel_check.js` 第 25 步（合成 paste 事件
  → 落盘 → 挂载 → PASTE_OK）；全量 pytest **394 passed**；e2e 全绿；
  release v0.1.243。

### 2026-10-18 第四十六轮：压缩触发估算计入图片 base64（用户实测）

- **用户反馈**：触发预算设 20 万，但原始消息才 1 万多就开始压缩。
- **根因**：`should_compress_raw` 用 `len(str(m.content))` 估算——视觉注入的
  user 消息 content 是块列表（text + image_url），`str(list)` 把 **base64
  data URL 整个算进字符数**（一张 1MB 图 ≈ 百万级字符 ×1.2 → 估算瞬间超 20 万）；
  而状态条 chip 的"原始"走 `context_stats`（只算文本块 + 图片每张 384）——
  两边口径不一致，看起来就是"1 万多就触发"。用户刚看过照片/发过图，缓冲里
  有 image_url 消息，每次调度 tick 都命中触发。
- **修复**：① `should_compress_raw` 改用 `content.content_text()`（只取文本块）
  + **图片每张按 384 计**（与 context_stats 同口径）；② `_estimate_message_tokens`
  同步改用 `content_text`；③ **压缩调用输入剥离 image_url 块**
  （`_strip_image_blocks` → 「（图片）」占位）——原来批次里带图会把 base64
  整个发给摘要模型（浪费且无用；详情存储侧清洗原有）。
- **回归**：`test_compression_contract.py` +2（base64 不计字符、压缩输入无
  base64）；全量 pytest **396 passed**；release v0.1.244。

### 2026-10-19 第四十七轮：show_image 历史回看修复（持久副本）

- **用户反馈**：助手展示的图片只在当时那条回复里能看；之后再问他，消息里的
  图片显示「无法加载」。问是不是预期设计。
- **根因**：历史重建（/history → 前端 `addImage` → api-image → `/file?path=`）
  按**原路径**重新拉图。工作区原文件之后被删除/修改/移动 → 404/403 → 前端
  「（图片加载失败）」。用户实测场景：助手迭代简历时展示 `_preview_1.png` /
  `_preview/resume_preview.png`，之后这些临时预览文件被清理，回看就报错
  （网络图片没问题——本来就落 `data/images/`）。
- **修复**：`show_image` 展示工作区文件前，**先复制持久副本到 `data/images/`**
  （`_persist_image`，内容哈希命名、重复展示去重）——`/file` 的允许根含
  data/images，之后原文件怎么变都不影响历史回看；docstring 同步说明。
- **回归**：`test_show_image.py` 本地用例补断言（展示路径指向 data/images 副本、
  删除原文件后副本仍在）+ 历史用例断言指向持久副本；全量 pytest **396 passed**；
  release v0.1.245。

### 2026-10-19 第四十八轮：改动文件快捷块（用户需求）

- **用户需求**：每次回复后，把它改过的文件放在回复下面做快捷入口——「打开文件」
  与「打开所在目录」两种方式；**按改动次数排序**（改得多的排前面）。
- **实现**：
  - 后端：`session.changed_files_for_ids(ids)`（changes.db 去重计数 + missing 标记
    + 次数降序）；实时路径在 `_emit_tool_events` 累积 `_turn_change_ids`、
    `_run_agent` 结束推 `changed_files` 事件；历史路径 /history 遍历 buffer 累积
    ToolMessage 的 change_ids，在最终回复后挂 `role=changed_files` 条目。
  - 前端：`addChangedFiles`（实时 + 历史同一渲染）；新 IPC `show-in-folder`
    （`shell.showItemInFolder` 选中文件）；「打开」复用 `open-path`。
- **过程中挖出并修复的两个真 bug（e2e 暴露）**：
  1. **输入框发送后"正在输入"状态残留**：submit 处理清空 `input.value` 时没同步
     typing 状态（不派发 input 事件）→ 后端一直以为用户在打字 → 每次模型调用被
     `TypingHintMiddleware` 追加提示；对真实模型只是"误提示"，对 **mock 脚本是
     致命**——脚本按"最后一条消息"决策（工具结果 → 收尾），提示顶掉判定 →
     触发词死循环到单轮调用上限（实测打补丁轮循环 ~100 次、出题轮双问 + 120s
     超时）。**用户随后决定直接砍掉该功能**：`TypingHintMiddleware` 整个移除
     （前后端 typing 状态链路：`/typing` 端点、`setTyping` IPC、`session.typing`、
     `syncTypingState` 全部删除）——提示对真实模型价值有限，带来的状态同步坑
     不值当。
  2. **mock 触发词劫持**：新步骤（第 26 步）排在第 21 步「出题」之后，
     「打补丁」被历史里的「出题」劫持（全历史扫描、优先级=分支顺序）→ 新词
     「补丁块」加在最前（专属快捷块步骤），`_e2e_patch_text()` 抽出共用负载。
- **回归**：`test_changed_files.py` +2；`test_agent.py` 打字提示用例随功能移除
  （删 `test_typing_hint_injected_temporarily`）；`cdp_panel_check.js` 第 26 步
  （`补丁块` → BLOCK_OK + /history 条目 → CF_OK）；全量 pytest **397 passed**；
  e2e 全绿；release v0.1.248。

### 2026-10-19 第四十九轮：改动文件块改一行折叠 + 按钮修复（用户反馈）

- **用户反馈 1**：全展开太占地方——改成**与工具组行同款的一行**：「本轮改动 N 个
  文件 · 文件名…」，按改动次数降序、超出从尾部截断（不做滑动窗口）；点这一行
  才展开，展开后每文件可选「打开」/「目录」。（纯代码链路，无模型参与 ✓）
- **用户反馈 2**：「打开」「目录」按钮点了没反应。**根因**：`tools._record_change`
  调 `db.record` 时**漏传 after_missing**（changes.py 有这个参数但没调用方设置过）
  → 删除的文件留底里 after_missing=0 → 快捷块仍显示「打开」按钮 → 点已删除的文件
  自然无反应（用户数据里 delete_file 记录全是 after_missing=0，实锤）。
- **修复**：
  1. `_record_change` 传 `after_missing = after is None`（删除/移动源/补丁删除）；
  2. `changed_files_for_ids` 的 missing 再兜底查**磁盘实际存在性**（覆盖
     run_command 等非留底方式删除的旧数据）；
  3. 前端「打开/目录」失败时给 `addMessage` log 反馈（不再静默）；
  4. 折叠设计：`.cf-line`（一行，点击 toggle）+ `.cf-body`（默认 display:none）。
- **回归**：`test_changed_files.py` 磁盘兜底用例；`test_code_workspace.py` delete
  用例断言 after_missing；`cdp_panel_check.js` 第 26 步加「默认收起→点击展开→
  按钮 IPC 链路形状」断言；全量 pytest **397 passed**；e2e 全绿；release v0.1.249。

### 2026-10-19 第五十轮：改动块聚合口径（用户反馈"中途多轮各出一块"）

- **用户反馈**：说一句话，助手多轮思考/执行工具（还常以 heartbeat 调度收尾、
  自主心跳续聊），每个回合结束都各出一个改动块——希望**只在回答最后统一出一块**。
- **根因**：实时侧每个 `_run_agent` 结束就 emit + 清空；而 /history 是跨回合累积
  的——两条路径口径不一致，实时侧在多回合/自主续聊时冒多个块。
- **修复（统一为"跨回合累积、回合终点出块"）**：
  - 实时侧：`_turn_change_ids` **不再在回合开始清空**；回合结束只在
    `current_trigger == "player"`（回应用户）时出块；自主回合
    （heartbeat/scheduled/nudge）继续累积；`_receive` 新消息边界先 flush
    （与 /history 的 human 边界一致）。
  - /history：追踪 `turn_autonomous`（注入消息（…）/[当前… = 自主）——自主回合的
    最终回复不出块；玩家回合的**最终回复（无 tool_calls）或 heartbeat 收尾**
    （heartbeat 也是回合终点）都出块。
- **回归**：`test_changed_files.py` +2（自主回合改动累积到用户边界统一出、
  heartbeat 收尾出块）；全量 pytest **399 passed**；e2e 全绿；release v0.1.250。

### 2026-10-19 第五十一轮：/history 回合边界修正（用户截图实证）

- **用户反馈**：截图显示一条消息的回复里仍有多个改动块（"本轮改动 1 个文件 ·
  _preview/_render.py" 出现在工具组后面 + "…resume_preview.png" 在 heartbeat 行
  后面）——回合被切碎。
- **根因（用真实数据副本跑 /history 实证）**：**回合中途的注入消息被当成回合边界**
  ——① nudge（「（你已经忙了一小会儿了…）」第 5 轮收尾提示）；② view_image 注入
  （「（这是小助读取的图片：xxx.png…）」，ScreenShotInjectMiddleware 的 user 消息）；
  ③ [当前议程]。每次注入都触发 `_flush_changed()` → 一回合的改动切成多块；且
  「（…」开头的注入还把 `turn_autonomous` 置 True → 玩家回合的最终回复不出块
  （块漂到下一个边界）。实测：11:49 回合被切成 3 块、11:51 回合切成 2 块。
- **修复**：/history 只有**真正的回合边界**（真实用户消息「对你说：」/「（系统：」
  开头）才 flush + 更新 `turn_autonomous`；nudge / view_image 注入 / [当前议程]
  不再算边界（实时侧本来就不 flush 这些——它们不走 `_receive`，现在两边完全同口径）。
- **验证**：真实数据副本重跑 /history——块数 5 → **2**（11:49 回合 8 文件合一块、
  11:51 回合 2 文件合一块，位置都在回复末尾）；`test_changed_files.py` +1
  （中途注入不切块）；全量 pytest **400 passed**；e2e 全绿；release v0.1.251。

### 2026-10-19 第五十二轮：移除 Nudge 收尾提示（用户决定）

- **用户判断**：「你已经忙了一小会儿了，把最重要的事做完就歇一下吧」是**陪伴期**
  的软性收尾设计；现在小助要完成具体工作（多轮工具是常态），这提示只会让它
  提前收手——不再需要。
- **移除**：`NudgeMiddleware` 整个删除（含 `PlannerState.model_call_count` 字段、
  `_run_agent` 的 state 初始化键、agent 链注册、docstring）；**硬上限
  `ModelCallLimitMiddleware` 计数独立**（用 `run_model_call_count`，不共用该字段）
  ——保险丝不受影响，实测确认。
- **注意**：旧 buffer 里仍可能留有历史 nudge 消息（持久化过）——/history 的
  回合边界判定本来就"正向匹配"（只有真实用户消息/「（系统：」算边界），
  不受影响；对应回归用例保留并注明"历史遗留"。
- **回归**：全量 pytest **400 passed**；e2e 全绿；release v0.1.252。

### 2026-10-19 第五十三轮：Windows 报错框修复（用户反馈"测试总弹窗"）

- **用户反馈**：跑测试时桌面上总弹 Windows「找不到文件 'C:\definitely-nonexistent-xyz\
  nope.txt'」报错框。
- **根因**：e2e 第 26 步的 IPC 链路检查用假路径调 `openInExplorer` →
  `shell.openPath` 对不存在的路径会触发 **Windows 系统报错框**（不只是返回错误码）。
  这也暴露真 bug：用户点「打开」时文件已被删除 → 同样弹系统框。
- **修复**：`open-path` 处理器**先查 `fs.existsSync`**，不存在直接返回
  `{ok:false, error:'not_found'}`（前端给 log 提示，不碰 shell）；`show-in-folder`
  本来就有存在性检查 ✓；e2e 假路径检查现在不再触发系统框。
- **回归**：全量 pytest **400 passed**；e2e 全绿（无弹窗）；release v0.1.253。

### 2026-10-19 第五十四轮：压缩分片重做（X 渐进前缀 + B 立即合并，用户定稿）

- **用户驱动的设计推演**（核心诉求链）：
  1. 叶子太粗——整批 15~20 万压成一个 L0，探索展开一次面对整批原文；
  2. 但分片会伤缓存——逐项推演发现"独立分片"第 2 片起位置错位全价
     （压缩调用 `[S][节点][第k片]` 与聊天序列 `[S][节点][第1片][第2片]…`
     从第 1 片处就分叉），每 20 万事件把批次大部分全价重读一遍；
  3. 结论：**渐进前缀（X）**——第 k 次调用带"第 1..k 片原文"，逐字节复刻
     聊天序列前缀 → **缓存全命中**；指令锚点（"从这条消息开始：「…」"）只压
     最后一片、前面是背景；单段输出解析风险与整批相同；
  4. **立即合并（B）**——n 片全成功后再一次调用合并成 1 个父节点进上下文
     （树里保留分片叶子可下钻）→ 上下文观感与整批一致（一次压缩一个节点）。
- **实现**：
  - `_split_batch_by_tokens`：切点**优先用户消息边界**（浮动窗口 [0.75×, 1.4×]，
    每片=完整若干轮对话）；无 human 边界时到 1.4× 在安全边界兜底收；
  - `_summarize_leaf`：新增 `context_msgs`（渐进前缀发送序列）与
    `anchor_quote`（锚点）；返回 node_id；details/移除/范围仍只针对本片；
  - `_anchor_quote`：本片首条有正文消息的开头 120 字（空 ai(tool_calls) 往前扫）；
  - `_merge_fresh_leaves`：与 `_compact_level` 同构——n 个叶子从树渲染（去标记）
    → `_call_compress`（父层指令）→ `tree.compact(叶子ids)`（叶子转 inactive）
    → `adds` 里 n 个叶子消息换成 1 个父节点消息；
  - 指令模板：叶子"范围"行拆成普通/分片两形态（中英共 6 份模板）；
  - **设置项**：`compress_chunk_tokens`（分片大小，默认 3 万，0=关闭分片整批压）、
    `compress_merge_parent`（分片后合并父节点，默认开）——设置页「记忆压缩」新增
    两个控件（数字输入 + 开关）。
- **回归**：`test_compression_contract.py` 重写/新增（小批次单节点、**渐进前缀
  增长 + 锚点 + 合并父节点 + 树里 n 叶子**、某片失败停手、切点优先 human/浮动）；
  全量 pytest **402 passed**；e2e 全绿；release v0.1.254。

### 2026-10-19 第五十五轮：分片方案四项定稿修正（用户逐条推演）

1. **层级合并持续消化**：某层超阈值 → 一轮内**持续合并到低于阈值**（旧行为每层
   每轮只合一批：10 个(6/3) 只合 3 个、剩 7 个仍超阈值挂到下一轮——用户指出）；
   上限 8 批/轮防跑飞。
2. **节点合并输入只带目标之前的节点**：目标之后的节点（未来信息）不进输入
   （与叶子剔除保留尾部同一原则，防模型把后面状态混进本段摘要；cutoff = 目标
   最大起始序号）；原始消息一律不进。
3. **叶子锚点补尾部**：头部锚点 = 首条有正文消息前 120 字（往前扫 4 条）；
   **新增尾部锚点** = 末条有正文消息结尾 60 字（往后扫 4 条）——防止模型把结尾
   的工具结果误当背景提前收尾；指令改为"从…开始，直到输入最后一条…为止"。
4. **`compress_merge_parent` 默认改为关**（用户定：分片叶子各自进 level 0、由
   6/3 层级合并逐批消化——即用户描述的流程；开启才立即合并成单父节点）。
- **回归**：`test_compression_contract.py` 27 例（+持续消化/目标之后排除/
  默认不合并/双锚点断言）；全量 pytest **405 passed**；e2e 全绿；release v0.1.258。

### 2026-10-19 第五十六轮：删除"分片后合并父节点"（用户确认）

- **用户结论**：该功能无实际价值——① 从未要求过（是设计时自行加的）；② 阈值
  合并本来就会往上合并，"立即合并"与它的唯一差别只是**分组边界**（合 5 个 vs
  合 4 个），对范围/时间/探索/缓存**没有任何影响**；③ 片数本来就不固定
  （≈批次 ÷ 分片大小），"一次压缩一个节点"的说法造成理解混乱。
- **删除范围**：`settings.py`（DEFAULT_SETTINGS/`_BOOLS`/docstring）、设置页开关
  + settings.js 读写、`middleware.py` 的 `_merge_fresh_leaves` 方法与
  `_compress_batch` 里的调用；测试收敛（渐进前缀用例改为断言"n 个叶子各自进
  上下文"、删除重复的默认用例、async 用例断言多个叶子）；AGENTS 同步。
- **无数据迁移**：settings.json 里残留的 `compress_merge_parent` 键会被
  load/save 自动忽略（只认 DEFAULT_SETTINGS 的键）。
- **回归**：全量 pytest **404 passed**；e2e 全绿；release v0.1.259。

### 2026-10-19 第五十七轮：合并漏摘分片叶子（用户实测"10 个 L0、范围重叠"）

- **用户反馈**：节点范围乱（`node1_045` 5939-6046 与 `node0_167` 5958-5998 并存、
  看起来重叠/不连续）；L0 数 10 超过阈值 8；压缩频率感觉高。
- **根因（数据实证）**：`_compact_level` 只从**快照 msgs** 里摘除目标消息——
  层级合并的目标若含**本轮刚产生的分片叶子**（消息在 `adds`、不在快照里），
  消息没被摘除 → inactive 子节点消息与父节点并存于 buffer。证据：node1_045
  的子节点 166-169 中，166（上一轮、在快照）被正确移除，167-169（本轮、在 adds）
  残留；日志"删除 317 条、插入 12 节点消息"（12 = 10 分片 + 2 父节点，本该只留 2+2）。
- **修复**：
  1. `_compact_level` 同步从 `adds` 摘除 target_ids 的消息；
  2. 自愈 `_drop_superseded_node_messages`（启动 + 压缩后）：清理"inactive 且父节点
     消息在场"的旧节点消息（父节点缺失时保留——宁可重复不丢覆盖）——用户现有
     buffer 重启后自动清干净；
  3. **压缩指令附 JSON 形状示例**（`_JSON_SKELETON`）：用户日志显示每次叶子调用
     都因形状漂移（lessons 写列表、user 写字典）失败重试一次（双倍模型调用，
     这也是"频率高/慢"的体感来源）——指令末尾给出精确形状。
- **回归**：`test_compression_contract.py` +2（adds 摘除/自愈清理）；全量 pytest
  **406 passed**；e2e 全绿；release v0.1.261。

### 2026-10-19 第五十八轮：日历页排版（用户反馈"格子好大"）

- **问题**：月历格子 `aspect-ratio: 1/1` + 7 列铺满面板 → 每格 ≈90×90，占满整页
  高度，下方日程列表被挤到看不见。
- **改法**：格子改为固定高 **38px**；网格 `width:100% + max-width:520px +
  align-self:center`（紧凑居中）；星期头字号/间距收紧；角标缩小（10px）。
- **踩坑**：初版用 `margin: 0 auto` 居中——`.view` 是 flex 列容器，**auto 边距会
  让 flex 子项收缩成内容宽度**（实测网格缩到 93px、星期头挤成一团）；改用
  `width:100% + max-width + align-self:center` 修复。
- **验证**：隔离实例 + CDP 截图核对（截图见开发记录）；e2e 全绿；release v0.1.262。
- **同日补充（用户要求提高空间利用率）**：月历原本占上方整行、左右空出一大截——
  改为**两栏布局**：`#cal-left`（月历，flex 1 1 320px）+ `#cal-day`（当天待办，
  flex 1 1 280px），同一高度；窄面板自动换行（flex-wrap）；添加表单改为两行
  （时间/类型/重复/添加一行、文本输入独占一行——`order: 5` + `flex-basis:100%`）。
  release v0.1.263。
- **再补充**：两栏下左栏较窄，格子改回**正方形**（`aspect-ratio: 1/1`，随栏宽
  自适应）；**移除用户手动添加日程的表单**（时间/类型/文本/重复/添加——日程与
  任务统一由小助通过日历工具管理）。截图验证；release v0.1.264。

### 2026-10-19 第五十九轮：心跳语义重定义（用户定稿：只做用户嘱托的定时器）

- **用户决策**：心跳不再自主搭话/自主学习——**只有用户明确要求"某时间/间隔后
  做某事、盯着某任务"时才设**；支持**巡检循环**（醒来 → 检查 → 完成就收尾 /
  未完成再设下一次）；生成出错**不重试**；**免打扰与自主学习整套去掉**。
- **提示词**：`system.md` 心跳 4 条重写（用户授权定时器 / 巡检循环 / 间隔选择 /
  钟点提醒走日历）；删除 Do Not Disturb 与 Self-directed learning 两条；日历
  "Proactive check-ins" 改为"只做用户要求的巡检"；`heartbeat` 工具 docstring
  同步（删除"每轮收尾都要调用它"——那正是"每轮排心跳"的来源）。
- **调度**：`start_heartbeat` 不再自动排 30 分钟（无开机自主唤醒）；
  `_fire_heartbeat` 醒来即消费（next=0 立即落盘、**不再保底重排**、崩溃重启不
  重复触发）；错误路径删除 FALLBACK/学习保底排程；`_check_startup_heartbeat`
  补触发后立即落盘 next=0。
- **删除**：`DndGuardMiddleware`（agent 链/导入）、`set_do_not_disturb` 工具、
  `POST /dnd`、`session.set_dnd/in_dnd`、`state_dict().dnd`、`PLANNER_DND_*`、
  `_is_self_learning_generation`/`tts_mute`/`_generation_tool_names`/
  `_heartbeat_set_this_generation`/`PLANNER_LEARNING_HEARTBEAT_MINUTES`；前端
  右键/托盘"切换免打扰"、`tts_mute` 处理、`state.dnd`；日历到点提醒不再受时段限制。
- **回归**：删除 DND 用例 5 个；重写持久化用例（消费即落盘/不自动重排/启动不排程）；
  新增"唤醒提示带嘱托+巡检语义""模型不设心跳则不重排""启动不自动排程"；全量
  pytest **402 passed**；release v0.1.265。

### 与方案的偏差

1. 工作目录面板的"子目录"只列目录（不列文件）；文件靠 `@` 补全与 `read_file`。
2. 「最近目录」只记工作区/cwd 两条路径（同一列表）；区外条目点击 → 确认后整体切工作区。
3. 权限菜单未做"只读模式下的读仍放行"的额外提示（行为本身符合矩阵）。
4. **面板只保留大窗**：原方案的 ⤢ 小/大切换（350×520 ↔ 680×760）被移除，统一按大窗打开，
   并补上**边缘拖拽调整大小**（用户要求）——尺寸持久化，重置方式：`ui-settings.json` 删 `panel_w/panel_h`
   或调 `window.planner.setPanelSize(680, 760)`。
5. 原「第三轮候选」里的 `run_command` 与工具卡折叠组**已实现**（见上）；**变更视图 + 任意回退、
   授权规则管理也已实现**（第五轮，库落在 `data/changes.db` 而非方案里写的 `data/changes/` 目录）；
   **`ask_user` 也已实现**（第六轮）；图片粘贴、代码块主题仍未做，见「七、后续可选」。

第六十轮：心跳取消/更新 + 文档去 DND 残留（2026-10-19）

- **用户需求**：心跳只有"设"没有"取消"——用户改主意/任务提前完成时没法撤销。
- **实现**：`HeartbeatInput` 加 `cancel`（cancel=true 时允许不填间隔；否则仍
  要求 minutes/seconds 二选一）；工具分支 `cancel → session.cancel_heartbeat()`
  返回「已取消下一次定时唤醒」/「当前没有排定的定时唤醒」；`session.
  cancel_heartbeat()` 清 `_next_heartbeat_at`/`_heartbeat_note` → **立即落盘**
  （重启不复活）+ `_wake_event.set()`；**更新** = 直接再调一次 `heartbeat`
  （新设定覆盖旧的，无需先取消）。`system.md` 心跳 bullet 补「Modify/cancel」。
- **文档清残留**：README（工具表/能力表/环境变量表）、`docs/api-contract.md`
  （`dnd` 事件/state 字段/`POST /dnd`/DND 中间件说明）、`docs/tour.md`（工具计数/
  免打扰条目/中间件表/术语表/调度链）、`docs/README.md`、研究脚本
  `docs/research/locomo/playback.py`（dnd_enabled 属性已删会崩）全部清理；
  `android/` 按约定不动。
- **回归**：`test_agent.py` +2（`test_heartbeat_update_overwrites`、
  `test_heartbeat_cancel`——取消落盘/无排定提示/重启不复活）；全量 **404 passed**；
  e2e 全绿（PATCH_OK/PASTE_OK/CF_OK）；release v0.1.266。

第六十一轮：节点长度预算写进压缩指令（用户口径：设置 N ≈ 总字数 N）

- **用户疑问**：模型知不知道 `node_token_budget`？确认：**不知道**——预算此前
  只用于两处：`_get_summary_model` 的 `max_tokens=3×`、事后 `_apply_budget`
  软裁剪（summary 上限 2×预算字符、列表项 8×200）；压缩指令里完全没提，
  模型写多长全凭发挥。release 实测：今日 L1 1208~1568 字、最新 L2 summary
  1035 字 + 字段共 ~3541 字；全树 247 节点平均 516 字、25 个超 1000 字。
- **修复**：六份压缩指令（叶子普通/叶子分片/父层 × 中英）新增 `{budget_line}`
  占位；`SummarizationMiddleware._budget_line()` 把预算写成**整节点总字数**
  （中文口径 1:1：设置 1000 → 约 1000 字，summary ~400、其余字段 ~600；
  英文评测路径保持 token 语义 ≈3.5 字符），并注明"是上限不是目标、内容少就
  写短、不要凑数"。事后软裁剪 summary 上限同步从 2×预算收紧到 1×预算。
- **设置页**：节点预算 hint 改为"整节点总字数目标（默认 1000 ≈ 1000 字，
  会写进压缩指令；超出软裁剪）"。
- **回归**：`test_compression_contract.py` +1（`test_instruction_carries_length_budget`
  ——叶子/父层指令都带预算、中英双语）、`test_profile_nodes.py` 预算断言
  2000→1000；全量 **405 passed**；e2e 全绿；release v0.1.268。
  **旧节点不受影响**，只影响之后新压缩的节点。

第六十二轮：悬浮球快速输入浮层 + 气泡显示时长设置（2026-10-19）

- **用户需求**：不展开面板也能输入——全局快捷键（默认左 Alt+J）在悬浮球旁
  弹出小输入框（输入 + 语音 + 发送 + 待发送文件），Enter 发送；气泡显示
  时长加到设置页。
- **实现（Electron 前端）**：
  - `main.js`：新增 `quickWin`（380px 宽、透明无边框、置顶、skipTaskbar，
    启动即创建隐藏）；`globalShortcut` 注册 `uiSettings.quick_input_hotkey`
    （默认 `Alt+J`，设置页可改，保存即重新注册；被占用只记日志）；
    `positionQuickInput()` 贴球上方（放不下放下方），`quick-resize` 随内容
    增高且固定"贴球那侧"；失焦/Esc 收起；`broadcastMounted` 补发 quickWin。
  - `quick-input.html/css/js`：输入（Enter 发送、Shift+Enter 换行、IME 防
    误发）、语音按钮（mic.js → /asr 填回）、发送按钮；待发送文件 = 全局
    mounted（显示/移除/拖入挂载）；发送走 `apiFetch('/chat')` →
    `clearMounted()` → 收起；没有其他按钮（按需求裁剪）。
  - `preload.js`：`onQuickShow` / `onQuickHide` / `hideQuickInput` /
    `quickResize`。
- **气泡时长**：main.js `TOAST_MS` 常量（30 秒）→ `toastLifetimeMs()` 读
  `uiSettings.toast_seconds`（默认 30、3~600 秒）；设置页「交互」新增
  「气泡显示时长」「快速输入快捷键」两行（存 `ui-settings.json`，主进程
  本地配置，无需后端）。
- **mock**：新增触发词「快速输入测试」（放在「补丁块」之前——全历史扫描
  顺序，见 `llm.MockChatModel._generate` 注释）。
- **回归**：`cdp_panel_check.js` 第 27 步（QUICK_OK：挂载显示→Enter 发送→
  挂载清空→/history 落库 + UI 设置可读）；全量 pytest **405 passed**；
  e2e 全绿；release v0.1.269。

第六十三轮：悬浮球待处理提醒（审批/提问黄灯 + 球旁卡片就地处理，2026-10-19）

- **用户需求**：对话中需要用户审核/回答时（审批、ask_user），悬浮窗状态也要
  有提醒并能就地处理——冒气泡提醒、悬浮球中心变**黄色呼吸灯**；解决后恢复；
  展开面板或直接在气泡里选择/授权都行（尽量保证悬浮球形态交互完整）。
- **行为变更**：面板隐藏时**不再自动展开面板**（旧行为会强行打断用户）——
  改为球旁持久提醒卡 + 黄灯；面板展开时仍在面板里显示审批弹窗/提问卡。
- **实现（Electron 前端）**：
  - `main.js`：`pendingApprovals/pendingQuestions` 跟踪事件（request 存、
    resolved 删、`pendingSummary()`），`broadcastPending` 推给悬浮球与提醒卡；
    `pendingWin`（340px、按内容增高、`showInactive` 不抢焦点、失焦不收起、
    × 只收卡片黄灯保留）；`refreshPendingWindow()` 统一裁决（面板展开 /
    快速输入浮层打开时让位；收起面板后未决请求重新冒出）；后端离线清空。
  - `pending.html/css/js`：审批卡（工具/路径 + 允许一次/拒绝/展开面板 +
    超时倒计时）、提问卡（选项按钮 + 自由输入 + 展开）；动作直接
    `POST /approval` / `POST /question`，resolved 事件回来自动收起。
  - `bubble.js` + `bubble.css`：`#core.pending` 黄灯呼吸（1.4s
    `pending-breathe`，状态点 `#f59e0b`），`onPending`/`getPending` 驱动。
    顺手修掉悬浮球逾期徽标读旧字段 `s.plan`（应为 `s.agenda`）。
- **mock**：触发词「气泡提问」（放在最前）触发 ask_user；回归
  `cdp_panel_check.js` 第 28 步（PENDING_OK：收起面板→触发→球黄灯→
  卡片点选项→黄灯灭）。第 21 步改为「先等提问卡进隐藏 DOM → 手动展开」。
- **回归**：全量 pytest **405 passed**；e2e 全绿（QUICK_OK + PENDING_OK）；
  release v0.1.270。顺带：快速输入/提醒窗 `backgroundThrottling:false`
  （隐藏时定时器不降频）。

第六十四轮：悬浮球逾期红点只统计今天（2026-10-20）

- **用户反馈**：修好 s.agenda 字段后重启，悬浮球左上角出现红点 9+——原来
  是全量逾期（更早日期没做完的日程都算）；要求只统计今天。
- **实现**：session.state_dict() 的 genda.overdue_count 从全量逾期
  改为**今天**已过时间且未完成（list_overdue_calendar 结果按
  date == today 过滤）；日历补提醒扫描（_check_calendar_due）仍用全量
  逾期，不受影响。
- **回归**：	est_calendar.py +1（昨天/今天已过点/今天全天 → 红点只计
  今天的 1 条；全量逾期查询仍为 2）；全量 **406 passed**；e2e 全绿；
  release v0.1.271。

第六十五轮：悬浮球配色对齐桌面/托盘图标（2026-10-20）

- **用户反馈**：悬浮球配色与桌面图标不一致（原来是青绿渐变
  `#7ce8db → #2fb3a6 → #178f86`），要求改成图标同款。
- **图标取色**（`frontend/assets/bubble.ico` 256px / `bubble_32.png`）：
  球身天蓝 `#5fb8d6`（顶部浅蓝高光 ~`#8fd4e8`、边缘略深 `#3f9fbf`）、
  中心绿点 `#6fd993`、白色高光/描边。
- **改动**：`bubble.css` `#core` 渐变 `#8fd4e8→#5fb8d6→#3f9fbf`、白描边
  0.5、辉光 `rgba(95,184,214,.35)`；状态点在线色 `#34c78a→#6fd993`；
  `style.css` `#morph-ball`（展开/收起动画里的球）同步同款渐变与辉光、
  `#morph-status-dot` 在线色改 `#6fd993`——动画衔接不跳色。
- 纯 CSS；用户实例在运行，未跑 e2e；release v0.1.272。

第六十六轮：拖动面板不再改变悬浮球位置（2026-10-20，用户要求）

- **用户反馈**：把球拖到合适位置 → 放大 → 把面板拖到别处 → 收起后球跑到
  面板那里了；希望球的位置不受面板拖动影响。
- **原因**：`morphOut()` 里有"面板被拖过（`panelDragged`）→ 悬浮球重定位到
  面板中心、原地缩小"的分支（陪伴期设计）。
- **修复**：删除该分支——缩小目标一律 = 球当前 bounds；面板拖动只移动面板，
  收起动画从面板位置飞回球的位置（球留在用户上次放它的地方）。
- **回归**：CDP 探针（release + 隔离数据：记球坐标 → 展开 → `movePanel`
  偏移 → 收起 → 球坐标一致 `BALL_STAYS_OK`）；全量 e2e 全绿；release v0.1.273。

第六十七轮：快速输入浮层改到悬浮球下方（2026-10-20，用户要求）

- **用户反馈**：气泡 toast 一般出现在悬浮球上方，快速输入浮层也放上方会重叠；
  输入框放球**下方**更合适。
- **改动**：`positionQuickInput()` 改为优先球下方（视觉球下沿 +10），下方放不下
  （贴屏幕底）才翻到上方；待处理提醒卡（`positionPendingWindow`）同步同样
  规则（两者本来就互斥）。`quick-resize`/`pending-resize` 增高时仍固定贴球那侧，
  并**夹到工作区内**（贴底时向下生长不越界）。
- **回归**：e2e 全绿（QUICK_OK / PENDING_OK 仍过）；release v0.1.274。

第六十八轮：悬浮球/快速输入发送的用户消息立即出现在面板（2026-10-20）

- **用户反馈**：语音输入或快速输入浮层发的消息，展开面板看不到自己的消息，
  只有模型回答；等回答完再收起/展开才出现。
- **原因**：面板的本地气泡只在面板自己的提交分支里渲染（`addMessage`）；
  悬浮球语音 / 快速输入走 `/chat`（或语音直发），后端没有事件通知前端，
  隐藏的面板 DOM 里就没有这条消息。展开时若还在生成，`loadHistory(true)` 被
  跳过（防抹掉在途思考），所以只有等生成结束再重开才靠 `/history` 重建出来。
- **修复**：`session.enqueue_player_message` 入 buffer 后立即
  `push_event({"type": "user_message", "id", "text"})`；面板 `handleEvent`
  新增分支用 `addUserMessage(text, id)` 补气泡，按 msg id 去重（面板本地提交
  与事件几乎同时到达），事件路径 `forceScroll=false`（用户正在翻历史时不被
  拽到底）；`addMessage` 给用户消息加 `data-msg-id`，事件带 id → 撤销按钮可用。
- **回归**：`test_server.py` +1（`test_chat_pushes_user_message_event`）；
  e2e 第 27 步加断言（发送后面板 DOM 立即出现该用户消息，旧代码会
  `PANEL_NOT_RENDERED`）；全量 **407 passed**；release 直连 CDP 全绿
  （SCROLL_OK / QUICK_OK / PENDING_OK；用户实例占用 18772 时完整 e2e 跳过）。

第六十九轮：修复"桌面快捷方式打开不是 release 版本"（2026-10-20）

- **现象**：用户从桌面快捷方式打开小助，历史消息全是 e2e 测试对话
  （"这是端到端验证的测试对话""用户选择了「A」"…）。
- **根因**：不是快捷方式/版本问题——是之前一次 **e2e 面板检查失败**
  （滚动断言 YANKED）在脚本末尾 `throw`，**跳过了清理段** → 隔离数据的
  **mock 后端继续监听 18772**。用户打开 release 前端时 `checkBackend`
  发现 18772 在线，就直接连上这个测试后端（不拉起正式后端），于是看到
  测试对话。经查 `planner-release\data` 未被写入测试数据（用户 12:20-12:44
  的真实对话仍在 buffer_state），无需恢复。
- **处理**：杀掉遗留 mock 后端（18772 / 37878 等 3 个孤儿进程）；确认
  `PORT_FREE`；用户重新打开快捷方式即正常。
- **加固**：`e2e_release.ps1` 主体包 `try/finally`——失败也执行清理：
  杀 `$backend`、按 `--remote-debugging-port=9223` 匹配本脚本 Electron、
  杀 18772 残留监听（开跑前已断言端口为空）、清环境变量；**不再
  `Get-Process electron` 全杀**（会误伤其他 Electron 应用）。
- **回归**：e2e 全绿且结束后 `PORT_FREE`；release v0.1.275。

第七十轮：Markdown 表格 + 可点击链接（2026-10-20，用户要求）

- **用户反馈**：模型输出的 GFM 表格原样显示竖线，不能渲染；输出里的链接
  希望能直接点开。
- **表格**：`md.js` 增加行级表格解析——首行 `| a | b |` + 分隔行
  `|---|---|`（支持 `:---:` 对齐），连续 `|` 行作为表体；单元格走行内格式
  （代码/链接/粗体）；外层 `.md-table-wrap`（可横向滚动）+ `.md-table`
  （边框/表头浅灰底/斑马纹），面板 `style.css` 与气泡 `toast.css` 各一套；
  单元格 `white-space: normal`，不继承 bubble 的 pre-wrap。
- **链接**：`[label](url)` 与裸 `http(s)://` 自动转 `<a class="md-link">`；
  **协议白名单 http(s)/mailto**（`javascript:`/`data:`/`file:` 按普通文本）；
  md.js 在 document **capture** 阶段拦截点击（preventDefault + stopPropagation，
  防止 file:// 导航与 toast 的"点击展开面板"）→ `openExternal` → 主进程
  `open-external`（白名单 + `shell.openExternal`）。
- **回归**：`cdp_panel_check.js` 第 29 步（MD_OK：表格结构/两种链接/
  `javascript:` 不生成 `<a>` 且 openExternal 返回 `ok:false`）；e2e 全绿、
  结束 `PORT_FREE`；release v0.1.276。

第七十一轮：去掉每轮议程快照 + 逾期追问去重（2026-10-20，用户要求）

- **用户疑问**：小助说"每条我说话的上下文里都会带一份未来 7 天议程快照"，
  是不是每条消息都附带、有点浪费？→ **澄清**：并非每条新消息都注入
  （`AgendaSnapshotMiddleware` 按议程指纹去重，变化才注入一次），但注入后
  作为 HumanMessage **常驻上下文**，模型每轮都看得到。
- **用户决定**：不带了。删除 `AgendaSnapshotMiddleware`、`_agenda_snapshot_text`
  与指纹字段；`system.md` 第 2/3/5 条改为"**没有自动议程快照**，需要时用
  `list_schedule` 查（`start_date` + 可选 `end_date`，默认单日）"。
- **顺带修重复提醒**：用户实测 20:00 到点提醒响过之后，20:13 逾期扫描又问
  "要不要提醒你"。根因：`list_overdue_calendar` 不看 `reminded_at` →
  已到点提醒的项仍被当作"漏网"再问。现在加 `skip_reminded=True`
  （`_check_calendar_overdue` 使用）；红点统计仍用默认参数（提醒过但没勾掉
  仍算未完成）。
- **回归**：删除 2 个议程快照测试、新增
  `test_overdue_skip_reminded_for_nudge`；全量 **406 passed**；release 直连
  CDP 全量检查全绿（用户实例占用 18772 时完整 e2e 跳过）；release v0.1.277。

第七十二轮：Markdown 标题 / 列表 / 引用渲染（2026-10-20，用户反馈）

- **用户反馈**：## 一、kernel 就是一个函数 这种标题没渲染成标题（只支持
  加粗/代码/表格/链接）。
- **实现**：md.js 增加行级块解析——#~###### → <h1>~<h6>；
  -/*/+ 列表 → <ul>；1./1) → <ol>；> → <blockquote>；
  块内仍走行内格式（加粗/代码/链接）。面板 style.css 与气泡 	oast.css
  各补一套块级样式（标题字号、列表缩进、引用左边线；white-space: normal
  避免 bubble 的 pre-wrap 造成块内多余空行）。
- **回归**：e2e 第 29 步扩展（标题/无序列表/有序列表/引用断言）；
  e2e 全绿（Markdown: MD_OK）、结束 PORT_FREE；release v0.1.278。

第七十三轮：面板录音 → 悬浮球同步录音特效（2026-10-20，用户反馈）

- **用户反馈**：在放大面板点 🎤 开始录音，然后 Esc 收起面板——录音其实还在
  继续，但悬浮球看起来完全正常（没有紫点、没有雷达环），不像长按球时那样
  "看得出在听"。
- **实现**：面板 `togglePanelVoice` 开始/结束/失败时分别
  `window.planner.setPanelRecording(true/false)` → 主进程广播
  `panel-recording` → `bubble.js` 给球加/去 `.recording` 并启停雷达环。
  面板收起后录音继续进行，球保持特效；回到面板点 🎤 结束 → 球同步复原。
- **雷达环**：面板的麦克风流在另一个渲染进程，悬浮球拿不到 analyser →
  `startSoundRing` 增加**合成波形**回退（有 analyser 时仍用真实音量）；
  顺手加 `ringWanted` 守卫，避免 `getAnalyser()` 异步期间被 stop 后环又亮起来。
- **回归**：e2e 第 30 步（PANELVOICE_OK：IPC 开 → 球 `.recording`+环
  `.active`，关 → 全部复原；麦克风本身无法自动化，直接测状态通路）；
  e2e 全绿、结束 `PORT_FREE`；release v0.1.279。

第七十四轮：标题栏收起按钮铺满角落（2026-10-20，用户反馈）

- **用户反馈**：放大窗口右上角的收起按钮，只有中间一条小横条区域能点，
  角落（标题栏上下 10px）点不动。
- **原因**：`#titlebar` 上下各 10px padding，`#titlebar-btns` 只 stretch 到
  **内容区**高度（实测按钮 52×21，标题栏 42 高）——上下 padding 成了死区。
- **修复**：`#titlebar-btns { margin: -10px 0; }` 抵消 padding → 设置/收起
  按钮撑满整条标题栏（实测 52×38，标题栏 39）；右缘 5px 仍留给 resize 热区。
- **回归**：e2e 第 5 步新增断言（`#btn-fold` 高度 ≥ 标题栏高度-4、宽 ≥40）；
  e2e 全绿、结束 `PORT_FREE`；release v0.1.280。

第七十五轮：压缩节点总字数硬兜底（2026-10-20，用户设 700 仍太长）

- **用户反馈**：`node_token_budget` 已设 700（发布实例实测），节点仍是
  summary 320~701 + 字段 1.7k~4.3k，总计 2.4k~5k 字。指令里的"整节点
  合计约 N 字"被模型忽略。
- **修复**：`_apply_budget` 增加**总量硬兜底**——summary ≤ 60% 预算；
  整节点总字数 ≤ 1.1×预算；超出时按"最不关键→最关键"**整组牺牲**
  （context.env → commands → artifacts → lessons.patterns → next_steps →
  blockers → user → todo → decisions → lessons.errors → constraints 最后，
  前一组删空才动下一组），列表全空仍超才截 summary。六份压缩指令末尾
  再强调一句"总字数绝对不要超过上面写明的长度上限"。
- **回归**：`test_profile_nodes.py` +1（极端超量输入 → 总字数 ≤ 1.1×预算、
  summary ≤ 60% 且 ≥120）；全量 **407 passed**；e2e 全绿、结束
  `PORT_FREE`；release v0.1.281。旧节点不变，新压缩/父层合并生效。

第七十六轮：悬浮球气泡渲染表格/标题 + 长内容内部滑动（2026-10-20，用户要求）

- **用户需求**：悬浮球状态下弹出的气泡也要像展开面板一样渲染 Markdown
  表格/标题；气泡高度随内容增大但要有上限，超出后内容可滑动，且**不显示
  滚动条**（不好看）。
- **实现**：`toast.html` 本来就引 `md.js`（表格/标题/列表/链接同一套渲染），
  本轮补齐气泡侧样式并在 `#toast-text` 加 `max-height: 430px;
  overflow-y: auto; scrollbar-width: none`（`::-webkit-scrollbar` 隐藏）；
  表格容器 `.md-table-wrap` 的横向滚动条同样隐藏。窗口高度仍由
  `toast.js` 测量上报、主进程 `Math.min(480, h)` 兜底；内容超过 430 后
  在气泡内滚动（滚轮可用），不再被窗口高度硬切。
- **回归**：e2e 第 31 步（TOAST_OK：长内容气泡含表格+标题、`overflowY=auto`、
  `scrollHeight>clientHeight`、滚轮/scrollTop 生效、`scrollbar-width:none`、
  窗口高度 ≤500）；e2e 全绿、结束 `PORT_FREE`；release v0.1.282。

第七十七轮：设置页服务商不回显 → 保存时悄悄改回 DeepSeek（2026-10-20 真实事故）

- **用户反馈**：一直在用 OpenCode Go，突然报 DeepSeek 402 欠费；怀疑发版改了配置。
- **诊断**：发布只同步 `app/src` + `app/frontend`，从不写 `data/settings.json`。
  DeepSeek `/user/balance` 返回 **-0.01 元**（`is_available:false`），1 token
  请求也 402——是账户余额问题。但 provider 为何变成 deepseek：**设置页
  `loadSettings` 从不设置 `#llm_provider.value`**（select 默认第一项
  deepseek），用户每次点「保存」（气泡时长/节点预算/语音等）都会把
  `llm_provider` 写回 deepseek；DeepSeek 分服务商 Key 为空 → 回落共享
  `.env` 的 `LLM_API_KEY`（欠费的那把）。OpenCode Key 实测可用。
- **修复**：`loadSettings` 先 `$('#llm_provider').value = current.llm_provider`
  再 `applyProviderFields()`，保存不再改回。
- **回归**：e2e 第 32 步（POST `llm_provider=opencode` → 找到/打开设置窗口
  → **reload**（窗口是单例，第 23 步可能已开着）→ 断言选择框为 opencode
  → 还原 deepseek）；e2e 全绿（SETTINGS_PROVIDER_OK）、结束 `PORT_FREE`；
  release v0.1.283。

第七十八轮：提示词统一管理（2026-10-20，用户要求）

- **背景**：提示词原本散在四处——`prompts/*.md`（系统提示）、`session.py`
  （运行时 f-string）、`middleware.py`（压缩指令常量）、`tools.py`
  （工具 docstring）；没有清单/注册表，运行时文案与 `/history` 的中文括号
  前缀过滤耦合。
- **实现**：
  - `src/planner/prompt_loader.py`：注册表 `PROMPTS`（35 个逻辑名 → 文件）+
    `load()/render()`（缺占位符抛 `PromptRenderError`）；
  - `prompts/README.md`：完整清单（系统/运行时/压缩/工具 schema 四类 + 注入点
    + kind + 维护约定）；
  - 运行时模板搬到 `prompts/runtime/`：玩家包装、心跳唤醒、日历到点/逾期/
    编辑、附件 7 种、截屏/读图注入、单轮上限、ask_user 结果（共 21 份）；
  - 压缩指令搬到 `prompts/compress/`：中英 × 叶子/父层 + 两种范围行 + JSON
    骨架 + 长度预算（12 份，内容与原常量逐字一致）；
  - `_receive(..., injected=True, kind=...)` 写入 `metadata.injected/kind`；
    `/history` 用它隐藏内部消息（旧 buffer 仍按 `（`/`[当前` 前缀兜底），
    回合边界只认 真实用户消息 + `kind ∈ {heartbeat, calendar_due,
    calendar_overdue}`（图片/戳一戳/日历编辑不切块）；
  - 工具 docstring 仍在 `tools.py`（LangChain 由函数生成 schema，不能外置），
    在 README 里单独标注。
- **回归**：`tests/test_prompts.py`（注册表文件齐全/缺 key 报错/注入标记）+
  `test_server.py::test_history_hides_injected_by_metadata`；全量
  **412 passed**；e2e 全绿、结束 `PORT_FREE`；release v0.1.284。

第七十九轮：记忆类问题优先用 explore_memory_tree（2026-10-20，用户反馈）

- **用户反馈**：小助不太愿意用 `explore_memory_tree`，更倾向去翻文件/搜索；
  希望"内部/过往经历/记忆相关"的问题优先查记忆树，代码文件之类才用其他工具。
- **现状**：`system.md` 记忆 section 原本已有"回答涉及过去的问题先看 node、
  不够就 explore、没查就说我不记得不行"，但**没有相对 grep/read_file/
  web_search 的工具路由优先级**——模型有时会先去翻文件。
- **改动**（三处）：
  1. `system.md` 职责新增 **#6 Memory first**（英文，靠前位置）；
  2. 记忆 section 首条加**硬规则**（中文）：过去/经历/聊过或约定过的事/偏好/
     长期任务进展 → **第一选择 `explore_memory_tree`**，不要用
     grep/read_file/web_search 去猜；代码/文件/网页/实时信息才用那些；
     两者都相关时先查记忆（决定与原因）再看文件；
  3. `explore_memory_tree` docstring 首句强调"优先用本工具（不要用
     grep/read_file/网页搜索去猜）"。
- **回归**：`test_prompts.py` +1（断言路由规则在 system.md）；
  全量 **413 passed**；e2e 全绿、结束 `PORT_FREE`；release v0.1.285。

第八十轮：子代理 White（后台异步、独立标签页、结构化协议，2026-10-21）

- **用户决策**：MVP A——White 只能读写主工作区、**不能执行命令**；主助手控制创建/
  复用/关闭；用户只和主助手对话，但可直接停止 White；完成后通知主助手验收。
- **运行时**：`agent_context`（contextvar 标记 main/white）+ `white_agent.py`：
  - 后台线程、独立消息/状态/停止信号，不占主助手 `chat_lock`；
  - 生命周期 idle → running → idle（保留上下文）→ closed；运行中可排队新指令；
    `reuse=false` 清空上下文；主助手 `white_control(close)` 关闭；
  - 无记忆树：`WhiteContextMiddleware` 超预算时旧消息摘要 + 滑动窗口（无节点）；
  - 完成后 `runtime/white_finished` 注入主助手（等主助手空闲）+ `white_status/
    white_result` 事件；主助手验收后决定 close/续派。
- **协议**：`white_task(instruction, reuse)` / `white_control(action)`；
  White 结束必须调 `finish_task`（pydantic schema：status/summary/findings/
  assumptions/next_steps）；改动文件与用量由运行器自动补充（不让模型编 JSON）。
- **安全**：White 工具只保留 web + 工作区文件（排除 run_command/show_image/
  calendar/memory/heartbeat/ask_user/递归委派）；`ApprovalMiddleware` 对 White
  的越界路径**硬拒绝**（不走审批）；changes 按 agent 分离消费。
- **前端**：Agent 标题栏（运行转圈/待审批气泡/未读）；White 标签页复用同款
  正文/思考/工具行渲染 + 任务头 + 结果卡；用户可停止；`/agents`、`/history?
  agent=white`、`/white/stop`；设置页新增「子代理」。
- **回归**：`test_white.py`（6 例：工具白名单/无工作区/越界拒绝/结果复用关闭/
  排队）+ `test_server.py::test_agents_and_white_history` + e2e 第 33 步
  （WHITE_OK）；全量 **420 passed**；e2e 全绿、结束 `PORT_FREE`；release v0.1.286。

第八十一轮：White 实时/历史视图修复 + 常驻标签页 + Web 工具冷却（2026-10-22）

- **用户反馈**：点开 White 只看到任务说明，看不到 thinking/工具/执行流程；希望 White
  标签页即使没派任务也常驻（灰色呼吸灯），方便主动查看；网页抓取很慢。
- **视图修复**：
  - 实时事件原来只在 White 标签页激活时渲染 → 现在**始终写入** White 视图（隐藏时也累积）；
  - 切到正在运行的 White 时**保留实时现场**，不再重载 `/history`；历史加载带 token，晚到
    的响应不会覆盖更新的实时事件（这正是"只看到任务/最新一条"的竞态来源）；
  - 用 release 真实 `white.json`（25 条消息）实测：17 个块、14 条工具行、thinking、结果卡全部渲染。
- **常驻标签页**：`/agents` 在 `white_enabled` 且未创建 White 时返回 placeholder；前端
  始终显示 `小助 | White`，White 空闲为**灰色呼吸灯**（`.agent-dot.gray` + `agent-breathe`），
  点开有"还没有任务"空态；close 后回到灰灯而不是删 tab。
- **Web 工具慢**：日志显示 DeepSeek 官方 `/v1/tools/fetch` 返回 404 → 每次都先撞官方，
  再回退内置抓取；GitHub raw 内置抓取 20s 超时。内置抓取超时 **20s→12s**。聊天模型/API
  与主助手完全相同；慢在目标站/网络超时。（404 冷却的判断在下一轮修正。）
- **测试**：`test_toggle_mock` 改为幂等 mode（防 POST 重试把 toggle 翻两次的偶发红）；
  全量 **421 passed**；release 版直连 CDP 全量面板检查全绿（含 WHITE_OK；用户实例占用
  18772 时完整 e2e 跳过）；release v0.1.288。

第八十二轮：修正 Web 官方链路冷却（404 是单 URL 失败，2026-10-22）

- **用户纠正**：主 Agent 可以正常使用 DeepSeek 服务搜索——上一轮"Key 没权限"的判断
  是错的。
- **实测**（当前 DeepSeek Key）：`/v1/tools/search` **200**；`/v1/tools/fetch` 抓
  javaguide 页面 **200**；只有 `raw.githubusercontent.com` 返回 **404
  failed to fetch URL**。即 **404 是单 URL 抓取失败**，不是账号级不可用。
- **修复**：`_mark_ds_web_failure` 的冷却只对 **401/402/403**（鉴权/余额/权限）生效，
  不再因 404 把官方链路整体关 10 分钟（否则一次 raw GitHub 失败会误伤后续正常页面）。
  内置抓取超时保持 12s。
- **White 与主助手**：同一个 `web_search`/`fetch_web` 实现、同一 session、同一
  DeepSeek Key、同一聊天模型/API；之前的慢是 raw GitHub 单 URL 404 + 内置回退网络超时，
  不是工具配置差异。
- **测试**：`test_web_tools.py` +1（404 不冷却 / 402 冷却）；release v0.1.289。

第八十三轮：停止立即中断模型请求（主助手 + White，2026-10-22）

- **用户反馈**：点停止后仍要等模型/API 返回（甚至超时）才停；希望“点了就强行停”，
  已写完的产物/上下文保留，写不完的停在当前状态。
- **根因**：`request_stop`/`White.stop` 只置停止标志；`_run_agent`/`_run_once` 只在
  **收到下一个流事件**时检查。模型调用卡住（思考中/网络慢/API 不返回）时没有事件，
  只能等 `LLM_TIMEOUT`/重试或 White 总超时。
- **修复**：
  - `PlannerSession.build_llm()`：主助手、White（每次任务）各自构建**独立模型实例**；
  - `request_stop()`：置标志 + 杀 run_command + **关闭该模型 `root_client`**（后台线程，
    `/stop` 立即返回）；httpx 流因 socket 关闭立刻抛 `ReadError`，`_run_agent` 识别为
    “用户停止”而不是报错 toast；
  - `WhiteAgent.stop()`/`close()`：关闭**自己的** `root_client`；清空排队任务；
    `_run_once` 捕获停止异常后按 `blocked` 收尾；White 审批等待也检查 `stop_event`
    （不再拖到 120s 审批超时）；
  - White 总时限改用 `threading.Timer` 看门狗，到点关闭客户端（此前阻塞时超时也失效）；
  - 工具正在写文件时先写完；`values` 已产生的消息/工具结果保留在上下文，流式中断的
    半句只留在面板、不落 buffer（下次从干净上下文续接）。
- **测试**：新增 `test_stop_aborts_blocked_model_immediately` 与
  `test_white_stop_aborts_blocked_model_immediately`（假模型 `_stream` 永久阻塞，
  停止后 <1s 返回且 root_client 被关闭）；全量 **424 passed**；release v0.1.290。
  v0.1.291 追加：e2e mock 的「白色任务」触发词改为只认最后一条真实用户消息，避免 White
  完成后主助手收到通知又重复派活导致状态一直“运行中”；直连面板检查 33 步全绿。

第八十四轮：修复停止后“Connection error”（共享 httpx client 被误关，2026-10-22）

- **现象**：点停止后，后续请求报
  `调用模型失败（deepseek / deepseek-flash）：Connection error.`；日志 traceback
  真正原因是 `httpx: Cannot send a request, as the client has been closed.`
- **根因**：`langchain_openai` 在不传 `http_client` 时会让多个 `ChatOpenAI` 实例
  **复用同一个底层 httpx client**。v0.1.290 的“停止立即关 root_client”把旧模型
  client 关掉后，新模型、White 模型、压缩模型全都拿到**同一个已关闭 client**，
  之后每次请求都失败（OpenAI SDK 包成 APIConnectionError，看起来像网络问题）。
- **实测**：同一个 DeepSeek Key 直接 `/chat/completions` 返回 **200**（`deepseek-flash`
  也可用），排除网络/Key/模型问题。
- **修复**：`build_chat_model()` 每次创建**独立 `httpx.Client`**（timeout 按
  `LLM_TIMEOUT`、follow_redirects），作为 `http_client=` 传给 ChatOpenAI；
  停止只关闭当前模型自己的 client。
- **回归测试**：`test_build_chat_model_instances_have_independent_http_clients`
  断言两个模型 client 不同、关一个另一个仍可用；全量 **425 passed**；release v0.1.292。
