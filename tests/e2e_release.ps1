# e2e_release.ps1 —— release 版端到端验证（强制隔离数据 + mock LLM）
# 验证 release 后端（venv python）+ Electron 前端启动链路。
# 数据目录一律用 $env:TEMP\opencode\planner_e2e_release（重建），
# 绝不触碰 release 真实 data/。窗口里显示的是测试对话。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File tests\e2e_release.ps1

$ErrorActionPreference = "Stop"
# release 目录 = 仓库上一级 planner-release（与 build.ps1 默认一致）
$RepoRoot = Split-Path $PSScriptRoot -Parent
$ReleaseRoot = Join-Path (Split-Path $RepoRoot -Parent) "planner-release"
$Port = 18772
$AppDir = Join-Path $ReleaseRoot "app"
$VenvPython = Join-Path $ReleaseRoot "venv\Scripts\pythonw.exe"   # pythonw：与真实启动一致，验证无控制台后端
$ElectronExe = Join-Path $ReleaseRoot "app\frontend\node_modules\electron\dist\electron.exe"
$FrontendDir = Join-Path $ReleaseRoot "app\frontend"
$IsolatedData = Join-Path $env:TEMP "opencode\planner_e2e_release"
$StartBat = Join-Path $ReleaseRoot "start.bat"

# ── 隔离性断言 1：start.bat 的隔离配置必须与期望值完全一致（防回归）──
$batText = Get-Content $StartBat -Raw
$sets = @{}
foreach ($line in (Get-Content $StartBat)) {
    if ($line -match '^set "([^=]+)=(.*)"$') { $sets[$Matches[1]] = $Matches[2] }
}
$expected = @{
    "PLANNER_PYTHON"      = $VenvPython
    "PLANNER_DATA_ROOT"   = Join-Path $ReleaseRoot "data"
    "PLANNER_PORT"        = "$Port"
    "PLANNER_URL"         = "http://127.0.0.1:$Port"
    "PLANNER_USER_DATA"   = Join-Path $ReleaseRoot "user-data"
}
foreach ($k in $expected.Keys) {
    if (-not $sets.ContainsKey($k)) { throw "start.bat 缺少隔离配置：$k" }
    if ($sets[$k] -ne $expected[$k]) {
        throw "start.bat 的 $k 与期望不一致：$($sets[$k]) ≠ $($expected[$k])（隔离失效！）"
    }
}
if ($batText -match "18771") { throw "start.bat 含有开发版端口 18771（隔离失效！）" }
Write-Host "[e2e] start.bat 隔离配置完整且与期望一致（5 个变量，无 18771）"

# 前置检查：release 真实后端若在跑，先拒绝（避免端口/数据混淆）
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "18772 已被占用（release 版可能正在运行）——先退出小助再跑验证"
}

Remove-Item $IsolatedData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $IsolatedData | Out-Null

# 子进程环境：隔离数据 + mock + release 端口
$env:PLANNER_DATA_ROOT = $IsolatedData
$env:PLANNER_MOCK_LLM = "1"
$env:PLANNER_PORT = "$Port"
$env:PLANNER_URL = "http://127.0.0.1:$Port"
$env:PLANNER_USER_DATA = Join-Path $IsolatedData "user-data"

Write-Host "[e2e] 启动隔离后端（mock + 隔离数据：$IsolatedData）"
$backend = Start-Process -FilePath $VenvPython -ArgumentList "-m planner" -WorkingDirectory $AppDir -PassThru -WindowStyle Hidden

$state = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $state = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/state" -TimeoutSec 3
        break
    } catch { }
}
if (-not $state) { throw "隔离后端 20 秒内未启动" }
if ($state.state.mode -ne "mock") { throw "要求 mock 模式，实际 mode=$($state.state.mode)" }
if (-not (Test-Path (Join-Path $IsolatedData "planner.db"))) { throw "后端未使用隔离数据目录！" }
Write-Host "[e2e] 后端 OK：mode=$($state.state.mode) 数据目录已隔离"

# 发一条测试对话（mock 回复），让窗口显示测试内容而非真实数据
$body = @{ message = "这是端到端验证的测试对话，请忽略" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/chat" -Method Post -ContentType "application/json" -Body $body | Out-Null
Write-Host "[e2e] 已发送测试对话"

Write-Host "[e2e] 启动 release Electron（窗口将显示测试对话）"
$CdpPort = 9223
$el = Start-Process -FilePath $ElectronExe -ArgumentList @($FrontendDir, "--remote-debugging-port=$CdpPort") -PassThru

# 用 CDP 确认渲染页面加载成功（短连接轮询 netstat 抓不到，页面存在即证明链路）
$pages = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $pages = Invoke-RestMethod -Uri "http://127.0.0.1:$CdpPort/json" -TimeoutSec 2
        break
    } catch { }
}
if ($el.HasExited) { throw "Electron 提前退出" }
if (-not $pages) { throw "CDP 未返回页面列表（前端可能未加载）" }
$titles = @($pages | ForEach-Object { $_.url })
if (($titles -match "bubble.html").Count -eq 0) { throw "悬浮球页面未加载：$($titles -join '; ')" }
Write-Host "[e2e] Electron OK：页面已加载（$($titles -join ' ; ')）"

# ── 隔离性断言 2：CDP 验证前端 apiBase 指向 release 端口（不是 18771）──
$cdpJs = Join-Path $env:TEMP "opencode\cdp_apibase_check.js"
@'
const http = require("http");
http.get("http://127.0.0.1:CDPPORT/json", (res) => {
  let d = "";
  res.on("data", (c) => (d += c));
  res.on("end", () => {
    const pages = JSON.parse(d);
    const bubble = pages.find((p) => p.url.includes("bubble.html"));
    if (!bubble) { console.log("CDP_APIBASE=NO_PAGE"); process.exit(1); }
    const ws = new WebSocket(bubble.webSocketDebuggerUrl);
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate",
      params: { expression: "window.planner && window.planner.apiBase", returnByValue: true } }));
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id === 1) {
        console.log("CDP_APIBASE=" + (msg.result && msg.result.result ? msg.result.result.value : "UNKNOWN"));
        ws.close(); process.exit(0);
      }
    };
    setTimeout(() => { console.log("CDP_APIBASE=TIMEOUT"); process.exit(1); }, 6000);
  });
});
'@ -replace "CDPPORT", "$CdpPort" | Set-Content -Path $cdpJs -Encoding ascii
$apiBase = (& node $cdpJs) -replace "^CDP_APIBASE=", ""
if ($apiBase -ne "http://127.0.0.1:$Port") {
    throw "前端 apiBase=$apiBase，期望 http://127.0.0.1:$Port（前端连错了端口，隔离失效！）"
}
Write-Host "[e2e] 前端 apiBase 指向 release 端口：$apiBase"

# ── 隔离性断言 3：后端数据目录 + 隔离 user-data 均落在隔离路径 ──
if (-not (Test-Path (Join-Path $IsolatedData "planner.db"))) { throw "后端未使用隔离数据目录！" }
if (-not (Test-Path (Join-Path $IsolatedData "user-data"))) { throw "Electron 未使用隔离 user-data（PLANNER_USER_DATA 未生效，将与开发版共用！）" }
Write-Host "[e2e] 隔离 user-data + 后端数据均落在隔离目录"

Write-Host "[e2e] 验证通过：release 链路完整且隔离断言全部通过"

# 清理
Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*planner-release*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item Env:PLANNER_DATA_ROOT, Env:PLANNER_MOCK_LLM, Env:PLANNER_PORT, Env:PLANNER_URL, Env:PLANNER_USER_DATA -ErrorAction SilentlyContinue
Write-Host "[e2e] 已清理进程与环境变量"
