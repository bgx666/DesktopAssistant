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
      AndroidManifest.xml
      assets/prompts/     # 安卓版 system.md + 来源与同步规程（见该目录 README）
      java/com/xiaozhu/planner/MainActivity.kt   # P0：Compose 入口
      res/values/         # strings（应用名 小助）/ themes
```

## 阶段状态

| 阶段 | 状态 |
|---|---|
| **P0 骨架 + 聊天** | **进行中**：工程骨架与工具链就绪，等首次 `assembleDebug` 通过 → 然后接流式对话（`core/llm` + `core/agent`） |
| P1 本地记忆 | 未开始。设计要点：Room + 分层摘要树 + **FTS 全文检索**（补 PC 版"只记得内容找不到原文"的短板） |
| P2 日历 + 提醒 | 未开始。AlarmManager 精确闹钟 → 通知栏；心跳尽力而为 |
| P3 语音 + 搜索 | 未开始。TTS 复用 MiMo（OpenAI 兼容）；**ASR 选型待定**（系统 SpeechRecognizer 零 key vs 云端） |
| P4 连电脑 + 迁移 | PC 侧只做**极小工具服务**（B 方案：无 agent / 无记忆 / 不对话）；迁移用 `../scripts/export_to_android.py` 一次性导出 |
| P5 打磨 | 未开始。前台服务保活、通知权限引导、打包与侧载说明 |

## 相关文档

- `app/src/main/assets/prompts/README.md`：安卓 prompt 的来源、工具映射（A/B 两类）、同步规程
- `../AGENTS.md`：PC 版仓库约定（发版流程、测试隔离）
- `../docs/tour.md`：PC 版代码导览
- `../scripts/export_to_android.py`：旧记忆 + 日历的一次性导出