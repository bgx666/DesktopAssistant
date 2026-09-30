# 小助 Android（主力端）

安卓版小助：**手机是唯一的大脑**——对话、记忆树、日历、提醒都在手机本地；电脑只在需要时充当可选工具外设（截屏 / 读文件）。

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
        data/             # Room（记忆 + 日历）、加密设置、免打扰与心跳状态
        reminder/         # AlarmManager 精确闹钟 → 通知；开机/改时间重排 + 补提醒
        ui/               # chat / calendar / memory / settings 四个 Compose 页面
      res/                # strings（应用名 小助）/ themes / 通知图标
```

## 阶段状态

| 阶段 | 状态 |
|---|---|
| **P0 骨架 + 聊天** | **已完成**：流式对话 + 工具调用卡片 + 设置页（加密存 key）|
| **P1 本地记忆** | **已完成**：Room 记忆树 + 压缩接线 + 记忆浏览页（分层 + 关键词检索）。检索用 `LIKE` 而非 FTS——SQLite 默认分词器把连续中文当一个词，FTS 反而搜不到 |
| **P2 日历 + 提醒** | **已完成**：`calendar_items` 一套日程（唯一待办概念）+ 重复系列（生成实体行 + 滚动续期）+ AlarmManager 精确闹钟 → 通知 + 免打扰 + `[当前议程]` 注入 + 日历页 |
| P3 语音 + 搜索 | 部分完成：`web_search` / `fetch_web` 已可用；TTS 复用 MiMo（OpenAI 兼容）、**ASR 选型待定**（系统 SpeechRecognizer 零 key vs 云端）未做 |
| P4 连电脑 + 迁移 | 未开始。PC 侧只做**极小工具服务**（B 方案：无 agent / 无记忆 / 不对话）；迁移用 `../scripts/export_to_android.py` |
| P5 打磨 | 未开始。前台服务保活、心跳真正落地、打包与侧载说明 |

### P2 的几个关键取舍（都是踩过或差点踩的坑）

- **精确闹钟而不是 WorkManager**：WorkManager 只保证"这个时间之后"，晚十几分钟对"20:00 提醒我"就是失职。没拿到 `SCHEDULE_EXACT_ALARM` 时降级成不精确闹钟——**宁可迟到，不能不到**（设置页有这两项权限的状态与跳转）。
- **重复系列生成实体行**，不是查询时虚拟展开：到点提醒靠"真实的行 + `remindedAt` 防重"，"这一次做过了/改期了"终究要落到行上。代价是只能排到 horizon（1 年），由滚动续期兜住。
- **免打扰期间不投递也不标记**，而是把闹钟顺延到窗口结束——否则 22:30 的提醒会被静默丢掉，用户永远不知道有这回事。
- **议程快照每轮重建**（今天 + 未来 7 天，重复项折叠成一行），这样模型不必先调一次 `list_schedule` 才敢说话。
- `scripts/check_android_prompt.py` 现在会**交叉校验 prompt 与真实注册表**：prompt 承诺了却没注册、注册了却没在 prompt 里说，都会红。

## 相关文档

- `app/src/main/assets/prompts/README.md`：安卓 prompt 的来源、工具映射（A/B 两类）、同步规程
- `../AGENTS.md`：PC 版仓库约定（发版流程、测试隔离）
- `../docs/tour.md`：PC 版代码导览
- `../scripts/export_to_android.py`：旧记忆 + 日历的一次性导出
- `../scripts/check_android_prompt.py`：prompt 工具名 ↔ 注册表一致性检查