# 小助（planner）—— 学习工作助手 Agent

「小助」是驻留在桌面悬浮窗里的学习/工作助理 Agent。它不只是记录待办：你告诉它目标，它帮你**拆解成阶段和逐日计划**、**安排每天做什么**、**主动回访进度**并**动态调整排期**。所有对话会被压缩进**分层摘要记忆树**长期保存，它能翻出几周前你随口说的决定。

- **后端**：Python + LangChain（`create_agent` + 中间件链），LangGraph 化 ReAct 循环
- **前端**：Electron 悬浮球（桌面常驻小球，点击展开对话面板、点击其他位置自动收起；可拖拽、右键菜单、自动拉起后端）
- **记忆**：SQLite 分层摘要记忆树（叶子压缩 + 向上递归合并，节点携带时间范围）
- **全本地 AI 能力**：语音合成（Kokoro-82M-zh，onnx 推理无 torch）、语音识别（SenseVoice）、图片 OCR（RapidOCR），离线可用

## 功能特性

| 能力 | 说明 |
|---|---|
| 任务录入 | 对话（`create_task`）或 HTTP `POST /task` 结构化录入 |
| 任务拆解 | `break_down_task`：LLM 产出阶段 + 逐日计划，直接落库 |
| 每日计划 | 自动从任务拆解生成；前端可勾选完成，动态调整排期 |
| 主动回访 | 后端调度线程：LLM 自主 `heartbeat(minutes)` 决定下次醒来（clamp 10~720 分钟） |
| 分段说话 | `continue_speaking`：分点描述时每调用一次暂停片刻继续说下一点（用户可立即打断） |
| 打开时静默 | 启动 2 分钟宽限期 + 到期心跳顺延：不打开就说话 |
| 免打扰 | 默认 22:00-08:00 静默（玩家消息不受限），可前端开关 |
| 长期记忆 | 对话超阈值自动压缩成记忆树，`explore_memory_tree` 翻阅 |
| 语音播报 | 本地 TTS：自动朗读 / 消息喇叭按钮 / 103 种音色切换 + 开关 + 试听 |
| 语音输入 | 按住悬浮球或长按发送按钮说话，本地识别填入输入框 |
| 图片/文件 | 拖拽文件挂载（PDF/Word/文本解析 + OCR）、截屏、网页搜索 |

## 架构

```
后端（Python, http://127.0.0.1:18771）
  server.py        ThreadingHTTPServer + JSON 端点（/chat /dequeue /settings /tts/* …）
  session.py       PlannerSession：并发、调度线程、事件队列、免打扰
  agent.py         create_agent 构建（LangGraph，无 checkpointer）
  middleware.py    业务中间件链（免打扰/计划快照/玩家优先/压缩记忆树/调用上限）
  tools.py         工具注册：create_task / break_down_task / continue_speaking /
                   heartbeat / web_search / capture_screen …
  tts.py / asr.py / ocr.py   本地语音合成 / 识别 / 图片文字识别
  memory/          SQLite 分层摘要记忆树
  store/           任务/计划/待办库（SQLite）
前端（Electron）
  main.js          主进程：悬浮球窗口 + 面板窗口、托盘、HTTP 代理（渲染进程直连被
                   CORS/PNA 拦截，全部请求走主进程转发）
  renderer/        悬浮球（bubble）与面板（index）页面、设置窗口
存储
  data/            运行时数据（git 忽略）：planner.db / memory_tree.db / 日志
```

## 快速开始

```bash
# 后端（真实 LLM 模式：从仓库根 .env 或 LLM_API_KEY 环境变量读取，默认 DeepSeek）
python -m pip install -e .          # 首次安装
python -m planner                   # http://127.0.0.1:18771

# Mock 模式（脚本化假 LLM，不调真实 API，可完整演示建任务→拆解→勾选链路）
set PLANNER_MOCK_LLM=1
python -m planner

# 前端悬浮窗（自动拉起后端；后端已跑会直接复用）
cd frontend
npm install
npm start
```

## 日常使用与发版

开发与使用分离：开发在本仓库进行；日常使用走发版产物（独立 venv + 独立数据目录，记忆/任务与开发互不影响）。

1. 发版：`build.ps1`（或双击 `build.bat`）——自动 patch+1 版本号、复制代码到仓库旁 `planner-release\`、生成 `start.bat`、打 `git tag vX.Y.Z`
2. 使用：双击 `planner-release\start.bat`（独立 venv + 独立数据，端口 18772）
3. 回滚：`git checkout 旧tag` 后重跑 `build.ps1`（数据在 release `data\`，不受影响）
4. 开发版（18771）与 release 版（18772）可同时运行：端口/URL/userData 全隔离

Python 解释器通过 `PLANNER_PYTHON` 环境变量指定（默认取 PATH 中的 `python`）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLANNER_PORT` | `18771` | 监听端口 |
| `PLANNER_MOCK_LLM` | 关 | `=1` 脚本化假 LLM |
| `PLANNER_DATA_ROOT` | `data/` | 运行时数据目录 |
| `PLANNER_PYTHON` | `python` | 启动后端/发版用的 Python 解释器 |
| `XIAOB_SHARED_ENV` | 无 | 共享 .env 路径（跨项目复用 LLM 配置） |
| `PLANNER_HEARTBEAT_MIN_MINUTES` / `MAX` | `10` / `720` | LLM 自主心跳护栏（分钟） |
| `PLANNER_DND_START_HOUR` / `END_HOUR` | `22` / `8` | 默认免打扰窗口 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek | LLM 配置（也支持任意 OpenAI 兼容端点） |
| `PLANNER_TTS_ENGINE` | `local` | `cloud` 时用 DashScope 云合成 |

## 数据存储（git 忽略）

```
data/
  planner.db        任务/阶段/待办（WAL）
  memory_tree.db    记忆树节点 + 对话 buffer（WAL）
  logs/planner.log  运行日志（RotatingFileHandler）
```

**重置数据**：停掉进程后删除整个 `data/` 目录即可。

## 测试

```bash
python -m pytest tests -v
```

覆盖：记忆树存储、任务库 CRUD、agent 生成管线（mock 驱动建任务→拆解→勾选闭环）、
心跳护栏与启动静默、免打扰、buffer 重启恢复、HTTP 全端点集成、语音合成/识别、事件 drain。
全部测试 mock LLM + tmp_path 隔离，不调真实 API、不写真实数据。

## 技术要点 / 踩坑记录

- **DeepSeek 严格校验 tool_calls 配对**：不使用 LangGraph checkpointer（会导致 400），对话 buffer 持久化走 JSON 落 SQLite
- **Windows 透明窗口**：悬浮球拖动偶发左上角残影（系统级渲染问题，已尝试 CSS transform 方案副作用更大，保持高频跟随）
- **渲染进程网络**：Electron 37 起 file:// 页面直连 127.0.0.1 被 CORS/PNA 全拦，前端所有后端请求经主进程代理转发
- **Windows WinError 10053**：HTTP/1.0 规避 keep-alive 拆除竞态；前端轮询带重试
- **真实 LLM 联调脚本**：`tests/live_check.py`（独立端口 + 隔离数据，消耗少量 API 额度，可选）
