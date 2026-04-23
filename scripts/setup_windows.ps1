# 숙박업 봇 자동 시작 등록 (Windows 작업 스케줄러).
#
# 관리자 권한 PowerShell에서 실행:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#
# 등록되는 세 작업:
#   1. ReservationBot     : 로그온 시 시작 + 5분마다 반복, pythonw.exe로 콘솔 없이 백그라운드
#   2. CoupangCDPChrome   : 로그온 시 CDP 모드 Chrome 자동 실행 (Akamai 우회 필수)
#   3. DiscordBot         : 로그온 시 Discord 봇 시작 (슬래시 커맨드 수신용, 상시 유지)
#
# 제거: scripts\uninstall_windows.ps1

$ErrorActionPreference = 'Stop'

# ── 관리자 권한 체크 ─────────────────────────────────
$current = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[오류] 관리자 권한 필요." -ForegroundColor Red
    Write-Host "       PowerShell을 관리자로 실행한 뒤 재시도하세요."
    Write-Host ""
    Write-Host "  1) 시작 메뉴 → 'PowerShell' 검색"
    Write-Host "  2) 오른클릭 → '관리자 권한으로 실행'"
    Write-Host "  3) cd '$PSScriptRoot\..'"
    Write-Host "  4) powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1"
    exit 1
}

# ── 경로 자동 감지 ───────────────────────────────────
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$MainPy      = Join-Path $ProjectRoot "main.py"
$CdpPy       = Join-Path $ProjectRoot "scripts\coupang_chrome_cdp_start.py"
$DiscordPy   = Join-Path $ProjectRoot "scripts\discord_bot_start.py"

if (-not (Test-Path $MainPy)) {
    Write-Host "[오류] main.py 를 찾지 못함: $MainPy" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $CdpPy)) {
    Write-Host "[오류] coupang_chrome_cdp_start.py 를 찾지 못함: $CdpPy" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $DiscordPy)) {
    Write-Host "[오류] discord_bot_start.py 를 찾지 못함: $DiscordPy" -ForegroundColor Red
    exit 1
}

# Python 실행 파일 — pythonw.exe 선호 (콘솔 창 숨김)
$PyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PyCmd) {
    Write-Host "[오류] python 명령을 PATH에서 찾지 못함." -ForegroundColor Red
    exit 1
}
$PyDir = Split-Path -Parent $PyCmd.Path
$PyW   = Join-Path $PyDir "pythonw.exe"
if (-not (Test-Path $PyW)) {
    Write-Host "[경고] pythonw.exe 없음 — python.exe로 폴백 (콘솔 창 뜸)" -ForegroundColor Yellow
    $PyW = $PyCmd.Path
}

Write-Host ("=" * 60)
Write-Host " 숙박업 봇 자동 시작 등록"
Write-Host ("=" * 60)
Write-Host " ProjectRoot : $ProjectRoot"
Write-Host " Python      : $PyW"
Write-Host ""

# ── 공통 설정 ────────────────────────────────────────
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# ── 1) ReservationBot ────────────────────────────────
# pythonw.exe로 main.py 1회 실행. Task Scheduler가 5분마다 트리거.
$actionBot = New-ScheduledTaskAction `
    -Execute $PyW `
    -Argument "`"$MainPy`"" `
    -WorkingDirectory $ProjectRoot

$triggerBotLogon  = New-ScheduledTaskTrigger -AtLogOn
# RepetitionDuration 최대값은 Task Scheduler XML 스키마 상 9999일. 약 27년이면 사실상 영구.
$triggerBotRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

try { Unregister-ScheduledTask -TaskName "ReservationBot" -Confirm:$false -ErrorAction SilentlyContinue } catch {}

Register-ScheduledTask `
    -TaskName "ReservationBot" `
    -Action $actionBot `
    -Trigger @($triggerBotLogon, $triggerBotRepeat) `
    -Settings $settings `
    -Principal $principal `
    -Description "숙박업 예약 자동화 + 재고 자동주문 (5분마다 + 로그온 시)" `
    -Force | Out-Null

Write-Host "[OK] ReservationBot 등록 (로그온 시 + 5분마다 반복)"

# ── 2) CoupangCDPChrome ──────────────────────────────
# 로그온 시 CDP 모드 Chrome 띄움 — 쿠팡 자동주문의 Akamai 우회 필수 조건.
$actionCdp = New-ScheduledTaskAction `
    -Execute $PyW `
    -Argument "`"$CdpPy`"" `
    -WorkingDirectory $ProjectRoot

$triggerCdp = New-ScheduledTaskTrigger -AtLogOn

try { Unregister-ScheduledTask -TaskName "CoupangCDPChrome" -Confirm:$false -ErrorAction SilentlyContinue } catch {}

Register-ScheduledTask `
    -TaskName "CoupangCDPChrome" `
    -Action $actionCdp `
    -Trigger $triggerCdp `
    -Settings $settings `
    -Principal $principal `
    -Description "쿠팡 자동주문용 CDP Chrome (port 9222) - 로그온 시 자동 실행" `
    -Force | Out-Null

Write-Host "[OK] CoupangCDPChrome 등록 (로그온 시)"

# ── 3) DiscordBot ────────────────────────────────────
# 로그온 시 Discord 봇 시작 — 슬래시 커맨드 수신(/approve, /status 등) 상시 유지.
# discord.py가 내부적으로 Gateway 재연결 처리하므로 한 번만 띄우면 됨.
$actionDiscord = New-ScheduledTaskAction `
    -Execute $PyW `
    -Argument "`"$DiscordPy`"" `
    -WorkingDirectory $ProjectRoot

$triggerDiscord = New-ScheduledTaskTrigger -AtLogOn

# Discord 봇은 상시 실행이라 ExecutionTimeLimit 해제 + 재시작 정책 추가
$settingsBot = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 99

try { Unregister-ScheduledTask -TaskName "DiscordBot" -Confirm:$false -ErrorAction SilentlyContinue } catch {}

Register-ScheduledTask `
    -TaskName "DiscordBot" `
    -Action $actionDiscord `
    -Trigger $triggerDiscord `
    -Settings $settingsBot `
    -Principal $principal `
    -Description "Discord 봇 (슬래시 커맨드 수신) - 로그온 시 상시 실행" `
    -Force | Out-Null

Write-Host "[OK] DiscordBot 등록 (로그온 시, 상시)"

Write-Host ""
Write-Host ("=" * 60)
Write-Host " 등록 완료"
Write-Host ("=" * 60)
Write-Host ""
Write-Host " 수동 실행:"
Write-Host "   Start-ScheduledTask -TaskName ReservationBot"
Write-Host "   Start-ScheduledTask -TaskName CoupangCDPChrome"
Write-Host "   Start-ScheduledTask -TaskName DiscordBot"
Write-Host ""
Write-Host " 상태 확인:"
Write-Host "   Get-ScheduledTask -TaskName ReservationBot, CoupangCDPChrome, DiscordBot | Select TaskName, State, LastRunTime"
Write-Host ""
Write-Host " 제거:"
Write-Host "   powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows.ps1"
Write-Host ""
Write-Host " 참고: 모든 작업은 pythonw.exe로 백그라운드 실행 — 콘솔 창 뜨지 않음."
Write-Host "       로그는 $ProjectRoot\logs\ 에 파일로 기록됩니다."
Write-Host "       DiscordBot은 5분 간격으로 최대 99회 자동 재시작 (크래시 복구)."
Write-Host ""
