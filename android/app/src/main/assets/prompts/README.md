# 安卓版 system prompt：来源、改动与同步规程

## 这份文件是什么

安卓 App 的系统提示词。**不是** PC 版的副本，而是从 PC 版裁剪来的：PC 版 13 个工具里有 3 个
（`list_dir` / `read_file` / `capture_screen`）在安卓上**只有连上电脑时才存在**，其余 10 个常驻手机，
另外加一个安卓专有的 `search_memory`（全文检索）。

| | |
|---|---|
| 来源 | `src/planner/prompts/system.md`（PC 版，68 行，对应提交 `10c46fb`） |
| 本文件 | `android/app/src/main/assets/prompts/system.md`（55 行） |
| 校验 | `python scripts/check_android_prompt.py` —— 工具名多了 / 少了 / 残留 PC 内容都会报错 |

## 为什么不能整体复制

PC 版里写着「你可以用 `list_dir` / `read_file` 读用户电脑上的文件」，这些工具在安卓上**默认不存在**。
整体复制会让模型去调一个不在工具列表里的工具，然后卡住、或者说假话（"我去读一下那个文件"）。
所以安卓版必须显式声明**两类工具**，并要求模型在没有 B 类工具时如实说你读不到。

## 工具映射（PC 13 个 → 安卓）

| PC 工具 | 安卓 | 备注 |
|---|---|---|
| `heartbeat` | **A 常驻** | 同语义；安卓上受 Doze / 被强停影响，属**尽力而为** |
| `continue_speaking` | **A 常驻** | 分段说话，可被打断 |
| `set_do_not_disturb` | **A 常驻** | 默认 22:00–08:00 |
| `list_schedule` | **A 常驻** | Room + 日历页 |
| `add_schedule` | **A 常驻** | 带 `time` 的到点由 **AlarmManager 推通知**（PC 版是后端自己触发） |
| `update_schedule` | **A 常驻** | 含 `status="done"` |
| `cancel_schedule` | **A 常驻** | 支持整系列 |
| `explore_memory_tree` | **A 常驻** | 按时间范围逐层下钻到叶子看原文 |
| —（PC 没有） | **A 常驻 `search_memory`** | **新增**：全文检索（FTS）。补 PC 版"只记得内容、不记得时间就找不着"的短板 |
| `web_search` | **A 常驻** | 离线也能用（Bing RSS 解析移植） |
| `fetch_web` | **A 常驻** | 抓正文 + 页面链接 |
| `list_dir` | **B 连电脑** | 列电脑上的目录 |
| `read_file` | **B 连电脑** | 读电脑上的文件（只读） |
| `capture_screen` | **B 连电脑** | 截电脑屏幕 + OCR；安卓自身截屏需 MediaProjection + 每次授权，P5 再议 |

## 与 PC 版的逐条差异

| # | 改动 | 原因 |
|---|---|---|
| 1 | 开场白：桌面悬浮球 → 手机 App；并明说"你是自包含的，电脑关机也照常工作" | 定位变了：手机是唯一大脑 |
| 2 | `add_schedule` 的到点行为：后端自己触发 → **推通知** | 安卓用 AlarmManager + 通知栏 |
| 3 | 心跳新增一条现实约束：Doze / 被强停会推迟唤醒，**必须准时的放日历** | 安卓后台限制（PC 上没有这个问题） |
| 4 | 新增"回复会被朗读"约束：纯文本，禁 Markdown 表格 / 代码块 / 裸链接 | 手机场景常是听而不是看 |
| 5 | 自主学习那条里的 `read_file` 加"连上电脑时"限定 | 该工具在安卓属 B 类 |
| 6 | 提到 `explore_memory_tree` 的地方并列 `search_memory` | 新工具要出现在它该出现的位置 |
| 7 | 新增整节「工具可用性（两类，别搞混）」 | PC 版 13 个工具恒在，根本没这个概念 |
| 8 | PC 版 "File reading (read-only)"（默认可用）→ 并入 B 类条件说明 + 只读约束 | 同上 |
| 9 | 「记忆与一致性」新增 `search_memory` 用法一条 | 明说"不要用猜时间去代替搜索" |
| 10 | 不再出现 `calendar_items` 这个表名 | 安卓侧表名/实现不同，prompt 不该依赖具体表 |
| — | **逐字保持不变**：责任 1–5、说话规约（含对照示例）、心跳两条、DND、时间、node 一致性规则 | 这些与平台无关，是调好的行为，别顺手改写 |

## 同步规程（PC 版 prompt 更新之后）

PC 版的 `src/planner/prompts/system.md` 仍会继续演进。同步时先看改动落在哪一段：

- **与平台无关的段**（责任 / 说话规约 / 记忆与一致性 / web search 的语气约束）→ **逐字**搬过来。
- **与工具相关的段** → 先判断该工具在安卓属 A 还是 B，再改到对应位置。B 类工具只能出现在
  「工具可用性」的 B 段里，或带条件限定（"连上电脑时…"）。
- **平台专有内容一律不带过来**：悬浮球、Electron、桌面窗口、18771 / 18772 / 18766、
  拖拽文件、`planner-release` 路径、`calendar_items` 表名。

改完**必须**跑：

```bash
python scripts/check_android_prompt.py
```

它检查三件事：① prompt 里出现的工具名是否都在允许清单内；② 11 个常驻工具是否都被提到；
③ 是否残留 PC 专有内容。任一条不满足即非零退出（可直接接进 CI / 提交前检查）。

## 待定（先按现状写，定了再回来改）

- **ASR 选型**：PC 版是本地 SenseVoice（离线免费）；安卓侧尚未定——可能是系统 `SpeechRecognizer`
  （零 key、多数 ROM 可离线）或云端服务。prompt 里目前没有任何 ASR 工具，P3 再补。
- **安卓自身截屏**：若以后支持（MediaProjection + 前台服务 + 每次授权），`capture_screen`
  会从 B 类变成 A 类，届时要同时改本文件的分类与 `scripts/check_android_prompt.py` 的清单。
- **图片输入**：PC 版支持拖图（vision）。安卓侧的对应能力（分享图片给小助）在 P5 选做，
  届时 prompt 需要补一段"收到图片时怎么处理"。
