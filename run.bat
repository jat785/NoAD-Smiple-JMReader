@echo off
REM JMReader 一键启动（Windows）
REM 依赖一律装在项目内的 .venv，绝不污染全局 Python 环境。
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [1/2] 正在项目目录下创建虚拟环境 .venv ...
  python -m venv .venv || goto :fail
  echo [2/2] 正在安装依赖（只装进 .venv）...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

".venv\Scripts\python.exe" -m app
goto :eof

:fail
echo.
echo 安装失败。请确认已安装 Python 3.10 或更高版本，并且 python 在 PATH 中。
pause
