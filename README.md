# 小助（planner）—— 学习工作助手 Agent

「小助」是驻留在桌面悬浮窗里的学习/工作助理 Agent。它不只是记录待办：你告诉它目标，它帮你**拆解成阶段和逐日计划**、**安排每天做什么**、**主动回访进度**并**动态调整排期**。所有对话会被压缩进**分层摘要记忆树**长期保存，它能翻出几周前你随口说的决定。

- 后端：Python + LangChain（`create_agent` + 中间件链），深度借鉴同机项目「小b」的记忆树与「丫丫」的 LangGraph 化 ReAct 模式
- 前端：Electron 悬浮球（桌面常驻小球，点击弹出对话面板、点击其他位置自动收起；可拖拽、右键菜单、自动拉起后端）
- 记忆：独立实现的 SQLite 分层摘要记忆树（`src/planner/memory/`，与 xiaob 同构，不 import xiaob）

## 运行

```bash
# 后端（真实 LLM 模式：从仓库根 .env 或 LLM_API_KEY 环境变量读取，默认 DeepSeek）
cd planner
python -m pip install -e .          # 首次
python -m planner                   # http://127.0.0.1:18771

# Mock 模式（脚本化假 LLM，不调真实 API，可完整演示建任务→拆解→勾选链路）
set PLANNER_MOCK_LLM=1
python -m planner

# 前端悬浮窗（自动拉起后端；后端已跑会直接复用）
cd frontend
npm install
npm start
```

## 日常使用（release 版）

日常使用时**不要**在开发目录跑，用发版产物：

1. 双击仓库旁 `planner-release\start.bat`（独立 venv + 独立数据目录 `planner-release\data`，记忆/任务与开发互不影响）
2. 每次开发完成后发版：`build.bat`（或 `powershell -File build.ps1`），自动复制代码、生成 `start.bat`、打 `git tag vX.Y.Z`
3. 回滚 = `git checkout 旧tag` 后重跑 build.ps1（数据在 release `data\`，不受影响）
4. 开发版（18771）与 release 版（18772）**可同时运行**：端口/URL/userData 全隔离

## 核心能力

| 能力 | 说明 |
|---|---|
| 任务录入 | 对话（`create_task`）或 HTTP `POST /task` 结构化录入 |
| 任务拆解 | `break_down_task`：LLM 产出阶段 + 逐日计划 JSON，直接落库（从今天起排期） |
| 每日计划 | 今日计划自动从任务拆解生成；前端可勾选完成（`POST /plan/done`） |
| 主动回访 | 后端调度线程：LLM 自主 `heartbeat(minutes)` 决定下次醒来（clamp 10~720 分钟） |
| 分段说话 | `continue_speaking`：分点描述时每调用一次暂停片刻继续说下一点（可被用户立即打断） |
| 打开时静默 | 启动 2 分钟宽限期 + 到期心跳顺延：不打开就说话；逾期任务提醒照常 |
| 免打扰 | 默认 22:00-08:00 静默（玩家消息不受限），可 `set_do_not_disturb` 或前端开关 |
| 长期记忆 | 对话超阈值自动压缩成记忆树（叶子落树 + 向上递归压缩），`explore_memory_tree` 翻阅 |
| 语音播报 | Kokoro 本地 TTS（onnx 推理无 torch）：自动朗读 / 消息喇叭按钮 / 设置里 103 音色切换 + 开关 + 试听 |
| 语音输入 | SenseVoice 本地 ASR（按住悬浮球或长按发送按钮说话） |
| 图片/文件 | 拖拽文件挂载（PDF/Word/文本解析 + RapidOCR 图片识别）、截屏、网页搜索（web_search/fetch_web） |
| 状态持久化 | planner.db（任务/阶段/计划）+ memory_tree.db（记忆树 + buffer），重启恢复 |

## 目录结构

```
src/planner/
  config.py        环境配置（PLANNER_PORT/MOCK/DND 窗口/心跳护栏；.env 本地优先，其次共享 .env）
  server.py        ThreadingHTTPServer + 契约端点（HTTP/1.0，规避 Windows 10053）
  session.py       PlannerSession：并发（chat_lock/buffer_lock/_inbox）、调度线程、事件队列、DND
  agent.py         create_agent 构建（LangGraph，无 checkpointer——DeepSeek 400 坑）
  middleware.py    DndGuard / PlanSnapshot / PlayerPriority / TypingHint / Nudge /
                   HeartbeatTrack / Logging / Summarization（压缩+记忆树一体）+ ModelCallLimit
  tools.py         @tool 工厂：create_task / break_down_task / mark_plan_done /
                   continue_speaking / heartbeat / set_do_not_disturb / web_search /
                   fetch_web / capture_screen / explore_memory_tree …
  llm.py           build_chat_model（DeepSeek v4 extra_body）+ MockChatModel（脚本化假 LLM）
  tts.py           本地 Kokoro-82M-zh（misaki 音素 → onnx 推理）+ DashScope 云引擎可选
  asr.py / ocr.py / fileparse.py  SenseVoice 语音识别 / RapidOCR / PDF·Word 解析
  settings.py      设置持久化（settings.json，校验护栏；压缩/LLM/TTS 配置启动即生效）
  store/tasks_db.py  任务库（RLock 串行化，避免 close 竞态）
  memory/          独立移植的 SQLiteMemoryTree（压缩阈值 8 / 4 合 1，节点含时间范围）
  prompts/system.md  助理人格与工作准则
frontend/         Electron 悬浮球（bubble 窗口 + 面板窗口；renderer 轮询 /dequeue 800ms）
tests/            pytest（全部 mock LLM + tmp_path 隔离）
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLANNER_PORT` | `18771` | 监听端口 |
| `PLANNER_MOCK_LLM` | 关 | `=1` 脚本化假 LLM |
| `PLANNER_DATA_ROOT` | `planner/data` | 运行时数据目录 |
| `PLANNER_HEARTBEAT_MIN_MINUTES` / `MAX` | `10` / `720` | LLM 自主心跳护栏（分钟） |
| `PLANNER_DND_START_HOUR` / `END_HOUR` | `22` / `8` | 默认免打扰窗口 |
| `PLANNER_MORNING_HOUR` / `EVENING_HOUR` | `8` / `21` | 定时触发点 |
| `PLANNER_FALLBACK_MINUTES` | `60` | LLM 忘调 heartbeat 的兜底间隔 |
| `LLM_*` | DeepSeek | 复用共享 .env 的 LLM 配置 |

## 数据落盘（git 忽略）

```
data/
  planner.db        任务/阶段/待办/回访（WAL）
  memory_tree.db    记忆树节点 + buffer_state（WAL）
  assistant/YYYY-MM-DD.jsonl  对话日志
  logs/planner.log  运行日志（RotatingFileHandler）
```

**重置数据**：停掉进程后删除整个 `data/` 目录即可（任务、记忆、日志全部清空，重新开始）。

## 记忆树节点字段与演进约定

节点内容分两层：**结构字段留列，内容字段进 JSON**。加/删字段只改
`planner/middleware.py` 的 pydantic model 与压缩提示词，存储零迁移。

| 列 | 内容 |
|---|---|
| `summary` | 内容摘要（高频查询，独立列） |
| `details` | 叶子原文；有 future_notes 时为 `{"messages": [...], "future_notes": [...]}` |
| `profile` | 用户画像 JSON：`preferences / personality / habits / goals` |
| `meta` | `{"schema_version": 1, ...}` 与未来扩展字段 |

演进约定：
- **加字段**：ProfileInfo / MemoryNodeOutput 加 Field + 提示词加说明（`meta` 为自由扩展位）
- **删字段**：pydantic / 提示词去掉（旧数据中该字段保留但被忽略，`extra="ignore"`）
- **破坏性变更**：`meta.schema_version` + 1，读取按版本分流
- 读取一律容错：JSON 解析失败 → 默认值；前端/工具用 `.get()` 不直接索引

## 测试

```bash
cd planner
python -m pytest tests -v
```

覆盖：记忆树存储、任务库 CRUD、agent 生成管线（mock 驱动建任务→拆解→勾选闭环）、
heartbeat 护栏、DND 窗口、buffer 重启恢复、HTTP 全端点集成、事件 drain。

## 已知限制 / 踩坑记录

- **DeepSeek 严格校验 tool_calls 配对**：不使用 LangGraph checkpointer（yaya 实测 400），
  buffer 持久化走 messages_to_dict → JSON 落 SQLite；
- **close 竞态**（实测 2026-08-05）：连接被 `close()` 与在途语句交错时 `fetchone()` 返回
  None——任务库所有访问与 close 走同一把 RLock 串行化，会话 close 先等 `chat_lock` 释放；
- **Windows WinError 10053**：HTTP/1.0 规避 keep-alive 拆除竞态；前端轮询带重试；
- 真实 LLM 联调脚本：`tests/live_check.py`（消耗少量 API 额度，可选）。
