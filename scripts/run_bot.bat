@echo off
cd /d "%~dp0.."
title Kapital Assets Bot
echo Starting bot... Keep this window open while the bot runs.
echo Stop: Ctrl+C
echo.
python src\bot.py
echo.
echo Stopped. Exit code: %ERRORLEVEL%
pause >nul
