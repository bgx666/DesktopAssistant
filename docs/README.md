# planner 目录索引

> 想知道**代码怎么跑** → 看 [`tour.md`](tour.md)（代码地图 / 三条主线 / 中间件速查）
> 想知道**每个目录装什么、面试怎么讲** → 看本文件
> 要**准备面试/答追问** → 看 [`面试深挖提纲.md`](面试深挖提纲.md)（高频问答 + 会被问穿的题 + 5 天节奏）

## 一、目录总览

```
planner/
├── src/planner/          ★ 后端核心（Python，~8000 行）—— 面试主战场
├── frontend/             ★ 桌面端（Electron，~6600 行）
├── android/              ★ 安卓端（Kotlin，原生重写，非 Python 移植）
├── tests/                测试（29 个文件，352 个用例）
├── scripts/              开发脚本（导出 / 契约校验 / 启动）
├── docs/                 文档（本文件、tour、api-contract、images、marketing、research）
├── data/                 运行时数据（git 忽略，勿提交）
├── archive/              历史归档（git 忽略）
├── build.ps1 / build.bat ★ 发版入口 —— 必须在仓库根，不能挪
├── pyproject.toml        打包配置
├── AGENTS.md             开发约定（改代码前必读）
└── README.md             架构与运行说明
```

## 二、三个端的关系（面试最常问的第一个问题）

| 端 | 技术 | 定位 | 入口 |
|---|---|---|---|
| `src/planner/` | Python + LangChain `create_agent` + 自研中间件链 | **大脑**，唯一持有记忆树与 LLM 会话 | `python -m planner` → 18771 |
| `frontend/` | Electron（主进程 + 悬浮球 renderer） | 桌面外壳，悬浮球 + 面板 | `cd frontend && npm start` |
| `android/` | Kotlin + Jetpack Compose + Room | 移动端**独立重写**，非移植 | `cd android; powershell -File build.ps1` |

**关键叙事**：三端共享同一套「工具契约 + 提示词」，但实现完全独立。记忆树算法在 Python 端是
`memory/sqlite_memory_tree.py`，在安卓端是 `core/memory/` 下的 `Compressor` / `MemoryStore` /
`NodeRenderer`。安卓端由 `scripts/export_to_android.py` 从 Python 端**一次性导出**旧记忆与日历
（`planner-export.json`），之后各自独立演进。

→ 被问「怎么保证两端行为一致」时的诚实答法：不追求逐行一致，靠 `check_android_prompt.py`
做提示词↔注册表交叉校验（prompt 承诺了却没注册、注册了却没在 prompt 里说，都报红）。

## 三、`docs/` 内部

| 路径 | 内容 | 什么时候看 |
|---|---|---|
| `tour.md` | **现状导览**：代码地图 / 三条主线 / 中间件链速查 / 术语表 / 阅读路线 | 想读懂项目，先读这份 |
| `api-contract.md` | 前后端 JSON 契约 | 改接口前 |
| `代码Agent-实施计划.md` | 把 PC 端扩展为本地代码助手（审批制读写 + 检索）的第一期计划；含**记忆 schema（任务型）与上下文/压缩策略**（六、七节） | 做「代码 Agent」功能前 |
| `代码Agent-界面升级方案.md` | 第二轮：**工作目录（cwd）+ 权限模式 + 审批交互 + 代码可读性**的升级方案（对齐 Codex/DSH/OpenCode） | 做界面/权限联动前 |
| `images/` | README 引用的架构图（README 用相对路径引用，**不能挪**） | — |
| `marketing/` | GitHub 主页页 + 宣传视频（`promo/video/xiaozhu_intro.mp4`） | 投简历、要作品演示时 |
| `research/` | LoCoMo 记忆评测（`locomo/`，含 scored 输出与 `locomo_refined` 上游仓库） | 谈「效果怎么量化」时 |

## 四、`archive/` 与 `data/` 的区别（别删错）

- `data/` — **活的**，当前对话、日志、记忆树、任务、TTS 缓存都在这。删了＝丢用户数据。
- `archive/data_backup_20260812/` — 2026-08-12 数据迁移前的死快照，已无人引用。
- `planner-release/` — 在仓库**上一层**，不在本仓库内，是用户日常实际运行的目录。

## 五、面试深挖时的三条主线

按 `tour.md` 第 2 节展开，每条都能一路讲到实现：

1. **用户消息线** — 一条消息怎么走完并发锁 → 中间件链 → LLM → 工具调用 → 事件队列 → 前端
2. **自主线** — 心跳/日历调度线程怎么在用户不说话时自己触发（DND 窗口拦截）
3. **记忆线** — buffer 超阈值 → 分层摘要树压缩（叶子压缩 + 向上递归，阈值 8/4 合 1）→ 检索回填

**最硬的技术点**（最值得主动讲，因为面试官会追着问）：
- 分层摘要树 + buffer_state 持久化（重启恢复）
- 压缩过程中的「未来信息校正」
- 上下文复杂度稳定性：压缩后水位恒定，传统做法线性膨胀（`docs/images/context_growth_10k.png`）
- 节点字段演进策略：结构字段留列、内容字段进 JSON → 加字段零迁移

**最容易被问穿的地方，提前想好**：
- 为什么摘要树是「分层」而不是单层 RAG？
- 压缩掉的信息玩家再也找不回，怎么防丢？（→ `explore_memory_tree` 工具 + LoCoMo 评测数据）
- 三端重复实现的维护成本？