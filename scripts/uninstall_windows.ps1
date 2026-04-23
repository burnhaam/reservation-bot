# 숙박업 봇 작업 스케줄러 등록 제거.
#
# 관리자 권한 PowerShell에서 실행:
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows.ps1

$ErrorActionPreference = 'Continue'

$current = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[오류] 관리자 권한 필요." -ForegroundColor Red
    exit 1
}

foreach ($name in @("ReservationBot", "CoupangCDPChrome", "DiscordBot")) {
    try {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
        Write-Host "[OK] $name 제거 완료"
    } catch {
        Write-Host "[스킵] $name — 등록되어 있지 않음"
    }
}

Write-Host ""
Write-Host "작업 스케줄러 등록 제거 완료."
