@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
title Hodim bot TUNNEL - BU OYNANI YOPMANG
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% tunnel.py
pause
