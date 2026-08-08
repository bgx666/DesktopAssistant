# 小助（planner）—— 学习工作助手 Agent

「小助」是驻留在桌面悬浮窗里的学习/工作助理 Agent。它不只是记录待办：你告诉它目标，它帮你**拆解成阶段和逐日计划**、**安排每天做什么**、**主动回访进度**并**动态调整排期**。所有对话会被压缩进**分层摘要记忆树**长期保存，它能翻出几周前你随口说的决定。

- **后端**：Python + LangChain（`create_agent` + 中间件链），LangGraph 化 ReAct 循环
- **前端**：Electron 悬浮球（桌面常驻小球，点击展开对话面板、点击其他位置自动收起；可拖拽、右键菜单、自动拉起后端）
- **记忆**：SQLite 分层摘要记忆树（叶子压缩 + 向上递归合并，节点携带时间范围）
- **全本地 AI 能力**：语音合成（Kokoro-82M-zh，onnx 推理无 torch）、语音识别（SenseVoice）、图片 OCR（RapidOCR），离线可用

## 无限上下文

大模型的上下文窗口是有限的，但小助的记忆**不是**——它用「压缩 + 分层记忆树」把漫长的对话历史折叠起来：

- 对话积累到阈值（默认 60 条）时，自动把**最早的对话压缩成摘要节点**存入记忆树，上下文窗口回收（只保留最近 20 条），**生成成本恒定**；
- 摘要节点继续向上递归合并（8 合 1），形成**越老越精炼的分层树**——近期的细节、久远的关键决策都还在；
- 每次压缩后，上下文**不是清零**，而是带着「摘要 + 用户画像 + 未来待办」继续——它记得你是谁、在做什么，只是把废话折叠了；
- 需要考古时，`explore_memory_tree` 工具可以按需展开任意节点，翻出几个月前的原话。

一句话：**窗口有界，记忆无限。** 你和它聊到第 1 轮还是第 10,000 轮，它的"脑内工作台"始终清爽，而"长期记忆"越积越厚：

![上下文随对话轮数增长（压缩前峰值 vs 记忆保留）](docs/images/context_growth.png)

对比没有记忆树的长对话——传统方案的上下文随轮数线性暴涨，很快撞上模型窗口上限（信息被截断/遗忘）；小助的上下文始终保持在一个稳定水位，10,000 轮也不慌：

![长对话对比：传统上下文膨胀 vs 小助恒定水位](docs/images/context_growth_10k.png)

### 压缩不是"扔进碎纸机"，而是"知情回顾"

普通的对话压缩是**孤立快照**：把一段旧对话单独丢给模型做摘要，模型只看到那段时间发生了什么，**看不到后来**——用户后来否定的决定、中途改变的计划、最终完成的结果，摘要一概不知，记忆就成了"过期的快照"。

小助的压缩不一样：压缩时，被压缩的片段**原样包裹在指令中，追加到完整原始上下文的末尾**——模型眼前是整个对话 + 待压缩片段的全文。这意味着它能看到**被压缩区域之后、相对它而言属于"未来"的信息**：

- 用户后来改口说"那个方案算了"→ 摘要不会记成"方案已确定"；
- 任务后来完成了、目标后来变了 → 摘要带着最终结果写，而不是停在中途；
- 对话里的伏笔后来揭晓了 → 摘要记得住因果，而不是只记铺垫。

摘要因此具有**事后校正能力**：每一层记忆都是"知道结局的回顾"，而不是"截稿时的快照"。这也正是记忆树层层向上压缩后依然可信的原因——越往上越精炼，但每一条精炼都经过了后续对话的检验。

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

## 使用的开源模型

小助的语音/视觉能力全部基于开源模型**本地推理**（无云端依赖、数据不出本机），首次使用时自动下载到用户缓存目录（`~/.cache/planner_tts`、`~/.cache/planner_asr`），之后离线可用：

| 模型 | 用途 | 说明 |
|---|---|---|
| **Kokoro-82M-v1.1-zh**（[onnx-community/Kokoro-82M-v1.1-zh-ONNX](https://huggingface.co/onnx-community/Kokoro-82M-v1.1-zh-ONNX)） | 语音合成（TTS） | 82M 参数、纯 ONNX 推理（无 torch），支持 100+ 中英文音色；配合 [misaki](https://github.com/hexgrad/misaki) 中文音素管线 |
| **SenseVoiceSmall**（[FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)） | 语音识别（ASR） | 阿里开源的多语言语音识别模型，ONNX 版经 [funasr-onnx](https://github.com/altescy/funasr-onnx) 加载 |
| **RapidOCR**（[RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR)） | 图片文字识别（OCR） | PaddleOCR 模型的 ONNX 移植版（约 15MB），用于截图/图片中的文字识别 |

各模型的许可证以其项目仓库为准（均为开源协议，商用前请自行核对）。语音输入/识别/OCR 全程本地处理，只有对话内容会发送到你配置的 LLM API。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 建议 3.11/3.12（3.13 也可，部分依赖版本受限） |
| Node.js | ≥ 18 | 前端 Electron（开发时用；日常使用不需要） |
| 操作系统 | Windows | 已适配 Windows（透明悬浮窗/托盘）；其他平台未验证 |

LLM 需要一个 **OpenAI 兼容的 API**（默认 DeepSeek，也可换任意端点）。语音合成/识别/OCR 全部本地推理，无需额外服务。

## 安装与配置

### 1. 后端

```bash
# 克隆仓库后，在仓库根目录：
python -m pip install -e .          # 安装依赖（httpx / langchain / onnxruntime / kokoro 等）
```

### 2. 配置 LLM API

在仓库根目录创建 `.env` 文件（已加入 .gitignore，不会提交）：

```bash
# .env
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx   # 必填
LLM_BASE_URL=https://api.deepseek.com # 可选，OpenAI 兼容端点
LLM_MODEL=deepseek-v4-flash           # 可选
```

不创建 `.env` 也可以，直接在系统环境变量里设置 `LLM_API_KEY` 等。

### 3. 启动

**后端**：

```bash
python -m planner          # 监听 http://127.0.0.1:18771
```

不想消耗 API 额度、只想看看长什么样？用 Mock 模式（脚本化假 LLM，可完整演示建任务→拆解→勾选闭环）：

```bash
set PLANNER_MOCK_LLM=1
python -m planner
```

**前端悬浮窗**（自动拉起后端；后端已跑会直接复用）：

```bash
cd frontend
npm install
npm start
```

第一次启动，小助会去本地缓存目录下载 Kokoro 语音模型（约 120MB，仅一次），随后全部离线可用。

### 4. 验证

- 打开浏览器访问 `http://127.0.0.1:18771/init`，返回 `{"ok": true, "mode": "llm"}` 即后端就绪
- 悬浮球单击 = 看它最近说了什么；长按 = 语音输入；拖到文件上 = 挂载文件后语音/文字一起发送

## 常用配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLANNER_PORT` | `18771` | 后端监听端口 |
| `PLANNER_MOCK_LLM` | 关 | `=1` 脚本化假 LLM（演示/测试） |
| `PLANNER_DATA_ROOT` | `data/` | 运行时数据目录（任务库/记忆树/日志） |
| `PLANNER_PYTHON` | `python` | 前端拉起后端用的 Python 解释器 |
| `XIAOB_SHARED_ENV` | 无 | 可选：共享 .env 路径（多项目复用同一份 LLM 配置） |
| `PLANNER_HEARTBEAT_MIN_MINUTES` / `MAX` | `10` / `720` | LLM 自主心跳间隔护栏（分钟） |
| `PLANNER_DND_START_HOUR` / `END_HOUR` | `22` / `8` | 默认免打扰窗口 |
| `PLANNER_TTS_ENGINE` | `local` | `cloud` 时改用 DashScope 云合成 |

## 数据存储

```
data/
  planner.db        任务/阶段/待办（WAL）
  memory_tree.db    记忆树节点 + 对话 buffer（WAL）
  logs/planner.log  运行日志（RotatingFileHandler）
```

**重置数据**：停掉进程后删除整个 `data/` 目录即可（任务、记忆、日志全部清空，重新开始）。

## 测试

```bash
python -m pytest tests -v
```

覆盖：记忆树存储、任务库 CRUD、agent 生成管线（mock 驱动建任务→拆解→勾选闭环）、心跳护栏与启动静默、免打扰、buffer 重启恢复、HTTP 全端点集成、语音合成/识别、事件 drain。全部测试 mock LLM + tmp_path 隔离，不调真实 API、不写真实数据。

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
```

## 技术要点 / 踩坑记录

- **DeepSeek 严格校验 tool_calls 配对**：不使用 LangGraph checkpointer（会导致 400），对话 buffer 持久化走 JSON 落 SQLite
- **Windows 透明窗口**：悬浮球拖动偶发左上角残影（系统级渲染问题，已尝试 CSS transform 方案副作用更大，保持高频跟随）
- **渲染进程网络**：Electron 37 起 file:// 页面直连 127.0.0.1 被 CORS/PNA 全拦，前端所有后端请求经主进程代理转发
- **Windows WinError 10053**：HTTP/1.0 规避 keep-alive 拆除竞态；前端轮询带重试
- **真实 LLM 联调脚本**：`tests/live_check.py`（独立端口 + 隔离数据，消耗少量 API 额度，可选）
