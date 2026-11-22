@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
title Hodim bot - BU OYNANI YOPMANG

where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python topilmadi. https://www.python.org/downloads/ dan o'rnating
  echo va o'rnatishda "Add python.exe to PATH" belgisini qo'ying.
  pause
  exit /b
)

:loop
echo Bot ishga tushmoqda... Dashboard: http://localhost:8080/dashboard
%PY% bot.py
echo Bot to'xtadi. 10 soniyadan keyin qayta ishga tushadi...
timeout /t 10 >nul
goto loop
