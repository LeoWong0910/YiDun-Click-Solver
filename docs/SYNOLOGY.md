# Synology NAS 一键部署

把本项目部署为群晖 DSM 7 **Container Manager** 上的常驻 HTTP 服务，局域网内用 `POST /solve` 调用识别流程。

## 前置条件

1. 群晖已安装 **Container Manager**（原 Docker 套件）
2. 控制面板 → 终端机和 SNMP → 启用 **SSH**
3. 部署账号能执行 `docker`（管理员，或已加入 `docker` 用户组）
4. 本机已安装 `ssh`；推荐同时安装 `rsync`

## 方式 A：本机一键脚本（推荐）

```bash
cp .env.example .env
# 编辑 .env：NAS_SSH、NAS_DIR、HOST_PORT、可选 YIDUN_API_TOKEN

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
- 解题：`curl -X POST http://<nas-ip>:8765/solve`

## 方式 B：Container Manager 图形界面

1. 用 File Station 在 `/docker`（或任意 volume）下新建文件夹 `yidun-click-solver`
2. 上传本仓库（至少包含 `Dockerfile`、`compose.yaml`、`requirements.txt`、`src/`、`tools/`、`captures/`、`results/`）
3. 打开 **Container Manager → 项目 → 新增**
4. 选择该文件夹，使用现有 `compose.yaml` 创建
5. 按需在环境变量里设置 `YIDUN_API_TOKEN`、`HOST_PORT`
6. 构建并启动

## API 说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 存活探测 |
| `GET` | `/` | 简短用法 |
| `POST` | `/solve` | 自动取参、识别、校验 |
| `GET` | `/solve` | 同上（query 参数，便于调试） |

可选请求体字段（JSON）：`captcha_id`、`dt`、`zone_id`、`fp`、`ir_token`、`max_attempts`、`get_only`、`points`、`output_detail` 等，会转成 CLI 参数。

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
- 每次 `/solve` 子进程调用现有 `tools/yidun_click_solver.py`
- `captures/`、`results/` 挂载到宿主机目录，方便在 File Station 查看产物
- 同进程内串行化解题，避免群晖低功耗 CPU 被并发打满

## 安全建议

- 仅在可信局域网暴露端口；不要把 `8765` 直接映射到公网
- 生产或共享局域网务必设置 `YIDUN_API_TOKEN`
- 本项目仅用于 CTF / 授权研究 / 本地沙箱，请遵守当地法律与目标站点条款
