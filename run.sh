#!/usr/bin/env sh
# JMReader 一键启动（macOS / Linux）
# 依赖一律装在项目内的 .venv，绝不污染全局 Python 环境。
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo
  echo "[1/2] 正在项目目录下创建虚拟环境 .venv ..."
  if command -v python3 >/dev/null 2>&1; then
    python3 -m venv .venv
  else
    python -m venv .venv
  fi
  echo "[2/2] 正在安装依赖（只装进 .venv）..."
  ./.venv/bin/python -m pip install --upgrade pip
  ./.venv/bin/python -m pip install -r requirements.txt
fi

exec ./.venv/bin/python -m app
