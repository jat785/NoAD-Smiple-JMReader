#!/usr/bin/env sh
# JMReader 一键启动（macOS / Linux）
# 依赖一律装在项目内的 .venv，绝不污染全局 Python 环境。
#
# 注意：本脚本在 Windows 上未实测（本项目的开发与验证环境是 Windows），
# 但换行符已由 .gitattributes 固定为 LF，可执行位也存进了 git，克隆后
# 直接用 ./run.sh 即可，不需要再 chmod。
set -e
cd "$(dirname "$0")"

PY=".venv/bin/python"

# 「venv 能用」不能只看解释器在不在。上一次 pip 装到一半失败、
# 或者把别人装好的 .venv 整个拷过来，都会留下一个能启动、但缺依赖的环境，
# 那种情况下报的 ModuleNotFoundError 完全指不到安装步骤。
# 所以让解释器自己去 import 关键依赖，import 得动才算真的装好。
if [ -x "$PY" ] && "$PY" -c "import fastapi, uvicorn, jmcomic" >/dev/null 2>&1; then
  exec "$PY" -m app
fi

if command -v python3 >/dev/null 2>&1; then
  PYBIN=python3
else
  PYBIN=python
fi

"$PYBIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo
  echo "需要 Python 3.10 或更高版本，当前是：$("$PYBIN" -V 2>&1)"
  echo "  macOS  可以 brew install python@3.12"
  echo "  Ubuntu 可以 sudo apt install python3 python3-venv"
  exit 1
}

if [ ! -x "$PY" ]; then
  echo
  echo "[1/3] 正在项目目录下创建虚拟环境 .venv ..."
  "$PYBIN" -m venv .venv || {
    echo
    echo "创建虚拟环境失败。Debian / Ubuntu 上通常是因为缺少 venv 模块，先装它："
    echo "    sudo apt install python3-venv"
    exit 1
  }
fi

echo "[2/3] 正在安装依赖（只装进 .venv）..."
"$PY" -m pip install --upgrade pip || {
  echo
  echo "升级 pip 失败。请确认当前网络可以访问 PyPI。"
  exit 1
}
"$PY" -m pip install -r requirements.txt || {
  echo
  echo "依赖安装失败。请确认当前网络可以访问 PyPI。"
  echo "依赖全部装在项目的 .venv 里，不会影响你的全局 Python 环境，可以放心重试。"
  exit 1
}

echo "[3/3] 启动 ..."
exec "$PY" -m app
