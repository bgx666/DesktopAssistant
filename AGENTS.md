# 小助 (planner) — 学习工作助手 Agent

## 项目身份

独立仓库（本仓库，不随 xiaob 主仓库提交）。Python 后端（LangChain create_agent + 中间件链）+ Electron 悬浮球前端。单机运行，SQLite 存储，无外部服务依赖。数据在 `data/`（git 忽略）。

## 命令

| 操作 | 命令 |
|------|------|
| 可编辑安装 | `python -m pip install -e .` |
| 启动后端 | `python -m planner` → http://127.0.0.1:18771 |
| Mock 模式 | `set PLANNER_MOCK_LLM=1` 后再启动 |
| 前端 | `cd frontend && npm start`（自动拉起后端） |
| 运行测试 | `python -m pytest tests/ -v`（pytest，mock LLM，tmp_path 隔离） |

LLM 配置走仓库根 `.env`（或共享 `.env`）：`LLM_API_KEY` 必填，默认 DeepSeek。

## 发版（开发/使用分离）

- **开发目录**（本仓库）：AI 改代码、跑测试；用户日常**不使用**开发目录
- **使用目录** `planner-release`（仓库旁，独立数据 `data/`、独立 venv，git 不追踪）：用户日常用
- 发版 = 开发完成后：`git commit`（工作区必须干净）→ 运行 `build.ps1`（或双击 `build.bat`）：
  自动 `vX.Y.Z` patch+1（可 `-Version` 手动）→ `robocopy /MIR` 复制 `src/`+`frontend/`（排除 node_modules）
  → venv 首次创建 + `pip install -e app`（可编辑，之后复制代码即生效）→ 首次 `npm install`
  → 首次迁移开发 `data/` 到 release → `git tag` + 生成 `start.bat`
- 用户日常：双击 `planner-release\start.bat`（`PLANNER_PYTHON`=venv、`PLANNER_DATA_ROOT`=release data、`PLANNER_PORT`=18772、
  `PLANNER_URL`、`PLANNER_USER_DATA` 均注入，main.js/config 已支持环境变量）
- 开发版（18771）与 release 版（18772）**可同时运行**：端口/URL/userData 全部隔离，互不抢占

### 隔离防回归（三层，缺一不可）
release 与开发版的隔离依赖 `start.bat` 注入的 5 个环境变量（`PLANNER_PYTHON`/`PLANNER_DATA_ROOT`/
`PLANNER_PORT`=18772/`PLANNER_URL`/`PLANNER_USER_DATA`）。**新增任何 release 运行状态**（日志、缓存、
userData、临时文件、端口）都必须经 `PLANNER_*` 环境变量注入 release，且满足：
1. **build.ps1 自检**：生成 `start.bat` 后校验变量完整且值正确，缺/错 → 拒绝发版（不许 git tag）
2. **e2e_release.ps1 静态断言**：解析 `start.bat` 全部 `set` 行与期望值逐一比对 + 禁止出现 18771
3. **e2e_release.ps1 动态断言**：CDP 实测前端 `apiBase`=18772、隔离 user-data 目录被创建、
   后端数据落在隔离目录
回归测试：故意改坏 `start.bat`（删变量/改值/带 18771）→ e2e 必须红。

## 关键约定（容易猜错/踩过坑的）

### ⚠️ 测试数据隔离（最高优先级）
- **pytest**：已用 `tmp_path` + `PLANNER_DATA_ROOT` 隔离，不写真实 `data/` ✓
- **任何端到端验证 / 手动联调脚本 / release 链路验证**：**必须**设置隔离数据目录 + mock，
  **绝不**让脚本直连或复用真实后端（18771 开发 / 18772 release），也**绝不允许**用真实数据目录
  启动验证实例（窗口里会加载真实对话，等同拿用户数据测试）：
  ```powershell
  $env:PLANNER_DATA_ROOT = "$env:TEMP\opencode\planner_e2e"   # 每次测试前删掉重建
  $env:PLANNER_MOCK_LLM = "1"
  Remove-Item $env:PLANNER_DATA_ROOT -Recurse -Force -ErrorAction SilentlyContinue
  ```
  - release 链路验证：`powershell -File tests\e2e_release.ps1`（内置隔离 + mock + 自动清理）
  - 真实 LLM 联调：`tests\live_check.py`（**永远 spawn 独立后端** 18773 + 隔离数据，不探测现有后端）
- 启动后端/Electron 时继承该环境变量；测试完杀进程。**真实用户对话、记忆树、任务只存在于 release `planner-release\data`（或迁移前的开发目录 `data/`），验证脚本一律不许触碰；用户日常使用只走 `start.bat`。**
- 深夜晚间（22:00-08:00）默认 DND 窗口会拦截心跳/自主生成——测试 nudge/心跳前先 `POST /dnd {"enabled": false}`。
- Windows 端口复用坑：残留旧后端进程可能占着 18771（含真实模式），测试前 `Get-Process python | Stop-Process -Force` 清理。

### 架构要点
- 记忆树：`memory/sqlite_memory_tree.py`（分层摘要树：叶子压缩 + 向上递归，阈值 8 / 4 合 1，节点含时间范围）；压缩输出 `MemoryNodeOutput`（summary + profile 画像四维度 + future_notes + meta.schema_version），pydantic 结构化 + 降级解析
- 节点字段演进：结构字段留列、内容字段进 JSON（profile/details/meta），加/删字段改 pydantic + 提示词即可，存储零迁移
- 对话 buffer：内存 → 每次生成结束持久化到 memory_tree.db 的 buffer_state（重启恢复）
- 全局状态用访问器：`game_state` 模式（session 状态机 `hidden → morphing_in → shown → morphing_out`）
- 退出用 `doQuit()`（process.exit，app.quit 会被窗口 close 拦截）
- 日志 `logging.getLogger("planner.*")` + RotatingFileHandler，不要 print

## 文档地图

- `README.md`：架构/运行/环境变量/字段演进约定
- `docs/api-contract.md`：前后端契约
