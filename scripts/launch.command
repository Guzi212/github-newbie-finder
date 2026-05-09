#!/usr/bin/env bash
# 一键启动 GitHub 小白检索器：backend (8000) + Streamlit (8501)，并打开浏览器。
# 双击此文件即可。重复双击是安全的：已运行的服务不会被重启。

set -u

# 自解析脚本所在目录（兼容通过符号链接调用的情况）
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"
cd "$ROOT" || { echo "找不到项目目录：$ROOT"; exit 1; }

mkdir -p logs

port_busy() { lsof -ti :"$1" >/dev/null 2>&1; }

# --- backend (FastAPI :8000) ---
if port_busy 8000; then
  echo "[backend ] 已在 :8000 运行，跳过"
else
  echo "[backend ] 启动中…"
  bash scripts/run-backend.sh
fi

# --- frontend (Streamlit :8501) ---
if port_busy 8501; then
  echo "[streamlit] 已在 :8501 运行，跳过"
else
  echo "[streamlit] 启动中…"
  nohup .venv/bin/streamlit run streamlit_app.py \
    --server.port 8501 --server.headless true \
    >> logs/streamlit.log 2>&1 &
  echo "           日志：logs/streamlit.log"
fi

# 等 Streamlit 就绪后再开浏览器（最多 30s）
echo -n "[browser ] 等待就绪"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8501 >/dev/null 2>&1; then
    echo " ✓"
    break
  fi
  echo -n "."
  sleep 1
done

open "http://localhost:8501"

echo
echo "✅ 已打开 http://localhost:8501"
echo "   可直接关闭此窗口；服务在后台继续运行。"
echo "   如需停止： pkill -f 'uvicorn app.main' ; pkill -f 'streamlit run streamlit_app'"
