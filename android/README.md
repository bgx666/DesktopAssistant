# 小助 Android（主力端）

安卓版小助：**手机是唯一的大脑**——对话、记忆树、日历、提醒都在手机本地；电脑只在需要时充当可选工具外设（截屏 / 读文件）。

## 图片是怎么存的、怎么找回来的

图片不进数据库、也不进记忆树，但**不会丢**：

```
filesDir/images/<sha1>.jpg     图片本体（内容寻址，同一张只存一份）
messages.images = ["sha1:…"]   数据库里只留几十字节的引用
记忆节点的 details.image_refs   压缩后仍留下 sha1 + 时间 + 当时说的话
```

| 环节 | 做法 |
|---|---|
| 落盘 | 尽量只存引用；**存文件失败就退回 base64**（绝不因为存文件失败丢图） |
| 迁移（老库里的 base64） | 启动时每次搬 20 行，**先写文件、成功了才改那一行**；逐行独立，失败的行原样留着下次再试 |
| 读取 | **同时认 `sha1:` 与 `data:` 两种格式** → 半迁移状态照常可用（这是"迁移不可能丢图"的关键） |
| 压缩 | 图片本体不进节点（base64 会把节点撑大几百倍），但**引用 + 当时说的话**写进 `image_refs`；摘要模型会**看到这些图**并把内容写进摘要 |
| 找回 | `list_images`（按日期/关键词列元信息，两条线：buffer + 记忆树）→ `show_image(id)` 把**一张**图放进待注入槽 → `AgentLoop` 在**下一次模型调用前**注入图片块（与 PC 的 `capture_screen` + `ScreenShotInjectMiddleware` 同构，取走即清空） |
| 管理 | 设置页 →「图片」：概览占用/建议上限、按时间与关键词筛选、三列网格、多选删除、导出 zip（含清单.txt）；相册里被删掉的显示「已不在」但**记录保留** |

**到上限只提示、不自动删**（上限可选 200MB / 500MB / 1GB / 不限制）。删图片**只删文件**：
记忆里仍然记得"那天有过一张照片"，只是之后没法再把画面调出来看——这句话在删除界面上会写明。

## 工具链（已装好，全部在 D 盘）

| 组件 | 版本 | 路径 |
|---|---|---|
| JDK | Temurin 17.0.20.1 | `D:\Android\jdk17` |
| Gradle | 8.9 | `D:\Android\gradle-8.9` |
| Android SDK | platform 35 / build-tools 35.0.0 / platform-tools 37.0.1 | `D:\Android\sdk` |
| Gradle 缓存 | — | `D:\Android\gradle-home`（故意放 D 盘，不占 C 盘） |
| 安装包缓存 | — | `D:\Android\downloads`（约 456MB，可删） |

装法是**便携式**的：全部 zip 解压，不需要管理员权限，也**没有装 Android Studio**。首次构建需要联网下载 AGP / Kotlin / Compose 依赖。

## 怎么构建

```powershell
cd D:\xiaob\planner\android
powershell -File build.ps1              # 编译 debug APK
powershell -File build.ps1 -Task test   # 单元测试
powershell -File build.ps1 -Install     # 编译并安装到已连接的手机
```

`build.ps1` 自己设好 `JAVA_HOME` / `ANDROID_HOME` / `GRADLE_USER_HOME` 再调 `gradlew`，不依赖系统环境变量。

> ⚠️ `build.ps1` 故意只写 ASCII：Windows PowerShell 5.1 读**没有 BOM** 的含中文 `.ps1` 会解析错乱（`../build.ps1` 就踩过这个坑）。若要加中文注释，必须存成**带 BOM 的 UTF-8**。

## 装到手机

1. 手机开发者选项里打开 **USB 调试**（或用「无线调试」），USB 连上电脑
2. `D:\Android\sdk\platform-tools\adb.exe devices` 确认能看到设备
3. `powershell -File build.ps1 -Install`；或手动
   `adb install -r app\build\outputs\apk\debug\app-debug.apk`

### 不用数据线：侧载

1. 电脑上 `powershell -File build.ps1`，产物在
   `app\build\outputs\apk\debug\app-debug.apk`（约 11 MB）
2. 把这个文件传到手机（微信文件传输 / USB 拷 / 网盘都行）
3. 手机上点开它安装；系统会拦一次「未知来源」，在弹窗里允许即可
   （各 ROM 位置不同：设置 → 应用 → 特殊权限 → 安装未知应用）
4. 覆盖安装**不会清数据**；`adb install -r` 同理。真要清干净用「清除数据」

## 升级不能丢数据（三条铁律）

用户的**对话、日历、记忆树**全在 `/data/data/com.xiaozhu.planner/databases/planner.db` 一个文件里。
Android 只在两种情况下清它：**卸载**、**设置里点"清除数据"**。所以：

| 做法 | 结果 |
|---|---|
| `adb install -r` 覆盖安装 / 手机上点 APK 安装 | ✅ 数据保留 |
| **卸载再装** | ❌ 全丢 |
| 设置 → 应用 → 清除数据 | ❌ 全丢 |
| 降级安装（装更低的 versionCode） | 系统拒绝；`-d` 强装也保留数据 |

**我这边唯一的风险是"改了表结构却没写迁移"**——Room 遇到对不上的 schema 会抛异常
（或走破坏性迁移把库清空）。所以定死两条规矩：

1. **绝不允许 `fallbackToDestructiveMigration`**。改了实体就 `version + 1`，
   并在 `PlannerDatabase.MIGRATIONS` 里补一条 `Migration(n, n+1)`。
2. `PlannerDatabaseMigrationTest` 机械校验：
   - 迁移链必须能从 1 一路走到库里的实际版本（读 `PRAGMA user_version`）——
     **忘了写迁移会在单测阶段就红**
   - 在一个 v1 结构的库上塞进节点/对话/运行状态，跑完迁移逐条查回来——
     **迁移把数据弄丢也会红**

另外给用户两条后路：

- 设置页「**导出备份**」：导出成与 PC 导出脚本**同格式**的 JSON（记忆树含叶子原文 +
  当前对话 + 日历 + 运行状态），换机/重装都能导回来
- 「导入并替换」这类**破坏性操作之前，自动在 `files/backup/` 留一份**，路径会写在结果里

### 装完先做这四件事
| 顺序 | 做什么 | 不做会怎样 |
|---|---|---|
| 1 | 设置页填 **API Key**（DeepSeek 或任意 OpenAI 兼容端点） | 发消息只会提示"还没配置" |
| 2 | 允许 **通知权限**（首次启动会问） | 到点提醒静默消失——最坑的一种失败 |
| 3 | 允许 **精确闹钟**（设置页有状态与跳转） | 提醒会晚几分钟（不精确闹钟兜底，不会丢） |
| 4 | 想用 MiMo 音色就填 **MiMo API Key**（与上面的 key 不是一家） | 退回系统语音，能出声但音色不同 |

麦克风权限按需申请（点「说」时）。语音识别用系统服务，部分机型要自己下离线包。

### 把电脑上的记忆搬过来（一次性）

```powershell
# 在 PC 仓库根目录
python scripts\export_to_android.py `
    --memory-tree D:\xiaob\planner-release\data\memory_tree.db `
    --calendar    D:\xiaob\planner-release\data\planner.db `
    --out         D:\xiaob\planner-export.json
```

把 JSON 传进手机 → App 设置页「导入旧数据」→ 选它 → 看清两边各有多少 → 确认。
第一次装 App 选「清空后导入」；已经在手机上聊过一阵子就选「追加」。
`--no-details` 能大幅减小体积，但**叶子原文就没了**（只剩摘要），导入时会明确提醒这一点。

模拟器或数据线都行，先把文件推上去：

```powershell
& "D:\Android\sdk\platform-tools\adb.exe" push D:\Android\logs\planner-export.json /sdcard/Download/
```

### 用真实数据验一遍导入（不需要设备）

`RealExportImportTest` 拿**真实导出文件**跑完整导入，断言一条记忆都不丢：每个叶子的原文、
画像四维度、活跃标记（被合并的节点不进上下文）、父子链接、重复系列 id、buffer 的角色与工具配对。
文件不在就自动跳过（仓库里不放私人对话），想验就：

```powershell
python scripts\export_to_android.py --memory-tree ...\memory_tree.db --calendar ...\planner.db `
    --out D:\Android\logs\planner-export.json
$env:PLANNER_ANDROID_EXPORT = "D:\Android\logs\planner-export.json"
cd android; powershell -File build.ps1 -Task test
```

导出端形状若变化，`scripts\check_export_contract.py` 会在电脑上就报出来，不用等导完才发现。

## 目录结构

```
android/
  settings.gradle.kts / build.gradle.kts / gradle.properties
  build.ps1               # 本机构建入口（设好 D 盘工具链环境）
  local.properties        # sdk.dir 指向 D:\Android\sdk（本机专用，不入库）
  app/
    build.gradle.kts
    src/main/
      AndroidManifest.xml # 通知 / 精确闹钟 / 开机重排 权限与 receiver
      assets/prompts/     # 安卓版 system.md + 来源与同步规程（见该目录 README）
      java/com/xiaozhu/planner/
        MainActivity.kt   # Compose 入口（聊天 / 日历 / 记忆 / 设置 四页）
        core/llm/         # OpenAI 兼容客户端：SSE 流式 + tool_calls
        core/agent/       # AgentLoop（原生多轮 ReAct）/ 工具注册表 / prompt / 会话落盘
        core/memory/      # 分层摘要记忆树（Room）+ 压缩 + buffer 修复
        core/calendar/    # 日历语义层：日期工具 / 重复系列 / 议程快照 / 免打扰
        core/tools/       # 11 个常驻工具（网页、记忆、日历、心跳、分段说话、免打扰）
        core/speech/      # 朗读文本清洗 + MiMo TTS 客户端 + 返回体解析（纯逻辑，可单测）
        data/             # Room（记忆 + 日历）、加密设置、免打扰与心跳状态
        reminder/         # AlarmManager 精确闹钟 → 通知；开机/改时间重排 + 补提醒
        speech/           # 平台侧：AppSpeaker（MiMo→系统 TTS 兜底）/ VoiceInput（系统识别）
        ui/               # chat / calendar / memory / settings 四个 Compose 页面
      res/                # strings（应用名 小助）/ themes / 通知图标
```

## 阶段状态

| 阶段 | 状态 |
|---|---|
| **P0 骨架 + 聊天** | **已完成**：流式对话 + 工具调用卡片 + 设置页（加密存 key）|
| **P1 本地记忆** | **已完成**：Room 记忆树 + 压缩接线 + 记忆浏览页（分层 + 关键词检索）。检索用 `LIKE` 而非 FTS——SQLite 默认分词器把连续中文当一个词，FTS 反而搜不到 |
| **P2 日历 + 提醒** | **已完成**：`calendar_items` 一套日程（唯一待办概念）+ 重复系列（生成实体行 + 滚动续期）+ AlarmManager 精确闹钟 → 通知 + 免打扰 + `[当前议程]` 注入 + 日历页 |
| **P3 语音 + 搜索** | **已完成**：`web_search` / `fetch_web`（Bing RSS 解析）；朗读走 MiMo TTS（`/chat/completions`，与 PC 版同一把嗓子）+ 系统 TTS 兜底；语音输入用系统 `SpeechRecognizer`（零 key、多数机型可离线） |
| **P4 连电脑 + 迁移** | **迁移已完成**（设置页「导入旧数据」读 PC 导出的一次性 JSON，带预览与确认）；连电脑（B 方案：无 agent / 无记忆 / 不对话的极小工具服务）未开始 |
| **P5 打磨** | **已完成**：心跳真正落地、通知/精确闹钟权限引导、打包与侧载说明、导入旧数据 |

### P2 的几个关键取舍（都是踩过或差点踩的坑）

- **精确闹钟而不是 WorkManager**：WorkManager 只保证"这个时间之后"，晚十几分钟对"20:00 提醒我"就是失职。没拿到 `SCHEDULE_EXACT_ALARM` 时降级成不精确闹钟——**宁可迟到，不能不到**（设置页有这两项权限的状态与跳转）。
- **重复系列生成实体行**，不是查询时虚拟展开：到点提醒靠"真实的行 + `remindedAt` 防重"，"这一次做过了/改期了"终究要落到行上。代价是只能排到 horizon（1 年），由滚动续期兜住。
- **免打扰期间不投递也不标记**，而是把闹钟顺延到窗口结束——否则 22:30 的提醒会被静默丢掉，用户永远不知道有这回事。
- **议程快照每轮重建**（今天 + 未来 7 天，重复项折叠成一行），这样模型不必先调一次 `list_schedule` 才敢说话。
- `scripts/check_android_prompt.py` 现在会**交叉校验 prompt 与真实注册表**：prompt 承诺了却没注册、注册了却没在 prompt 里说，都会红。

### P3 的两个取舍

- **朗读优先 MiMo、失败退回系统 TTS**：MiMo 保证和电脑版是同一把嗓子；系统 TTS 保证"没配密钥 / 断网 / 请求失败"时也不至于哑巴。读之前会清洗文本（链接留文字、括号内容不读、emoji 不读、markdown 符号去掉）——`（笑）` 被念成"括号笑括号"是最典型的翻车。
- **语音输入用系统 `SpeechRecognizer`，不接云 ASR**：零 key、零成本、不用把用户说的话再发给第三方；代价是识别质量随 ROM 差异大。所以它只是**输入的一种方式**，键盘永远在。
- **用户一发消息就停止朗读**：手机上听着长回复想插话时，最烦的是"它还在念"。

### P5 心跳怎么真正落地（安卓上没有 PC 那个调度线程）

PC 版靠后端调度线程唤醒心跳；安卓上没有那个线程，所以"心跳"必须自己找落脚点：

- **不精确闹钟**（`setAndAllowWhileIdle`）：心跳是自省节律，Doze 里晚一点无所谓；
  顺带不占精确闹钟额度，也不会因为没给 `SCHEDULE_EXACT_ALARM` 就完全失效。
- **三条去向**：界面活着 → 交给界面说；界面不在 → 起 `HeartbeatService` 跑一轮、回复推通知；
  免打扰 → 顺延到窗口结束（跳过一次心跳没损失，吵醒用户才是损失）。
- **前台服务跑完就停**，不做常驻：常驻要一直挂通知、一直耗电，而心跳是"隔一阵醒一次"。
  起不来时退回一条通知，**不静默失败**。
- **"想一轮"抽成了 `ConversationEngine`（单例）**：聊天页与后台服务共用同一份历史与工具集，
  否则后台跑过一轮、用户回到界面就看不到那段——记忆在库里没错，但上下文会错位。
  轮次由 `Mutex` 串行化：并发跑两轮会写坏 buffer（`assistant(tool_calls)` 与 `tool` 结果必须成对相邻）。

## 相关文档

- `app/src/main/assets/prompts/README.md`：安卓 prompt 的来源、工具映射（A/B 两类）、同步规程
- `../AGENTS.md`：PC 版仓库约定（发版流程、测试隔离）
- `../docs/tour.md`：PC 版代码导览
- `../scripts/export_to_android.py`：旧记忆 + 日历的一次性导出
- `../scripts/check_android_prompt.py`：prompt 工具名 ↔ 注册表一致性检查