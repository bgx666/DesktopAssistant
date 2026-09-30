# Build helper for the Android app (小助 Android).
#
# ASCII-only on purpose: Windows PowerShell 5.1 mis-decodes non-ASCII .ps1 files
# that lack a UTF-8 BOM, and that can corrupt parsing (../build.ps1 hit exactly
# this). If you want Chinese comments here, save the file as UTF-8 *with* BOM.
#
# Usage:
#   powershell -File build.ps1                 # assembleDebug
#   powershell -File build.ps1 -Task test      # unit tests
#   powershell -File build.ps1 -Task lint
#   powershell -File build.ps1 -Install        # assembleDebug + install to device
[CmdletBinding()]
param(
    [string]$Task = "assembleDebug",
    [switch]$Install
)
$ErrorActionPreference = "Stop"

# Toolchain lives entirely on D: (portable zip install, no Android Studio needed).
$env:JAVA_HOME        = "D:\Android\jdk17"
$env:ANDROID_HOME     = "D:\Android\sdk"
$env:ANDROID_SDK_ROOT = "D:\Android\sdk"
$env:GRADLE_USER_HOME = "D:\Android\gradle-home"
$env:Path = "$env:JAVA_HOME\bin;$env:ANDROID_HOME\platform-tools;$env:Path"

Set-Location $PSScriptRoot
$gradlew = Join-Path $PSScriptRoot "gradlew.bat"
if (-not (Test-Path $gradlew)) {
    throw "gradlew.bat not found. Run 'gradle wrapper --gradle-version 8.9' in android/ first (gradle: D:\Android\gradle-8.9\bin\gradle.bat)."
}

# --no-daemon is deliberate: an interrupted CLI build leaves a stale "busy" daemon in
# GRADLE_USER_HOME/daemon, and the next run then hangs trying to start a new one.
& $gradlew $Task --no-daemon
if ($LASTEXITCODE -ne 0) { throw "gradle $Task failed (exit $LASTEXITCODE)" }

if ($Install) {
    & $gradlew ":app:installDebug"
    if ($LASTEXITCODE -ne 0) { throw "installDebug failed (exit $LASTEXITCODE)" }
    Write-Host "[build] installed to the connected device."
}