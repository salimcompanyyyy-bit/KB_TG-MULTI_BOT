@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Kapital Assets Bot
echo Запуск бота... Окно можно свернуть, не закрывайте, пока бот нужен.
echo Остановка: Ctrl+C
echo.
python bot.py
echo.
echo Бот остановлен (код %ERRORLEVEL%). Нажмите любую клавишу, чтобы закрыть окно...
pause >nul
