#Requires -Version 5.1
<#
.SYNOPSIS
    A股实时筛选看板 · Windows 启动脚本

.DESCRIPTION
    等价于 macOS 的「运行实时看板.command」，面向 Windows：
      1. 清理占用 8765 端口的异常残留进程（Windows 没有 lsof，改用 Get-NetTCPConnection）
      2. 后台启动 scripts/realtime_dashboard.py，stdout/stderr 写入 daily-stock-analysis/logs/
      3. 等待 http://localhost:8765/api/status 就绪（最长 120 秒，首轮含 K 线预热）
      4. 通过 BROWSER 环境变量让看板用 Edge 打开页面，不修改系统默认浏览器

.PARAMETER Stop
    停止正在运行的看板。
.PARAMETER NoBrowser
    只启动服务，不打开浏览器。
.PARAMETER Port
    监听端口，默认 8765。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "启动实时看板.ps1"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "启动实时看板.ps1" -Stop
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$NoBrowser,
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'

$ScriptDir   = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ScriptDir
$Dashboard   = Join-Path $ScriptDir 'scripts\realtime_dashboard.py'
$LogDir      = Join-Path $ScriptDir 'logs'
$BaseUrl     = "http://localhost:$Port"

function Write-Note {
    param([string]$Message)
    Write-Host "[看板] $Message"
}

function Get-PythonExe {
    foreach ($name in @('python.exe', 'python3.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Get-PortOwnerPid {
    param([int]$ListenPort)
    $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Test-DashboardApi {
    param([string]$Base, [int]$TimeoutSec = 4)
    try {
        $resp = Invoke-WebRequest -Uri "$Base/api/status" -TimeoutSec $TimeoutSec -UseBasicParsing
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-EdgeExe {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    $cmd = Get-Command 'msedge.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-AutoShutdownWindow {
    # realtime_dashboard.py 的 _loop：工作日 15:15-15:59 且已有结果时会立即收盘关机
    $now = Get-Date
    if ($now.DayOfWeek -eq 'Saturday' -or $now.DayOfWeek -eq 'Sunday') { return $false }
    return ($now.Hour -eq 15 -and $now.Minute -ge 15)
}

# ── 停止模式 ──────────────────────────────────────────────
if ($Stop) {
    $owner = Get-PortOwnerPid -ListenPort $Port
    if (-not $owner) {
        Write-Note "看板未在运行（端口 $Port 空闲）"
        exit 0
    }
    Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 1000
    if (Get-PortOwnerPid -ListenPort $Port) {
        Write-Note "停止失败，请手动结束 PID $owner"
        exit 1
    }
    Write-Note "已停止看板（PID $owner）"
    exit 0
}

# ── 前置检查 ──────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $Dashboard)) {
    Write-Note "找不到看板脚本：$Dashboard"
    exit 1
}

$python = Get-PythonExe
if (-not $python) {
    Write-Note "未找到 python，请先安装 Python 3 并加入 PATH"
    exit 1
}

# ── 端口占用处理 ──────────────────────────────────────────
$owner = Get-PortOwnerPid -ListenPort $Port
if ($owner) {
    if (Test-DashboardApi -Base $BaseUrl) {
        Write-Note "看板已在运行（PID $owner），不再重复启动"
        if (-not $NoBrowser) {
            $edgeAlive = Get-EdgeExe
            if ($edgeAlive) {
                Start-Process -FilePath $edgeAlive -ArgumentList $BaseUrl
                Write-Note "已用 Edge 打开 $BaseUrl"
            } else {
                Start-Process $BaseUrl
            }
        }
        exit 0
    }
    Write-Note "端口 $Port 被 PID $owner 占用但 API 无响应，清理异常残留进程"
    Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 1000
    if (Get-PortOwnerPid -ListenPort $Port) {
        Write-Note "端口仍被占用，请手动处理后重试"
        exit 1
    }
}

# ── 启动看板 ──────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$stamp  = Get-Date -Format 'yyyyMMdd'
$outLog = Join-Path $LogDir "dashboard_$stamp.out.log"
$errLog = Join-Path $LogDir "dashboard_$stamp.err.log"

$env:PYTHONIOENCODING = 'utf-8'

$inClosingWindow = Test-AutoShutdownWindow
if ($inClosingWindow) {
    $todayDir = Join-Path $ProjectRoot ("筛选结果\{0}" -f (Get-Date -Format 'yyyyMMdd'))
    Write-Note "注意：现在是工作日 15:15 之后，看板会在启动后立即执行收盘关机，不会常驻。"
    Write-Note "      当日报告已归档在：$todayDir"
    Write-Note "      如需盘中看板，请在交易日 09:15-15:05 之间启动。"
}

$edge = $null
if ($NoBrowser) {
    Write-Note "已指定 -NoBrowser，不打开浏览器"
} else {
    $edge = Get-EdgeExe
    if ($edge) {
        # realtime_dashboard.py 内部调用 webbrowser.open()，BROWSER 会被优先采用
        $env:BROWSER = $edge
        Write-Note "已指定 Edge 打开：$edge"
    } else {
        Remove-Item Env:\BROWSER -ErrorAction SilentlyContinue
        Write-Note "未找到 Edge，将由系统默认浏览器打开"
    }
}

$proc = Start-Process -FilePath $python `
    -ArgumentList "`"$Dashboard`"" `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

Write-Note "看板进程已启动：PID $($proc.Id)"
Write-Note "运行日志：$errLog"

# ── 等待服务就绪 ──────────────────────────────────────────
$deadline = (Get-Date).AddSeconds(120)
$ready = $false
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        if ($proc.ExitCode -eq 0 -and $inClosingWindow) {
            Write-Note "看板已按收盘规则自动退出（预期行为，非故障）"
            exit 0
        }
        Write-Note "看板进程已退出（退出码 $($proc.ExitCode)），请查看日志：$errLog"
        exit 1
    }
    if (Test-DashboardApi -Base $BaseUrl -TimeoutSec 3) {
        $ready = $true
        break
    }
    Start-Sleep -Milliseconds 700
}

if (-not $ready) {
    Write-Note "等待服务就绪超时（120 秒），请查看日志：$errLog"
    exit 1
}

Write-Note "服务就绪：$BaseUrl（首轮筛选可能仍在进行，页面会自动刷新）"
Write-Note "停止看板：powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Stop"
exit 0
