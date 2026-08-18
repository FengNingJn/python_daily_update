# 飞书统一消息中心与 AI 对话

该服务使用飞书企业自建应用机器人，将 NGA、Arkvol 主动推送和 AI 问答放在同一个聊天窗口。
所有消息先写入 NAS 上的 SQLite `events` 表，再统一投递到飞书；AI 通过只读工具查询同一份完整历史。

## 数据流

```text
NGA 报告 ─┐
Arkvol ───┼─> feishu_bot.db/events ─> 飞书主动推送
其他来源 ─┘             └──────────> OpenAI 只读历史工具
```

- `event_id` 与 `dedupe_key` 防止同一消息重复入库。
- `deliveries` 保存待发送、发送中、已发送、基线和失败状态。
- 为避免网络响应丢失后重复推送，失败项不会自动重发，可在确认后显式重排队。
- OpenAI 请求保持 `store: false`，原始历史和对话以 NAS 数据库为准。
- 机器人每天使用 SQLite 在线备份生成一份一致性副本；遵循现有要求，不自动删除旧备份。
- AI 网关同时支持 OpenAI Responses API 和 DeepSeek/OpenAI-compatible Chat Completions；通过 `OPENAI_API_MODE` 选择，`auto` 会按 Base URL 自动判断。

## 飞书后台

1. 创建企业自建应用并开启机器人能力。
2. 开通权限：`im:message:send_as_bot`、`im:message.p2p_msg:readonly`。
3. 在“事件与回调”中选择长连接，并订阅 `im.message.receive_v1`。
4. 创建并发布一个可用版本，将当前用户加入可用范围。

## NAS 配置

复制 `feishu.env.example` 为项目根目录的 `.env`，填写：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `OPENAI_API_KEY`

不要将 `.env`、App Secret 或 API Key 提交到 GitHub。

启动：

```bash
docker compose --profile feishu up -d nga-feishu-bot
```

首次给机器人发送消息的飞书用户会自动绑定为所有者；之后机器人只处理该用户的消息。
首次启动只建立历史基线，不推送旧报告。后续新发言才会主动推送。

Arkvol 容器挂载同一个 `/bot-state/feishu_bot.db`，每天 08:00、17:00 生成的宽基和板块指数也会进入统一历史。

## 对话能力

机器人向 OpenAI Responses API 暴露以下只读工具：

- `search_messages`
- `get_recent_messages`
- `get_messages_by_author`
- `get_market_snapshot`
- `get_daily_messages`
- `get_message_detail`

因此“拥有全部历史”指 AI 可以按需检索完整数据库，而不是在每次请求中重复发送全部消息。

常用命令：

- `/help`：查看示例问题
- `/status`：查看各来源消息数和投递状态
- `/ai-status`：查看 AI 接口与模型状态（不会显示密钥）
- `/set-deepseek-key sk-你的密钥`：仅绑定用户可用；把 DeepSeek 密钥保存到 NAS 私有配置，不写入对话数据库

首次配置 DeepSeek 时，在与机器人的单聊中发送一次 `/set-deepseek-key sk-你的密钥`。
收到“DeepSeek 已配置完成”后，可以删除飞书里的这条配置消息，然后发送普通问题测试。

## 离线自测

```bash
python -m py_compile message_hub.py feishu_bot.py arkvol_push.py
python feishu_bot_selftest.py
```
