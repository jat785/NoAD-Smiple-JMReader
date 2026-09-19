@echo off
REM JMReader 一键启动（Windows）
REM 依赖一律装在项目内的 .venv，绝不污染全局 Python 环境。
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

REM 「venv 能用」不能只看解释器在不在。上一次 pip 装到一半失败、
REM 或者把别人装好的 .venv 整个拷过来，都会留下一个能启动、但缺依赖的环境，
REM 那种情况下报的 ModuleNotFoundError 完全指不到安装步骤。
REM 所以让解释器自己去 import 关键依赖，import 得动才算真的装好。
if not exist "%PY%" goto :create
"%PY%" -c "import fastapi, uvicorn, jmcomic" >nul 2>&1
if not errorlevel 1 goto :run

echo.
echo [1/2] .venv 已存在但依赖不完整，正在补装 ...
"%PY%" -m pip install -r requirements.txt || goto :fail
goto :run

:create
echo.
echo [1/2] 正在项目目录下创建虚拟环境 .venv ...
python -m venv .venv || goto :nopython
echo [2/2] 正在安装依赖（只装进 .venv）...
"%PY%" -m pip install --upgrade pip || goto :fail
"%PY%" -m pip install -r requirements.txt || goto :fail

:run
"%PY%" -m app
goto :eof

:nopython
echo.
echo 无法创建虚拟环境。请确认：
echo   1. 已安装 Python 3.10 或更高版本（https://www.python.org/downloads/）
echo   2. 安装时勾选了 "Add python.exe to PATH"
echo   3. 在命令行里直接执行 python -V 能看到版本号
echo.
pause
exit /b 1

:fail
echo.
echo 依赖安装失败。请确认当前网络可以访问 PyPI。
echo 依赖全部装在项目的 .venv 里，不会影响你的全局 Python 环境，可以放心重试。
echo.
pause
exit /b 1
