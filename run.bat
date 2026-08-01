@echo off
REM AlphaGPT客户端启动器
REM pythonw = 无控制台；
REM 开发调试需看控制台日志时，直接在终端执行： python run.py
cd /d "%~dp0"
start "" pythonw run.py
