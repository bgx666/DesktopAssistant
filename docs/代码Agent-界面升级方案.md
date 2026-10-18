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

