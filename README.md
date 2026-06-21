# Telegram 转发机器人（Docker + TDL）

一个基于 Telegram Bot 的文件转发工具，可将消息或文件转发到指定私人频道，并通过 [TDL](https://github.com/iyear/tdl) 提升大文件下载与传输效率。

支持 Docker 容器化部署，适合需要稳定转存 Telegram 文件、归档资源或管理转发历史的场景。

> 说明：本项目支持处理禁止复制、禁止转发的文件。请确保仅在合法、合规且已获得授权的范围内使用。

---

## 功能特性

- **TDL 加速传输**  
  调用宿主机已安装的 `tdl` 工具，高效下载 Telegram 文件。

- **自动转发文件**  
  向机器人发送文件、消息链接或转发消息后，可自动加入队列并转发到指定频道。

- **用户账号监听源频道**  
  可使用 Telethon 用户账号 session 监听源频道新消息，bot 只负责接收命令和发送状态消息。

- **自动监听频道转发**  
  配置 `MONITOR_CHAT_IDS` 后，源频道出现新消息时可自动转发到目标频道。

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
- 需要自动监听时：源频道 ID
- 需要用户账号监听时：Telethon 用户账号 session

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

目标频道 `FORWARD_TO_CHAT_ID` 建议填写 tdl 可识别的裸 ID，也就是去除 `-100` 前缀后的数字。

示例：

```text
原始频道 ID：-1001234567890
FORWARD_TO_CHAT_ID 配置：1234567890
```

自动监听源频道 `MONITOR_CHAT_IDS` 只填写源频道裸 ID，不要加 `-100` 前缀。多个频道用英文逗号分隔。

示例：

```text
MONITOR_CHAT_IDS=1234567890,9876543210
```

也可以先启动 bot 后，在管理员会话里使用：

```text
/chatid
/monitor add 1234567890
/monitor set 1234567890,9876543210
```

`/monitor` 命令只修改当前运行中的内存配置；容器重启后仍以 `docker-compose.yml` 或 `.env` 中的 `MONITOR_CHAT_IDS` 为准。

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
      - MONITOR_CHAT_IDS=源频道裸ID
      - USER_MONITOR_ENABLED=1
      - USER_SESSION_STRING=填入gen-session输出的字符串

  gen-session:
    build: .
    container_name: telegram-tdl-forwarder-gen-session
    profiles:
      - tools
    volumes:
      - ./gen_telethon_session.py:/app/gen_telethon_session.py
    environment:
      - TZ=Asia/Shanghai
      - API_ID=你的_API_ID
      - API_HASH=你的_API_HASH
    command: python3 -u gen_telethon_session.py
    stdin_open: true
    tty: true
```

> 注意：请勿将真实的 `API_HASH`、`BOT_TOKEN`、`USER_SESSION_STRING` 等敏感信息公开上传到 GitHub 或分享给他人。

---

### 步骤 3：生成 Telethon 用户账号 session（可选但推荐）

如果只手动给 bot 发送链接或转发消息，可以跳过本步骤。

如果需要自动监听源频道新消息，推荐生成 `USER_SESSION_STRING`，让用户账号负责监听源频道。这样源频道不需要把 bot 设为管理员，只要这个用户账号已经加入源频道即可。

运行一次性服务：

```bash
docker compose run --rm gen-session
```

旧版 Docker Compose 命令也可以使用：

```bash
docker-compose run --rm gen-session
```

按提示输入：

- 手机号
- Telegram 验证码
- 二步验证密码，如果账号开启了 2FA

成功后会输出一行：

```text
USER_SESSION_STRING=一大串字符串
```

将这整行中的字符串填回 `docker-compose.yml`：

```yaml
- USER_SESSION_STRING=一大串字符串
```

`USER_SESSION_STRING` 等同于该 Telegram 用户账号的登录凭证，请妥善保存，不要泄露。

---

### 步骤 4：启动服务

```bash
docker-compose build --no-cache && docker-compose up -d
```

---

### 步骤 5：查看运行日志

```bash
docker-compose logs -f
```

如果日志中没有报错，并且机器人可以正常响应消息，则说明部署成功。

如果启用了用户账号监听，日志中应能看到：

```text
用户账号监听已启动
源频道新消息将由用户账号 Telethon client 监听，bot 只处理命令和状态消息
```

如果没有配置 `USER_SESSION_STRING` 或 session 无效，程序会回退到 bot 推送监听。此时 bot 必须是源频道管理员，否则 Telegram 不会把频道新消息推送给 bot。

---

## 使用指南

### 基础使用

1. 启动机器人服务。
2. 在 Telegram 中向机器人发送消息链接、转发频道消息，或直接发送支持的文件消息。
3. 机器人会将任务加入队列。
4. 处理完成后，消息会被转发到配置的目标频道。

### 自动监听源频道

配置 `MONITOR_CHAT_IDS` 和 `USER_SESSION_STRING` 后，源频道有新消息时会自动加入转发队列。

推荐配置：

```yaml
- MONITOR_CHAT_IDS=1234567890
- USER_MONITOR_ENABLED=1
- USER_SESSION_STRING=一大串字符串
```

权限要求：

- 使用用户账号监听时：生成 session 的用户账号必须已经加入源频道。
- 回退到 bot 监听时：bot 必须是源频道管理员。
- tdl 登录账号需要有源频道读取权限。
- bot 和 tdl 登录账号需要有目标频道发帖权限。

### 查看帮助

向机器人发送：

```text
/start
```

即可查看帮助菜单。

### 常用 bot 命令

```text
/forwardto <频道ID>       设置目标频道
/showto                  查看当前目标频道
/chatid                  查看当前聊天/频道 ID，用于配置监听源频道
/monitor                 查看当前监听源频道列表
/monitor add <裸ID>       添加监听源频道
/monitor del <裸ID>       删除监听源频道
/monitor set <ID1,ID2>    覆盖监听源频道列表
/monitor clear           清空监听源频道列表
/queue                   查看队列状态
/flog                    查看最近转发记录
/flog clear              清空转发记录
/export_range <来源> <起始ID> <结束ID>  批量导出并转发消息范围
```

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
- 如需自动监听源频道，建议先通过 `docker compose run --rm gen-session` 生成 `USER_SESSION_STRING`。
- 使用用户账号监听时，生成 session 的用户账号必须已加入源频道。
- `USER_SESSION_STRING` 是登录凭证，请像密码一样保护。
- 请确保容器内可以访问 `/usr/local/bin/tdl`。
- 请确保 `~/.tdl` 权限正确，否则可能导致 TDL 无法读取登录状态。
- `FORWARD_TO_CHAT_ID` 建议使用去除 `-100` 前缀后的裸 ID。
- `MONITOR_CHAT_IDS` 只填写源频道裸 ID，不要加 `-100` 前缀，多个频道用英文逗号分隔。
- 不要泄露 `API_HASH`、`BOT_TOKEN` 等敏感信息。
- 如需长期运行，建议将 `restart` 改为：

```yaml
restart: unless-stopped
```

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

