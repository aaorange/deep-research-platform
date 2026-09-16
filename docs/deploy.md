# 腾讯云部署文档

## 服务器

| 项 | 值 |
|---|---|
| 机型 | 腾讯云轻量应用服务器 4核 / 4G / 59G |
| 公网 IP | 118.89.171.24 |
| 系统 | Ubuntu 24.04 + Docker 29 + compose v2 |
| 部署目录 | `/home/ubuntu/deep-research-platform` |
| 访问入口 | `http://118.89.171.24:8080` |
| 共存项目 | kb-platform（RAG 知识库，占用 80 端口，注意别动它的容器） |

## 架构

`docker compose` 一条命令拉起 5 个服务：

- **frontend**（nginx）：唯一对外端口 `8080:80`，托管前端静态页，`/api` 反代到 backend（SSE 关缓冲）
- **backend**（uvicorn）：entrypoint 先跑 `alembic upgrade head` 再启动；健康检查 `/api/health`
- **worker**（arq）：执行 LangGraph 研究主图；`depends_on: backend healthy` 等迁移完成再启动
- **postgres / redis**：仅 docker 内网，不暴露公网端口；pgdata 命名卷持久化

密钥通过 `/home/ubuntu/deep-research-platform/.env` 注入（权限 600，不进镜像不进仓库）。

## 国内构建加速（已配置）

- Docker 镜像加速：`/etc/docker/daemon.json` → `mirror.ccs.tencentyun.com`（腾讯云内网）
- pip / apt：腾讯云镜像 `mirrors.cloud.tencent.com`
- npm：`registry.npmmirror.com`；playwright 浏览器下载走 npmmirror binary 镜像
- 注意：`uv.lock` 锁定 pypi.org 直链，国内服务器构建过慢，故生产 Dockerfile 用 pip 安装

## 常用运维命令

```bash
ssh ubuntu@118.89.171.24
cd /home/ubuntu/deep-research-platform

sudo docker compose ps                 # 查看状态
sudo docker compose logs -f backend    # 看日志（backend / worker / frontend）
sudo docker compose restart worker     # 重启单个服务
sudo docker compose up -d --build      # 改代码后重建并滚动更新
sudo docker compose down               # 全部停止（数据卷保留）
```

## 更新部署流程

代码推到 GitHub 后，服务器侧更新（服务器上是归档解压，无 .git）：

```bash
# 本地打包并上传
git archive --format=tar.gz -o deploy.tar.gz HEAD
scp deploy.tar.gz ubuntu@118.89.171.24:/home/ubuntu/

# 服务器上覆盖并重建
cd /home/ubuntu/deep-research-platform
tar xzf /home/ubuntu/deploy.tar.gz -C . --overwrite
sudo docker compose up -d --build
```

## 待办（安全加固）

- [ ] 腾讯云控制台 → 轻量服务器 → 防火墙：确认放行 **8080**（22/80/443 默认已放行）
- [ ] SSH 改密钥登录并禁用密码登录（当前为密码登录）
- [ ] 备案 + 域名 + HTTPS（可选路线，备案约 2-3 周）
