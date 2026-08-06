# build.ps1 —— 一键发版：复制代码到 D:\xiaob\planner-release 并生成 start.bat
# 用法：
#   powershell -File build.ps1            # 自动 vX.Y.Z patch+1
#   powershell -File build.ps1 -Version v0.2.0   # 手动指定
# 前置：git 工作区干净（未提交改动会拒绝发版）。

param(
    [string]$Version = "",
    [string]$ReleaseRoot = "D:\xiaob\planner-release"
)

$ErrorActionPreference = "Stop"
$DevRoot = $PSScriptRoot
$AppDir = Join-Path $ReleaseRoot "app"
$VenvDir = Join-Path $ReleaseRoot "venv"
$DataDir = Join-Path $ReleaseRoot "data"
$VersionFile = Join-Path $ReleaseRoot "VERSION"
$StartBat = Join-Path $ReleaseRoot "start.bat"
$MinicondaPython = "D:\Miniconda3\python.exe"

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

# ── 4. venv（可编辑安装：代码复制即更新，零重装开销）────────
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[build] 首次创建 venv..."
    & $MinicondaPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "venv 创建失败" }
    & $venvPython -m pip install -e $AppDir
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }
}

# ── 5. frontend 依赖（首次 npm install，electron 已缓存则快）─
if (-not (Test-Path (Join-Path $AppDir "frontend\node_modules"))) {
    Write-Host "[build] 首次 npm install..."
    Push-Location (Join-Path $AppDir "frontend")
    try {
        & npm install
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

# ── 7. VERSION + start.bat + git tag ────────────────────────
Set-Content -Path $VersionFile -Value $Version -Encoding ascii
$startBat = @"
@echo off
rem 小助 release $Version —— 双击启动（独立数据目录 $DataDir）
set "PLANNER_PYTHON=$venvPython"
set "PLANNER_DATA_ROOT=$DataDir"
start "" "$(Join-Path $AppDir "frontend\node_modules\.bin\electron.cmd")" "$(Join-Path $AppDir "frontend")"
"@
Set-Content -Path $StartBat -Value $startBat -Encoding ascii
git tag $Version
if ($LASTEXITCODE -ne 0) { throw "git tag $Version 失败" }

Write-Host ""
Write-Host "[build] 发版完成：$Version"
Write-Host "  使用：双击 $StartBat"
Write-Host "  数据：$DataDir"
