#!/usr/bin/env bash
# 容器入口：api 先跑迁移再起 uvicorn；worker 等 backend healthy 后由 compose 拉起
set -e

case "$1" in
  api)
    uv run alembic upgrade head
    exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec uv run arq app.worker.WorkerSettings
    ;;
  *)
    exec "$@"
    ;;
esac
