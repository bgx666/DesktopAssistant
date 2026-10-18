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

测试解释器用 **conda base**（`D:\Miniconda3\python.exe`，Python 3.13，pytest/langchain/planner 齐备）。仓库里的 `.venv` 是 uv 建的 Python 3.10，**没有 pytest 也没有 pip**，`uv run` 还会因 `onnxruntime-directml` 没有 cp310 wheel 而解不开依赖；release 的 `venv` 只装运行依赖（由 conda base 派生，`pyvenv.cfg: home=D:\Miniconda3`），同样没有 pytest —— 这两个都别用来跑测试。

LLM 配置走仓库根 `.env`（或共享 `.env`）：`LLM_API_KEY` 作兜底。
**模型解析链**（`llm.py::resolve_llm_config`）：`settings.json` 的 `llm_provider`（`deepseek` 官方 / `opencode`=OpenCode Go）
→ 分服务商字段 `llm_api_key_<provider>` / `llm_model_<provider>` / `llm_base_url_<provider>`（留空用预设端点/模型）
> 旧字段 `llm_api_key` / `llm_model`（**仅 DeepSeek 兜底**，读取时自动迁移）> env `LLM_API_KEY` / `PLANNER_LLM_MODEL`。
默认 DeepSeek 官方 `deepseek-flash`（V4.1-Flash，多模态）；切服务商互不覆盖 Key 与模型。共享 `.env` 的 `LLM_MODEL`
是 xiaob 游戏的配置，**不影响 planner**。视觉注入只在模型名判定为多模态（`deepseek-flash` / vision / gpt / claude / gemini 系列等）且非 mock 时启用；**图片块常驻 buffer 整个会话**（每张最多 384 token，每轮模型调用重发计费），`/history` 与压缩摘要只取文本块，不暴露 base64。
- **面板状态条**（输入框下方）：目录 / 权限模式 / 模型 / 思考强度四个 chip；模型菜单按服务商分组
  （`GET /llm/providers` 预设，`GET /llm/models?provider=` 有 key 时拉实时清单），切换即 `POST /settings`。

## 发版（开发/使用分离）

- **开发目录**（本仓库）：AI 改代码、跑测试；用户日常**不使用**开发目录
- **使用目录** `planner-release`（仓库旁，独立数据 `data/`、独立 venv，git 不追踪）：用户日常用
- **发版有两条路径，先确认当前在用哪条**：
  - **`build.ps1`（文档路径）**：`git commit`（工作区必须干净，脏则直接 `exit 1`）→ 运行 `build.ps1`
    （或双击 `build.bat`）：自动 `vX.Y.Z` patch+1（可 `-Version` 手动）→ `robocopy /MIR` 复制
    `src/`+`frontend/`（排除 node_modules）+ `pyproject.toml` → venv 首次创建 + `pip install -e app`
    （可编辑，之后复制代码即生效）→ 首次 `npm install` → 首次迁移开发 `data/` 到 release
    → 写 `VERSION` + **重新生成 `start.bat`/`start.vbs`** → 自检 → `git tag`
  - **手工路径（2026-08-16 起实际在用）**：提交 `cfcba81` 记录「`-SkipPip` 在当前 PowerShell 环境下不稳定，
    release 改用手动方式完成」。即手工复制代码 + 手工改 `VERSION`，**不重生成 launcher、不打 tag**。
    现状特征（可核对）：`start.bat`/`start.vbs` 停在 8/16；git tag 停在 `v0.1.175`；`VERSION` 手工递增到
    `v0.1.179`（**176–179 无 tag**，所以版本号不能当发版凭证看）。
- ⚠️ **release 的私有配置不许写在 launcher 里**：`build.ps1` 会按模板重新生成 `start.bat`/`start.vbs`，
  模板只含 5 个隔离变量 + `XIAOB_SHARED_ENV`，写在里面的东西会被静默抹掉（自检只查"该有的在不在"）。
  语音等私有配置放**共享 `.env`**（`XIAOB_SHARED_ENV` 指向的文件，`config.py` 会加载）——另一好处是
  `build.ps1` 用 ascii 编码写 `.bat`，中文音色名放 launcher 会变成 `??`。
  `build.ps1` 现已加防回归：重新生成前若发现会丢掉手工加的 `PLANNER_*` 变量则拒绝发版。
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

### ⚠️ 清理进程纪律（2026-10-18 误杀事故）
- **禁止无差别杀进程**：`Stop-Process` 按 `planner` 关键字扫杀会把**用户正在使用的
  release 实例**一起杀掉（实际事故：用户在让 release 联网干活，AI 清理调试实例时
  连同误杀，强杀导致最后一轮对话没落盘）。正确做法：
  1. 杀之前先查 `Get-NetTCPConnection -LocalPort 18772`——**端口在监听 = 用户实例
     在跑**，必须先问用户（用户同意后再动，或干脆等用户自己退）；
  2. 调试实例**启动时记下 PID**，清理时只杀自己记下的 PID（含该 electron 进程树）；
  3. e2e 脚本本身已内置"端口被占就拒绝"保护，**不要**在跑 e2e 前手动杀进程绕过它。

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
- 记忆树：`memory/sqlite_memory_tree.py`（分层摘要树：叶子压缩 + 向上递归，阈值 8 / 4 合 1，节点含时间范围）；压缩输出 `MemoryNodeOutput`（**任务型 schema v2**：summary + task/decisions/lessons/context/user，落库 `meta.fields`；旧画像节点兼容渲染），pydantic 结构化 + 降级解析
- 节点字段演进：结构字段留列、内容字段进 JSON（profile/details/meta），加/删字段改 pydantic + 提示词即可，存储零迁移
- **压缩输入边界**（计划 7.2）：叶子 = 带 `[已压缩 · 不需要压缩]` 标记的节点 + 待压原文（保留尾部不进）；节点压缩 = 目标去标记（从树渲染）+ 指令点名；**叶子触发只看 token 预算**（`compress_token_budget`，默认 10 万；条数不再参与），节点向上合并仍按 阈值 8 / 4 合 1；**四份压缩指令（叶子/父层 × 中英）都内置空字段规则**：没有相关内容的字段**可以直接省略**（省略=空），不许写「无/暂无/N/A」占位词、不许为填充硬凑——渲染层 `_render_fields_lines` 本就跳过空字段
- **压缩空转修复（2026-10-18，用户实测 raw 涨到 18 万不降）**：三个坑叠加——① **摘要模型必须显式关思考**（`_get_summary_model` 传 `thinking_mode="disabled"`）：跟随用户设置时 OpenCode 上 thinking=enabled，推理 token 吃光 `max_tokens` → 返回空内容 → 「叶子摘要为空，跳过」，每次触发每次空转；② **压缩调用有分片兜底**（`_split_batch_by_tokens`，≤3 万 token/片、最多 12 片/轮；只在**安全边界**切——不以 tool 开头、不以带 tool_calls 的 ai 结尾，批次作为消息发送、配对断裂直接 400；片失败即停手防序号错位）——但**默认整批优先**（`_compress_batch`：批次 ≤ `_ONE_SHOT_MAX_TOKENS`(30 万) 先整批一次调用 = **一个节点**；超限或整批失败才回退分片。用户反馈"一次压缩冒出好几个 L0 节点、感觉不是 15 万压一次"后从"无条件下分片"改为整批优先，2026-10-18）；③ OpenCode 端点不支持 `with_structured_output(json_mode)`：首次失败在 session 打 `_compress_no_structured` 标，本进程内直接走降级链（省每次注定失败的 ~15s）。**第二波修复（同日）**：④ `max_tokens` 1.5×→**3×**（任务型 JSON 6 字段比纯文本长，1.5× 被截断 → 解析失败）；⑤ 降级链第二跳**也先解析 JSON**、坏了用 `clean_wrapped_summary` **抠出 summary 字符串**（曾漏这两步：整段 JSON 外壳被存成摘要，节点/节点消息全是 `{"summary": ...}`）；⑥ **渲染层剥壳**（`_render_node_text`/`_node_full_text` 过 `clean_wrapped_summary`，历史坏数据展示时自动干净）+ **启动自愈**（`_load_buffer_state` → `_heal_wrapped_node_summaries` 原地剥缓冲里的坏节点消息）；⑦ **压缩指示灯**：压缩开始/结束推 `compress_state` 事件 + `state_dict().compressing`，前端标题栏「压缩记忆中…」+ 琥珀色呼吸灯（`.dot.compressing`）。回归：`test_compression_contract.py` 共 20 例（整批单节点/失败回退分片/安全边界/thinking=disabled/降级标记/端到端单节点替换/剥壳/自愈/state 标志）
- **代码工作区（审批制）**：`workspace_root` 空 = 关闭；`workspace_cwd` = 当前工作目录（**相对路径基准 + 提示词文件树焦点 + 命令执行 cwd**）；`permission_mode`（`ask`/`auto`/`read_only`，Auto = **cwd 内写免审批**、外圈仍审批）；写工具与 `run_command` 只在 `tools.build_code_tools` 注册（另有 `get_cwd` 实时返回 工作目录/工作区根/权限模式）；**`apply_patch`（Codex V4A 风格多文件补丁）**：一次调用可 Add/Update/Delete/Move 多个文件，`@@ 锚点` + 上下文行定位（行级 trim 宽松兜底在 `_match_line_block`），**原子生效**（解析/定位失败全部不改）；审批按补丁涉及的全部文件（`workspace.tool_targets` 用正则抽路径），每文件单独留底可回退（`session.change_diff` 对多行结果拼接多段 diff）；**`read_file` 按行读**（`offset` 行起始 / `limit` 行数，输出带行号 `42| …`、续读提示、单行 >2000 字符截断、总输出 60K 预算）——先 `grep` 定位再精读片段，不要整文件硬读；**注意缓存代价**：system prompt 内嵌 `{workspace}/{cwd}/{permission_mode}` 与 cwd 文件树/AGENTS.md，**切 cwd/工作区/权限模式都会重建 prompt+agent → 该次请求的前缀缓存从 0 失效（一次全价），之后恢复命中**（实测命中率可达 99%+，别频繁切换）；`ApprovalMiddleware` 黑名单硬拒 → 权限矩阵判定 → 未命中规则则挂起审批（超时默认拒绝；**面板隐藏时主进程自动展开**）；**命令逐条审批**（Auto 不豁免）+ 命令硬黑名单（递归删/格式化/下载即执行/强推远端…）；「记住并允许」按 scope 落 `data/permissions.json`（文件 / 目录 `src/**` / 工作区 `**` / 命令 `cmd`+`cmd_prefix` 如 `pytest *`，设置页可逐条删除/清空，`POST /permissions/delete|clear`，删除立即生效）；`data/tool_results/` 是助手自身卸载目录（读免审批）；`workspace_recent` 记最近目录（≤8）；停止/关窗会杀执行中的命令进程树（`session.kill_run_proc`）
- **变更留底/回退**：write/edit/move/delete 执行前 `tools._record_change` 记入 `data/changes.db`（`changes.py`：`ChangesDb`/`snapshot_before`/`rollback_to`/`diff_text`；单侧 >2M 字符或二进制 → `no_snapshot` 不可回退）；`rollback_to(id)` = **回退到该条之前**（id≥ 且未回退的条目新→旧恢复；`before_missing` = 删除；路径不在当前工作区/黑名单 → 跳过并在 `failed` 报原因）；**工具结果「修改前后」diff**：`_record_change` 把 change id 入队 `session._pending_changes`，`_emit_tool_events` 按工具名顺序消费 → `tool_result` 事件带 `diff`（unified diff 文本；move 给说明句；>200k 字符或 no_snapshot 给替代说明），同时写进消息 `additional_kwargs.change_ids`（`/history` 重建也带 diff——白名单转换不会发回模型）；面板「变更」tab（`GET /changes`、`POST /changes/rollback|clear`，回退后推 `changes_update`），设置页可管理授权规则
- **结构化提问（ask_user）**：工具阻塞在 `session.ask_user`（复用审批挂起模式：`pending_questions` + Event 0.5s 分片等待，停止/关窗立即唤醒，超时=跳过并让模型自行判断）；事件 `question_request`/`question_resolved`（answer/selected/status）驱动**对话区内联提问卡**（app.js `showQuestion` 动态插入 `#messages` 尾部：选项按钮 + 自由输入 + 倒计时；**禁止再改回全屏遮罩弹窗**——用户反馈会挡住对话内容没法参考上文；回答/超时后 `settleQuestionCard` 收成一行状态），前端 `POST /question {id, answer?, selected?}`；面板隐藏时 `main.js` 自动展开（与审批同分支）；mock 模型用消息里带「考我」触发该工具（dev/e2e 用）
- **面板语音输入（🎤）**：点击开始录音、再点一下结束 → `/asr` 识别 → **只填进输入框**（不自动发送）；开始录音会先关掉连续对话模式并打断在播语音（避免抢麦）；**发送按钮不再长按录音**（旧长按逻辑 2026-10-17 移除）；悬浮球长按录音不变（bubble.js）
- **语音播报开关（tts_enabled）三处闸门 + 启动竞态**：① 后端 `session.tts._enabled`（`_create_tts`/`update_settings` 应用，`_maybe_speak`/`synthesize_async` 据此不发 audio 事件）；② 面板/悬浮球 `voiceMode.ttsEnabled`（普通模式流式 TTS 的真正发声闸门，queueSpeak 拦截）；③ 设置页保存后**必须 `window.planner.settingsSaved()`**（主进程 `settings-saved` → 重拉 /settings → 广播 bubble+panel）——曾漏这步：运行中关掉播报后 voiceMode 的 ttsEnabled 仍 true，回复继续被流式朗读，直到重启才停；`onSettings` 收到 false 时 `stopSpeakingNow()` **立即停嘴**（含正在播）；`voiceMode.ttsEnabled()` 是给 e2e 断言用的访问器。**启动竞态**（2026-10-18 修）：start.bat 同时拉起前后端，面板可能在**后端就绪前加载**——voiceMode attach 的一次性 `/settings` 拉取静默失败 → ttsEnabled 停在默认 true（实测："重启后仍自动播报"的真凶）；修复：主进程 `checkBackend` 成功（后端上线）时 `fetchSettingsAndBroadcast()` 主动补发一次全量设置（`settings-saved` 处理器复用同一函数）。回归：`cdp_panel_check.js` 第 23 步（运行中同步）；启动竞态验证方式：先起 electron 再起后端 → 面板 `voiceMode.ttsEnabled()` 应为 false
- **气泡朗读按钮（喇叭）三态**：空闲点=合成并朗读；合成中点=取消；**播放中再点=停止播报**（图标变红色方块、标题「停止播报」）。单音频元素 `ttsAudio` 被自动朗读/流式 TTS 共用 → 任何新播放开始前 `clearSpeakPlaying()` 复位旧的"播放中"按钮；旧异步流程用全局 `speakToken` 作废、按钮自身复位用 `dataset.gen` 判定（别的按钮被点不该卡住本按钮）、`waitAudioEnded` 监听 **ended/pause/emptied/error**（换 src 在 Chromium 不一定触发 pause，漏了按钮会永远"播放中"）。回归：`cdp_panel_check.js` 第 22 步（SPEAKSTOP_OK；环境播不出来时软跳过 SKIP_PLAY，不判红）
- **思考流（thinking_stream）**：DeepSeek `reasoning_content` 靠 `llm.py` 的 `_install_langchain_shims` 保留（包装 `langchain_openai` 的 `_convert_delta_to_message_chunk`/`_convert_dict_to_message` 塞进 `additional_kwargs`——原版会静默丢弃；chunk 聚合时字符串自动拼接；版本升级由假端点回归保护，且验证 history 不回传 reasoning——`_convert_message_to_dict` 白名单不会把它发回端点）；`session._run_agent` 逐块推 `thinking_stream`（每轮模型调用一个新 `msg_id`，超步结束重置）；**随消息持久化**（buffer_state 往返），`/history` 对带思考的 AI 消息发独立 `thinking` 条目（前端与实时流共用「默认折叠单行窗口」组件：新内容进入、旧内容左移、点行展开全文）；**正文 content 不混入思考**（只走 additional_kwargs）；mock 触发词「多想」
- **网页搜索/抓取（web_search / fetch_web）**：**优先 DeepSeek 官方 Search/Fetch**（`POST {base}/v1/tools/search`、`/v1/tools/fetch`，自研引擎；按次计费 5 元/千次、QPS 5）——Key 走 `llm_api_key_deepseek` > 旧字段 `llm_api_key` > env `LLM_API_KEY`（**与聊天 provider 无关**，用 OpenCode 聊天也只需在设置里填 DeepSeek 分服务商 Key），base 用 `llm_base_url_deepseek` > 官方端点；搜索用 `summary`（600~800 字，官方为模型设计）作摘要并附日期，fetch 直接拿 Markdown 正文；**无 Key 或调用失败自动回退内置爬虫链（360 → bing，`_search_web` 签名带 session）**，老环境零回归；`tests/test_web_tools.py` 有 autouse fixture 默认禁用官方链路（防 conftest 的 test-key 真联网），官方链路 4 个显式用例覆盖（搜索命中/回退、抓取命中/回退）
- **用量/缓存统计**：同一 shim 把 DeepSeek 顶层 `prompt_cache_hit_tokens`（原版也丢）映射进 `usage_metadata.input_token_details.cache_read`（OpenCode Go 实测原生就报 OpenAI 风格 `prompt_tokens_details.cached_tokens`，无需映射）；`build_chat_model(stream_usage=True)`；`session._run_agent` 按轮累计（跨 ReAct 多次模型调用求和，耗时=整轮生成；同时记**思考 token** `output_token_details.reasoning` 与**最近一次调用实报输入** `_last_prompt_tokens`）→ `llm_stats.py`（`data/llm_stats.json` 环形 50 条，字段含 `reason`）→ `stats_update` 事件 + `GET /llm/stats`；**状态条合并 chip**「原始 17.0k · 命中 75% · 272 t/s」（2026-10-18 起：原「标题栏上下文占用」并入——原始消息估算 token（`context_stats().breakdown.raw`）+ 近 50 轮平均命中/速度；标题栏不再有 token chip）；点击打开**合并面板**（`#stats-panel`）；**上下文展示口径（2026-10-18 用户指定）**：单条堆叠（`.ctx-stack`，各分项占**固定 100 万总窗口**的比例，不再显示相对占比）+ 一行图例（`.ctx-legend`：色点+名称+token+占 1M 的百分比，<0.1% 显示 `<0.1%`），脚注给合计与 API 实报；`cache_known=False`（端点没报缓存字段）不计入命中率，显示「端点未报」；**启动竞态**（与语音播报同类坑）：面板先于后端加载时 `/llm/stats` 首次拉取失败 → 不重试则 chip 的命中/TPS 一直「—」（用户反馈过），修复 = `setOffline` 恢复路径一并 `refreshStats()`（2026-10-18，与 refreshLlm 同处）
- **UI 形态约定**：思考过程/工具调用都是**单行样式**（与气泡区分：气泡只给最终回答）；**相邻工具调用自动合并成一行组**（`.tool-group`，默认折叠，头部为「N 个操作 + 工具名列表」——名字**按调用顺序全量列出**（不去重、不加计数、**不设上限**），窗口放不下时用与思考行同款的**滑动窗口**（尾部对齐、左缘渐隐 `mask`）；展开后每个工具各占一行、每行可再单独点开参数/结果；单个工具不显示组头）——不要回退成卡片，也不要打散成"每个工具只占一个顶层行"；mock 固定 usage（1200/900）供 dev/e2e 验证统计
- **视觉设计语言（2026-10-18 全量重做）**：中性灰 + 单一强调色 `--primary:#2563eb` 的企业风（Linear/Vercel 气质）——**不要玻璃拟态（backdrop-filter）、不要光晕装饰、不要渐变按钮/渐变气泡、不要 emoji**（图标一律内联 SVG，如设置齿轮/麦克风；chips 纯文字）；面=纯白 + 1px 实色边框 `--line:#e5e7eb`，页底 `#f6f7f9`，圆角 12/10/8 三档，阴影仅浮层用 `--shadow-md`；用户气泡=浅灰 `#e9edf3`、助手气泡=白底描边（都不是彩色）；变量名沿用 `--glass/--glass-strong`（语义已改为纯白）；e2e 断言不再依赖 emoji（模型 chip 等文本断言在 `cdp_panel_check.js` 第 4 步，模型 chip 异步填充有等待）
- **单轮调用上限（干活硬保险）**：`model_call_limit`（默认 100，设置页「交互 → 工作循环」可调 10~500）→ `ModelCallLimitMiddleware(run_limit=…, exit_behavior="end")`（agent 构建时读取；改设置自动重建 agent）；超限时中间件注入的英文 `Model call limits exceeded` AI 消息在写回 buffer 前由 `session._neutralize_limit_message` 替换成中文提示（接着干/调大上限），不让英文错误进对话
- **图片展示（show_image）**：工作区内图片（matplotlib 先 `savefig`）或**网络图片直链**（httpx 下载到 `data/images/` 缓存，content-type/大小白名单）→ `session._pending_images` 队列 → `_emit_tool_events` 推 `image` 事件（`url=/file?path=…`）并把路径写进消息 `additional_kwargs.image_paths`（`/history` 出 `role=image` 条目）；`GET /file` 只服务工作区/`tool_results`/`images` 三个根（防穿越）；前端经主进程 `api-image` IPC 下载成本地文件再 `<img>`（file:// 页面直连 http 媒体受限）；mock 触发词「画图」（需工作区里有 plot.png）
- **自身看图（view_image）**：把**本地图片**读进模型上下文（自查图表/用户让看的图），常驻注册、只支持本地路径（不做网络图）：工作区内自由、区外走 read_file 同款审批（`READ_TOOLS`+`tool_targets` 已收录，黑名单硬拒；无工作区时绝对路径自由读）；视觉可用 → `imageutil.image_to_data_url` → `session._pending_screenshots` 队列（条目 `{url, text, viewed}`，`ScreenShotInjectMiddleware` 下一轮注入 user 消息 image_url 块——tool 消息不能带图）；**去重查 buffer**（注入消息 additional_kwargs.viewed_image == normcase 路径；被压缩掉后允许重读，重启随 buffer_state 恢复）；非视觉模型回退 OCR（`ocr.ocr_image_file`）；mock 触发词「看图」（触发词分支顺序：看图→画图→改文件→考我→多想）
- **网页渲染自查（render_page）**：开发网页/前端时的"眼睛"——`_find_browser` 找本机 Edge/Chrome（PATH → 常见安装路径），无头渲染（`--headless=new --screenshot --window-size --virtual-time-budget=8000`，**临时 user-data-dir 防碰用户浏览器配置**）→ PNG 存 `data/screenshots/`（保留最近 30 张）→ 与 view_image 同链路注入上下文；本地文件按工作目录解析 + 黑名单拦截，http(s) 直传（dev server）；找不到浏览器/超时/无产出给文字说明；prompts/workspace.md 教了"改完必须看过截图再收工"的习惯；测试 `tests/test_render_page.py` monkeypatch `_find_browser`+`subprocess.run`（不真跑浏览器）
- **对话内 HTML 预览（```html 卡片）**：回复里的围栏 `html` 代码块由 `enhanceHtmlPreviews` 渲染成预览卡（源码可折叠、可放大）：iframe `sandbox="allow-scripts"`（**绝无 allow-same-origin**，拿不到父页面/`window.planner`）；预览文档走后端 `POST /preview`→`GET /preview/{id}`（**不能用 srcdoc**——srcdoc 会继承面板 CSP `script-src 'self'` 把内联脚本拦掉；http 文档不继承），GET 时 `_wrap_preview_html` 注入收紧版 CSP（`connect-src 'none'` 断掉调后端 API 的路，form/frame/object 禁用）；预览 HTML 属不可信内容——**任何新渲染路径都必须保持这两道隔离（sandbox + 预览文档自己的 CSP）**；mock 触发词「做个网页」
- **工作目录面板（📁 chip）**：面板**悬在整个输入框上方**（`positionPopover('#ws-panel', $('#input-bar')`，不是 chip）——按 chip 定位会盖住输入区，表现为"点输入框没反应/打不了字"（第一下只关面板、焦点落 BODY；任务栏缩小再放大触发 resize 关掉面板后才正常）；点目录项是"浏览进入"（重渲染后**面板不关闭**——document click 里对"处理过程中被移除的 target"要放行，否则 `closest('.popover')` 落空会误关浮层，历史 bug）；「设为当前目录」按浏览位置动态启用（=cwd 时禁用）；「回到根」=cwd 回工作区根（已在根时禁用）；「📁 打开文件夹」主进程 `shell.openPath`（`open-path` IPC）；「更换工作区」**直接弹目录选择器**、选完即收起面板（不再绕设置页；未启用工作区时点 📁 也直接弹选择器，弹框关闭后主进程把键盘焦点还给原窗口）。回归：`cdp_panel_check.js` 第 17 步（导航不消失/按钮状态/**面板开着时不得盖住输入区**/cwd 落库断言）
- **键盘焦点兜底（Windows 原生弹窗后遗症）**：`window.confirm`/原生对话框关闭后键盘焦点可能不还给页面（**鼠标能点、输入框打不了字**，任务栏最小化再还原才恢复——用户实机踩过）——**应用内确认框一律用 `uiConfirm`（面板 `#ui-confirm` / 设置页同款），禁止再用原生 confirm/alert**；主进程 `refocus-window` IPC（`win.focus()+webContents.focus()`）供前端兜底，面板输入区 mousedown 时无条件调用（`document.hasFocus()` 在故障态仍是 true，别拿它做条件）；`pick-directory` 弹框关闭后也显式把焦点还给父窗口；回归：`cdp_panel_check.js` 第 18 步（最近目录→应用内确认→切换后输入框第一下点击即可打字；若原生 confirm 回归会挂住页面→超时报红 `NATIVE_CONFIRM_HANG`）
- **面板收/展与在途生成（2026-10-17 修复）**：主进程 `/dequeue` 把事件**实时投给面板窗口（含隐藏态，audio 除外按形态单播）**——隐藏期间 DOM 与对话流保持一致；`pendingEvents` 只剩"面板页未加载完"的缓存（morph-in-done 补发，别再加别处写入）；展开完成 `onPanelShown` **生成中（state.thinking）不 `loadHistory(true)` 重建**——在途思考/工具调用/流式正文还没写进 buffer（`_run_agent` 结束才回写），重建=全抹掉（用户实测："生成中收/展面板，工具行和 thinking 都没了"），空闲时才重建做全量对齐；**morph 陈旧定时器**（morph.js）：每次 `runMorph` 递增 `morphGen` 并清掉上一次的兜底定时器/transitionend 监听器——历史 bug：morph-out 经 transitionend 完成后其 590ms 兜底定时器仍挂着，590ms 内又开始 morph-in（收起后马上弹审批/提问）会把"out 完成"再发一次，主进程把刚展开的面板又隐藏；main.js 的 morph-in/out-done 加了状态守卫（非 morphing_* 时忽略并 `[morph]` 记日志）。回归 `cdp_panel_check.js` 第 21 步（隐藏期间发「出题，多想一下」→ 自动展开 → 断言思考块/提问卡/引导语仍在，答完收尾完整）；**mock 触发词全历史扫描**：一个词用过就永远留在历史里 → 新触发词必须加在旧词之前（「出题」= ask_user 专属高优先词，注释在 `llm.MockChatModel._generate`）
- **对话区滚动策略**：追加内容（思考流/正文流/工具行/图片/日志）**只在用户"贴底"（距底 <100px）时跟随**；用户翻上去看历史时绝不打扰（`scrollMessagesToBottom(force)`，force 只给用户自己的动作：发消息、jump 按钮）；`loadHistory` 重建时按"距底距离"恢复阅读位置（压缩后台重载/面板重显不打断）；回归：`cdp_panel_check.js` 第 19 步
- 对话 buffer：内存 → **收到消息立即持久化**（`_receive` → `_save_buffer_state`，含 `_inbox` 快照）→ 生成结束再存一次 → 落 memory_tree.db 的 buffer_state（重启恢复；`inbox` 列 2026-10-18 加，老库开库时 compat 自动补列）。**断点续聊**：重启时 `_load_buffer_state` 恢复 inbox 并拉起 `_player_worker` 续跑排队消息；强杀/断电最多丢"正在生成、还没收束的半句"，用户消息与排队消息不再丢（事故背景：外部强杀 + 只在生成结束保存 → 用户最后一轮消息丢失）
- **悬浮球交互**（2026-10-17 起）：单击=放大（`togglePanel` 展开面板）；长按=语音输入；拖拽移动；拖文件=挂载；右键菜单=放大 / 语音对话模式 / 免打扰 / 设置 / **显示最近的气泡**（主进程 `send('show-reply')` → bubble.js `showLastReply`，原单击行为）/ 清除气泡 / 退出。回归测试：`tests/cdp_bubble_check.js`（e2e 内置，真·受信输入实测单击展开）
- **任务栏（Windows）**：只有**面板窗口**有任务栏标签——`skipTaskbar: false` + `icon: bubble.ico` + `app.setAppUserModelId('com.xiaozhu.planner')`（不设 AppUserModelID 会被归到 "Electron"）；悬浮球/气泡窗口仍是 `skipTaskbar: true`（托盘型小部件不占任务栏）；**最小化走系统标准语义**（点任务栏图标缩小、再点还原，不再收起成悬浮球）；只有"变形动画进行中"被最小化才干预——rAF 暂停会冻住动画：in→直接落到已展开终态；out→完成收起，`restore()` 后**延迟 60ms 再 hide**（在"最小化+隐藏"残留态上直接 hide 会留任务栏幽灵按钮，点它会又冒一个面板、与悬浮球并存卡死）；面板收起（`morph-out-done → hide()`）后标签自动消失
- **面板页加载自愈**：`morph.js` 加载时查 `getPanelState()`，若已是 `shown` 就直接进入可交互态（`stage.ready` + `panel-ui` opacity/pointer-events）——`#morph-stage` 默认 `pointer-events: none`、`#panel-ui` 默认透明，页面一旦刷新/异常重载原本会整页透明且点不动（输入框点不进）；回归：`cdp_panel_check.js` 第 17 步断言输入框可聚焦未被遮挡
- 全局状态用访问器：`game_state` 模式（session 状态机 `hidden → morphing_in → shown → morphing_out`）
- 退出用 `doQuit()`（process.exit，app.quit 会被窗口 close 拦截）
- 日志 `logging.getLogger("planner.*")` + RotatingFileHandler，不要 print

## 文档地图

- `docs/README.md`：目录索引（每个目录装什么 + 面试叙事 + 不可挪清单）
- `docs/tour.md`：现状导览（代码地图/三条主线/中间件速查/术语表/阅读路线）——人想读懂项目先看这份
- `docs/面试深挖提纲.md`：面试用（高频追问的标准答法 + 深挖一层 + 会被问穿的题 + 准备节奏）
- `docs/api-contract.md`：前后端契约
