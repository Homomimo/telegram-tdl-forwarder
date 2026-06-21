# Telegram 转发机器人（Docker + TDL）

一个基于 Telegram Bot 的文件转发工具，可将消息或文件转发到指定私人频道，并通过 [TDL](https://github.com/iyear/tdl) 提升大文件下载与传输效率。

支持 Docker 容器化部署，适合需要稳定转存 Telegram 文件、归档资源或管理转发历史的场景。

> 说明：本项目支持处理禁止复制、禁止转发的文件。请确保仅在合法、合规且已获得授权的范围内使用。

---

## 功能特性

- **TDL 加速传输**  
  调用宿主机已安装的 `tdl` 工具，高效下载 Telegram 文件。

- **自动转发文件**  
  向机器人发送文件后，可自动下载并转发到指定频道。

- **转发历史记录**  
  自动保存转发记录，便于后续查询与管理。

- **文件持久化存储**  
  通过 Docker Volume 将数据保存到宿主机，避免容器重建导致数据丢失。

- **管理员控制**  
  支持管理员查看历史、清理文件等管理操作。

- **Docker 部署**  
  支持 Docker 与 Docker Compose，一键构建并运行。

---

## 环境要求

部署前请确保宿主机已安装并配置以下组件：

- [Docker](https://docs.docker.com/engine/install/)
- [Docker Compose](https://docs.docker.com/compose/install/)
- [TDL](https://docs.iyear.me/tdl/zh/getting-started/installation/)
- Telegram API 凭证
- Telegram Bot Token
- 管理员 Telegram 用户 ID
- 接收文件的频道 ID

---

## 安装 TDL

执行以下命令安装 TDL：

```bash
curl -sSL https://docs.iyear.me/tdl/install.sh | sudo bash
```

安装完成后，使用二维码登录：

```bash
tdl login -T qr
```

扫码后，如账号开启了两步验证，请根据提示输入二步验证密码。

确认 `tdl` 具有执行权限：

```bash
chmod +x /usr/local/bin/tdl
```

配置 TDL 目录权限：

```bash
chmod -R 755 ~/.tdl
```

---

## 安装 Docker

```bash
curl -fsSL get.docker.com -o get-docker.sh && sh get-docker.sh
```

---

## 安装 Docker Compose

下载官方二进制文件：

```bash
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
  -o /usr/local/bin/docker-compose
```

添加执行权限：

```bash
sudo chmod +x /usr/local/bin/docker-compose
```

验证安装结果：

```bash
docker-compose --version
```

---

## 获取必要配置

### Telegram API 凭证

前往以下地址申请：

[Telegram API](https://my.telegram.org/auth)

需要获取：

- `API_ID`
- `API_HASH`

### Bot Token

通过 Telegram 中的 [@BotFather](https://t.me/BotFather) 创建机器人，并获取：

- `BOT_TOKEN`

### 用户 ID / 频道 ID

可通过 [@userinfobot](https://t.me/userinfobot) 获取账号 ID。

频道 ID 获取后，请去除 `-100` 前缀。

示例：

```text
原始频道 ID：-1001234567890
配置时填写：1234567890
```

---

## 快速部署

### 步骤 1：克隆仓库

```bash
git clone https://github.com/Homomimo/telegram-tdl-forwarder.git
cd telegram-tdl-forwarder
```

---

### 步骤 2：编辑 Docker Compose 配置

打开配置文件：

```bash
nano docker-compose.yml
```

参考配置如下：

```yaml
services:
  telegram-bot:
    build: .
    container_name: telegram-tdl-forwarder
    restart: "no"
    volumes:
      - ./forward_history.json:/app/forward_history.json
      - /usr/local/bin/tdl:/usr/local/bin/tdl
      - ~/.tdl:/root/.tdl
    environment:
      - TZ=Asia/Shanghai
      - API_ID=你的_API_ID
      - API_HASH=你的_API_HASH
      - BOT_TOKEN=你的_BOT_TOKEN
      - ADMIN_IDS=你的_Telegram用户ID
      - FORWARD_TO_CHAT_ID=接收频道ID
```

> 注意：请勿将真实的 `API_HASH`、`BOT_TOKEN` 等敏感信息公开上传到 GitHub 或分享给他人。

---

### 步骤 3：启动服务

```bash
docker-compose build --no-cache && docker-compose up -d
```

---

### 步骤 4：查看运行日志

```bash
docker-compose logs -f
```

如果日志中没有报错，并且机器人可以正常响应消息，则说明部署成功。

---

## 使用指南

### 基础使用

1. 启动机器人服务。
2. 在 Telegram 中向机器人发送任意文件。
3. 机器人会自动下载文件。
4. 下载完成后，文件会被转发到配置的目标频道。

### 查看帮助

向机器人发送：

```text
/start
```

即可查看帮助菜单。

---

## 常用命令

### 启动服务

```bash
docker-compose up -d
```

### 停止服务

```bash
docker-compose down
```

### 重启服务

```bash
docker-compose restart
```

### 查看日志

```bash
docker-compose logs -f
```

### 重新构建并启动

```bash
docker-compose build --no-cache && docker-compose up -d
```

---

## 数据持久化说明

当前配置会将以下内容挂载到容器中：

```yaml
volumes:
  - ./forward_history.json:/app/forward_history.json
  - /usr/local/bin/tdl:/usr/local/bin/tdl
  - ~/.tdl:/root/.tdl
```

说明：

- `forward_history.json`：保存转发历史记录。
- `/usr/local/bin/tdl`：挂载宿主机的 TDL 可执行文件。
- `~/.tdl`：挂载宿主机的 TDL 登录会话与配置目录。

---

## 注意事项

- 请确保宿主机已完成 `tdl login` 登录。
- 请确保容器内可以访问 `/usr/local/bin/tdl`。
- 请确保 `~/.tdl` 权限正确，否则可能导致 TDL 无法读取登录状态。
- 频道 ID 配置时需要去除 `-100` 前缀。
- 不要泄露 `API_HASH`、`BOT_TOKEN` 等敏感信息。
- 如需长期运行，建议将 `restart` 改为：

```yaml
restart: unless-stopped
```

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
