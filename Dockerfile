# 后端镜像：api 与 worker 共用（含 crawl4ai 所需的 Chromium）
FROM python:3.12-slim-bookworm

# apt 换腾讯云源加速（playwright --with-deps 会装系统库）
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

# uv 管理依赖（pip 安装 uv 本体，避免依赖 ghcr.io）
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple uv

# 依赖层：先拷 lockfile 利用构建缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# crawl4ai 渲染层：本地 Chromium
RUN uv run playwright install --with-deps chromium

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
