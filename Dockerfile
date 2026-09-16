# 后端镜像：api 与 worker 共用（含 crawl4ai 所需的 Chromium）
FROM python:3.12-slim-bookworm

# apt 换腾讯云源加速（playwright --with-deps 会装系统库）
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

# 依赖：pip 直接装（uv.lock 锁定 pypi.org URL，国内服务器构建慢，故绕开 uv）
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic-settings>=2.4" \
    "sqlalchemy[asyncio]>=2.0" "asyncpg>=0.29" "alembic>=1.13" "redis>=5.0" \
    "httpx>=0.27" "ddgs>=9.16.0" "crawl4ai>=0.9.3" "trafilatura>=2.2.0" \
    "langgraph>=1.0" "langgraph-checkpoint-postgres>=2.0" "psycopg[binary,pool]>=3.2" "instructor>=1.17" \
    "openai>=2.0,<3.0" "arq>=0.26" "playwright>=1.62.0" "markdown>=3.10.3"

# crawl4ai 渲染层：本地 Chromium（npmmirror 加速浏览器下载）
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright
RUN playwright install --with-deps chromium

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
