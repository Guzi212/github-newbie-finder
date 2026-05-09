#!/usr/bin/env bash
# 双击此文件停止后台服务：backend (uvicorn) + Streamlit。
# 仅匹配本项目特征字符串，不会误杀其它 uvicorn / streamlit 项目。

set -u

echo "[stop] 关闭 backend (uvicorn app.main) …"
if pkill -f "uvicorn app.main" 2>/dev/null; then
  echo "       已发送 SIGTERM"
else
  echo "       未在运行"
fi

echo "[stop] 关闭 streamlit (streamlit_app) …"
if pkill -f "streamlit run streamlit_app" 2>/dev/null; then
  echo "       已发送 SIGTERM"
else
  echo "       未在运行"
fi

sleep 1

# 兜底：端口仍被占用就强杀
for port in 8000 8501; do
  pids=$(lsof -ti :"$port" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "[stop] :$port 仍被占用 → SIGKILL: $pids"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
done

# 再确认一次
sleep 1
still=""
for port in 8000 8501; do
  if lsof -ti :"$port" >/dev/null 2>&1; then
    still+=" :$port"
  fi
done

echo
if [ -z "$still" ]; then
  echo "✅ 已停止 backend (8000) 和 Streamlit (8501)。可关闭此窗口。"
else
  echo "⚠️ 端口仍被占用：$still ， 可手动检查：lsof -i$still"
fi
