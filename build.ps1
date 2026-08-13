# build.ps1 —— 一键发版：复制代码到仓库上一级 planner-release 并生成 start.bat
# 用法：
#   powershell -File build.ps1            # 自动 vX.Y.Z patch+1
#   powershell -File build.ps1 -Version v0.2.0   # 手动指定
# 前置：git 工作区干净（未提交改动会拒绝发版）。
# Python 解释器：默认取 $env:PLANNER_PYTHON，未设置时用 PATH 中的 python。

param(
    [string]$Version = "",
    [string]$ReleaseRoot = ""   # 默认 = 仓库上一级 planner-release
)

$ErrorActionPreference = "Stop"
if (-not $ReleaseRoot) { $ReleaseRoot = Join-Path (Split-Path $PSScriptRoot -Parent) "planner-release" }
$DevRoot = $PSScriptRoot
$AppDir = Join-Path $ReleaseRoot "app"
$VenvDir = Join-Path $ReleaseRoot "venv"
$venvPythonExe = Join-Path $VenvDir "Scripts\python.exe"    # 安装/初始化用（有控制台，输出可见）
$venvPythonW = Join-Path $VenvDir "Scripts\pythonw.exe"     # 运行时注入（无控制台，后端不弹黑窗）
$DataDir = Join-Path $ReleaseRoot "data"
$VersionFile = Join-Path $ReleaseRoot "VERSION"
$StartBatPath = Join-Path $ReleaseRoot "start.bat"
$MinicondaPython = if ($env:PLANNER_PYTHON) { $env:PLANNER_PYTHON } else { "python" }

function Invoke-RobocopyMirror {
    param([string]$Src, [string]$Dst, [string[]]$Exclude)
    $a = @($Src, $Dst, "/MIR") + $Exclude + @("/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS")
    & robocopy @a | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "robocopy 失败 (code $LASTEXITCODE): $Src" }
}

# ── 1. git 工作区校验 ────────────────────────────────────────
Set-Location $DevRoot
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0) { throw "git 不可用（在 $DevRoot 下执行）" }
if ($dirty) {
    Write-Host "工作区有未提交改动，先提交再发版："
    Write-Host $dirty
    exit 1
}

# ── 2. 版本号 ────────────────────────────────────────────────
if (-not $Version) {
    if (Test-Path $VersionFile) {
        $cur = (Get-Content $VersionFile -Raw).Trim()
        if ($cur -match "^v(\d+)\.(\d+)\.(\d+)$") {
            $Version = "v{0}.{1}.{2}" -f $Matches[1], $Matches[2], ([int]$Matches[3] + 1)
        } else { $Version = "v0.1.1" }
    } else { $Version = "v0.1.0" }
}
if ($Version -notmatch "^v\d+\.\d+\.\d+$") { throw "版本号格式应为 vX.Y.Z，收到：$Version" }
$tagExists = git tag -l $Version
if ($tagExists) { throw "git tag $Version 已存在，换一个版本号" }

# ── 3. 复制代码（node_modules 除外，/MIR 不影响被排除目录）────
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Invoke-RobocopyMirror (Join-Path $DevRoot "src") (Join-Path $AppDir "src") @("/XD", "__pycache__", "*.egg-info")
Invoke-RobocopyMirror (Join-Path $DevRoot "frontend") (Join-Path $AppDir "frontend") @("/XD", "node_modules")
Copy-Item (Join-Path $DevRoot "pyproject.toml") $AppDir -Force

# ── 4. venv（可编辑安装：代码复制即更新；每次构建同步依赖，新依赖自动进 release）──
if (-not (Test-Path $venvPythonExe)) {
    Write-Host "[build] 首次创建 venv..."
    & $MinicondaPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "venv 创建失败" }
}
Write-Host "[build] 同步依赖（pip install -e）..."
& $venvPythonExe -m pip install -e $AppDir 2>&1
if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }

# ── 5. frontend 依赖（首次 npm install，electron 已缓存则快）─
if (-not (Test-Path (Join-Path $AppDir "frontend\node_modules"))) {
    Write-Host "[build] 首次 npm install..."
    Push-Location (Join-Path $AppDir "frontend")
    try {
        & npm install 2>&1
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
    } finally { Pop-Location }
}

# ── 6. 首次发版：迁移开发目录 data → release data ───────────
if (-not (Test-Path $DataDir)) {
    $DevData = Join-Path $DevRoot "data"
    if (Test-Path $DevData) {
        Write-Host "[build] 首次发版：迁移 $DevData → $DataDir"
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        Copy-Item (Join-Path $DevData "*") $DataDir -Recurse -Force
    } else {
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    }
}

# ── 7. VERSION + start.bat + start.vbs + git tag ────────────
Set-Content -Path $VersionFile -Value $Version -Encoding ascii

# release 隔离配置（集中定义，供 start.bat/start.vbs 生成与自检共用）
$RelPort = 18772                       # release 端口，与开发版 18771 必须不同
$RelUrl = "http://127.0.0.1:$RelPort"
$RelUserData = Join-Path $ReleaseRoot "user-data"
$ElectronExe = Join-Path $AppDir "frontend\node_modules\electron\dist\electron.exe"
$FrontendDir = Join-Path $AppDir "frontend"
# 共享 .env（本机私有，不入库）：从开发仓库根 .env 读取并注入 release 启动
$SharedEnvLine = ""
$VbsSharedEnvLine = ""
$DevEnvFile = Join-Path $DevRoot ".env"
if (Test-Path $DevEnvFile) {
    $line = Get-Content $DevEnvFile | Where-Object { $_ -match "^XIAOB_SHARED_ENV=" } | Select-Object -First 1
    if ($line) {
        $SharedEnvLine = $line.Trim()
        $v = $line.Substring("XIAOB_SHARED_ENV=".Length).Trim()
        $VbsSharedEnvLine = "env(""XIAOB_SHARED_ENV"") = ""$v"""
    }
}
$startBatContent = @"
@echo off
rem xiaozhu release $Version -- double-click to start (data: $DataDir, port $RelPort)
set "PLANNER_PYTHON=$venvPythonW"
set "PLANNER_DATA_ROOT=$DataDir"
set "PLANNER_PORT=$RelPort"
set "PLANNER_URL=$RelUrl"
set "PLANNER_USER_DATA=$RelUserData"
$SharedEnvLine
start "" "$ElectronExe" "$FrontendDir"
"@
Set-Content -Path $StartBatPath -Value $startBatContent -Encoding ascii

# start.vbs：wscript 无窗口启动（快捷方式用这个，不弹 cmd 黑窗）
$StartVbsPath = Join-Path $ReleaseRoot "start.vbs"
$startVbsContent = @"
Set ws = CreateObject("WScript.Shell")
Set env = ws.Environment("PROCESS")
env("PLANNER_PYTHON") = "$venvPythonW"
env("PLANNER_DATA_ROOT") = "$DataDir"
env("PLANNER_PORT") = "$RelPort"
env("PLANNER_URL") = "$RelUrl"
env("PLANNER_USER_DATA") = "$RelUserData"
$VbsSharedEnvLine
ws.Run """" & "$ElectronExe" & """" & " " & """" & "$FrontendDir" & """", 0, False
"@
Set-Content -Path $StartVbsPath -Value $startVbsContent -Encoding ascii

# start.bat 自检：隔离性缺失/被改坏 = 终止发版（防回归）
$batText = Get-Content $StartBatPath -Raw
$vbsText = Get-Content $StartVbsPath -Raw
$required = @(
    @{ Name = "PLANNER_PYTHON(venv)"; Value = $venvPythonW },
    @{ Name = "PLANNER_DATA_ROOT(release data)"; Value = $DataDir },
    @{ Name = "PLANNER_PORT($RelPort)"; Value = $RelPort },
    @{ Name = "PLANNER_URL($RelUrl)"; Value = $RelUrl },
    @{ Name = "PLANNER_USER_DATA(隔离)"; Value = $RelUserData }
)
foreach ($r in $required) {
    $key = $r.Name.Split('(')[0]
    $batPat = $key + '=' + $r.Value
    $vbsPat = 'env("' + $key + '") = "' + $r.Value + '"'
    if ($batText -notmatch [regex]::Escape($batPat)) {
        throw "start.bat 自检失败：缺少 $($r.Name)"
    }
    if ($vbsText -notmatch [regex]::Escape($vbsPat)) {
        throw "start.vbs 自检失败：缺少 $($r.Name)"
    }
}
if ($batText -match "18771") { throw "start.bat 自检失败：不允许出现开发版端口 18771" }
if ($vbsText -match "18771") { throw "start.vbs 自检失败：不允许出现开发版端口 18771" }

git tag $Version
if ($LASTEXITCODE -ne 0) { throw "git tag $Version 失败" }

Write-Host ""
Write-Host "[build] 发版完成：$Version"
Write-Host "  使用：双击 $StartBatPath"
Write-Host "  数据：$DataDir"
