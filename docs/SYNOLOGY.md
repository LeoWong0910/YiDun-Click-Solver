# Synology NAS 一键部署

把本项目部署为群晖 DSM 7 **Container Manager** 上的常驻 HTTP 服务。部署后仍保留原 CLI 能力，并提供：

1. **验证码自动识别**：`POST /solve`（完整闭环）与 `POST /recognize`（仅本地 CV）
2. **微信 / 邮件通知推送**：识别成功或失败后自动推送

## 前置条件

1. 群晖已安装 **Container Manager**（原 Docker 套件）
2. 控制面板 → 终端机和 SNMP → 启用 **SSH**
3. 部署账号能执行 `docker`（管理员，或已加入 `docker` 用户组）
4. 本机已安装 `ssh`；推荐同时安装 `rsync`

## 方式 A：本机一键脚本（推荐）

```bash
cp .env.example .env
# 编辑 .env：NAS_SSH、NAS_DIR、HOST_PORT、可选 YIDUN_API_TOKEN
# 以及通知渠道：SERVERCHAN_SENDKEY / PUSHPLUS_TOKEN / SMTP_* 等

chmod +x scripts/deploy-synology.sh
./scripts/deploy-synology.sh
# 或: ./scripts/deploy-synology.sh admin@192.168.1.50
```

脚本会：

1. 把项目同步到 NAS 的 `NAS_DIR`（默认 `/volume1/docker/yidun-click-solver`）
2. 写入远程 `.env`
3. 在 NAS 上执行 `docker compose up -d --build`

完成后访问：

- 健康检查：`http://<nas-ip>:8765/health`
- 自动识别：`curl -X POST http://<nas-ip>:8765/solve`
- 通知测试：`curl -X POST http://<nas-ip>:8765/notify/test`

## 方式 B：Container Manager 图形界面

1. 用 File Station 在 `/docker`（或任意 volume）下新建文件夹 `yidun-click-solver`
2. 上传本仓库（至少包含 `Dockerfile`、`compose.yaml`、`requirements.txt`、`src/`、`tools/`、`captures/`、`results/`）
3. 打开 **Container Manager → 项目 → 新增**
4. 选择该文件夹，使用现有 `compose.yaml` 创建
5. 按需设置 `YIDUN_API_TOKEN`、`HOST_PORT`、通知相关环境变量
6. 构建并启动

## 原 CLI 功能（部署后仍可用）

容器内原命令不变：

```bash
docker exec -it yidun-click-solver \
  python tools/yidun_click_solver.py --json-out results/live.json

# 仅取图
docker exec -it yidun-click-solver \
  python tools/yidun_click_solver.py --get-only

# CLI 也可推送通知
docker exec -it yidun-click-solver \
  python tools/yidun_click_solver.py --notify
```

`/solve` 默认会带上 `--auto`，保证与本地一键识别行为一致。

## API 说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 存活探测 |
| `GET` | `/` | 简短用法 |
| `POST` | `/solve` | 自动取参、识别、校验（可顺带通知） |
| `GET` | `/solve` | 同上（query 参数，便于调试） |
| `POST` | `/recognize` | 仅本地 CV：`image_path` 或 `image_base64` |
| `POST` | `/notify/test` | 发送一条测试通知 |

### 自动识别示例

完整闭环：

```bash
curl -X POST "http://<nas-ip>:8765/solve" \
  -H "Content-Type: application/json" \
  -d '{"max_attempts": 5}'
```

只识别本地图片（容器内 `captures/` 下的文件名即可）：

```bash
curl -X POST "http://<nas-ip>:8765/recognize" \
  -H "Content-Type: application/json" \
  -d '{"image_path":"captcha_xxx.jpg"}'
```

### 微信 / 邮件通知

在 `.env` 中配置任一渠道即可自动启用（也可显式 `NOTIFY_ENABLED=true`）：

| 渠道 | 环境变量 | 说明 |
| --- | --- | --- |
| Server酱 | `SERVERCHAN_SENDKEY` | 微信推送（SCT key） |
| PushPlus | `PUSHPLUS_TOKEN` | 微信推送 |
| 企业微信机器人 | `WECOM_WEBHOOK_URL` | 群机器人 webhook |
| 通用 Webhook | `NOTIFY_WEBHOOK_URL` | JSON POST |
| 邮件 | `SMTP_HOST` + `NOTIFY_EMAIL_TO` 等 | SMTP |

`NOTIFY_ON=success,failure`（默认）控制何时推送；可改为 `always`。

测试推送：

```bash
curl -X POST "http://<nas-ip>:8765/notify/test" \
  -H "Content-Type: application/json" \
  -d '{"title":"测试","body":"群晖通知正常"}'
```

单次请求可覆盖：`{"notify": false}` 跳过推送。

若设置了 `YIDUN_API_TOKEN`：

```bash
curl -X POST "http://<nas-ip>:8765/solve" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"max_attempts": 5}'
```

## 常用运维

在 NAS SSH 会话中：

```bash
cd /volume1/docker/yidun-click-solver
docker compose ps
docker compose logs -f api
docker compose restart
docker compose down
```

重新一键部署（本机）：

```bash
./scripts/deploy-synology.sh
```

## 架构说明

- 镜像内启动 `tools/nas_api_server.py`（标准库 HTTP 服务）
- `/solve` 子进程调用现有 `tools/yidun_click_solver.py`（保留原自动识别）
- `/recognize` 直接调用 `src/yidun_vision.py` 本地 CV
- 通知由 `src/notify.py` 统一发送（邮件 / Server酱 / PushPlus / 企微 / webhook）
- `captures/`、`results/` 挂载到宿主机目录，方便在 File Station 查看产物
- 同进程内串行化解题，避免群晖低功耗 CPU 被并发打满

## 安全建议

- 仅在可信局域网暴露端口；不要把 `8765` 直接映射到公网
- 生产或共享局域网务必设置 `YIDUN_API_TOKEN`
- 本项目仅用于 CTF / 授权研究 / 本地沙箱，请遵守当地法律与目标站点条款
