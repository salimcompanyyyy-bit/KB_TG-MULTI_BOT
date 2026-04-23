# Один раз запустите этот скрипт правой кнопкой -> "Выполнить с помощью PowerShell"
# или в PowerShell: powershell -ExecutionPolicy Bypass -File ".\Создать_ярлык_на_рабочем_столе.ps1"

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$batPath = Join-Path $projectRoot "run_bot.bat"

if (-not (Test-Path -LiteralPath $batPath)) {
    Write-Host "Не найден run_bot.bat в папке проекта." -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Kapital Assets Bot.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $batPath
$shortcut.WorkingDirectory = $projectRoot
$shortcut.WindowStyle = 1
$shortcut.Description = "Запуск Telegram-бота Kapital Assets"
$shortcut.Save()

Write-Host "Готово: ярлык на рабочем столе -> $lnkPath" -ForegroundColor Green
