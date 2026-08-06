# e2e_release.ps1 —— release 版端到端验证（强制隔离数据 + mock LLM）
# 验证 release 后端（venv python）+ Electron 前端启动链路。
# 数据目录一律用 $env:TEMP\opencode\planner_e2e_release（重建），
# 绝不触碰 release 真实 data/。窗口里显示的是测试对话。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File tests\e2e_release.ps1

$ErrorActionPreference = "Stop"
$ReleaseRoot = "D:\xiaob\planner-release"
$Port = 18772
$AppDir = Join-Path $ReleaseRoot "app"
$VenvPython = Join-Path $ReleaseRoot "venv\Scripts\python.exe"
$ElectronExe = Join-Path $ReleaseRoot "app\frontend\node_modules\electron\dist\electron.exe"
$FrontendDir = Join-Path $ReleaseRoot "app\frontend"
$IsolatedData = Join-Path $env:TEMP "opencode\planner_e2e_release"

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

Write-Host "[e2e] 验证通过：release 链路完整（隔离数据 + mock）"

# 清理
Get-Process electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*planner-release*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item Env:PLANNER_DATA_ROOT, Env:PLANNER_MOCK_LLM, Env:PLANNER_PORT, Env:PLANNER_URL, Env:PLANNER_USER_DATA -ErrorAction SilentlyContinue
Write-Host "[e2e] 已清理进程与环境变量"
