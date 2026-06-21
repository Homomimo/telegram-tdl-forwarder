import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

print("正在生成 Telethon 用户账号 StringSession。")
print("请按提示输入手机号、验证码；如果开启二步验证，还需要输入密码。\n")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n复制下面这一行到 docker-compose.yml 或 .env：\n")
    print("USER_SESSION_STRING=" + client.session.save())
