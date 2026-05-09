#!/usr/bin/env bash
# 启动 FastAPI 后端：杀掉旧进程，带 INFO 日志，stdout/stderr 落盘到 logs/backend.log。
# 用法：
#   bash scripts/run-backend.sh         # 后台运行（默认）
#   bash scripts/run-backend.sh fg      # 前台运行，方便看实时日志
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  echo "找不到 .venv，请先创建虚拟环境并安装 requirements.txt" >&2
  exit 1
fi

LOG="logs/backend.log"
mkdir -p logs

# 杀掉占用 8000 端口的旧进程（包括 --reload 派生出来的 worker），避免冲突
pkill -f "uvicorn app.main" 2>/dev/null || true
if command -v lsof >/dev/null 2>&1; then
  lsof -ti :8000 | xargs -r kill -9 2>/dev/null || true
fi
sleep 1

CMD=(.venv/bin/uvicorn app.main:app
     --host 127.0.0.1 --port 8000
     --reload --log-level info)

mode="${1:-bg}"
if [ "$mode" = "fg" ]; then
  exec "${CMD[@]}" 2>&1 | tee -a "$LOG"
fi

# 后台运行：日志追加到 logs/backend.log
nohup "${CMD[@]}" >> "$LOG" 2>&1 &
PID=$!
echo "backend 已启动 (pid=$PID)，日志：$LOG"
echo "  实时跟踪：tail -f $LOG"
echo "  健康检查：curl http://127.0.0.1:8000/healthz"
