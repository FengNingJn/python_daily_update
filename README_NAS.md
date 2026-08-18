# 群晖 DS918+ 部署

项目在容器内每 300 秒执行一次：

```bash
python update_nga.py --inc --days=1
```

持久化目录：

- `data/`：Cookie、报告、缓存和推送配置
- `state/`：已推送内容指纹和运行锁
- `logs/`：运行日志

首次启动会把现有报告建立为推送基线，避免把历史发言全部推送到手机。
`wheels/` 保存 Linux 离线依赖，构建时不需要从 PyPI 下载 Python 包。

## 推送配置

复制 `config.example.json` 为 `data/config.json`。默认 `provider` 为
`none`，只更新报告而不推送。

Server酱配置：

```json
{
  "push": {
    "provider": "serverchan",
    "serverchan_sendkey": "在 NAS 本地填写",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": ""
  }
}
```

SendKey 和 `nga_cookies.json` 不应提交到 GitHub。

ntfy 配置：

```json
{
  "push": {
    "provider": "ntfy",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "使用一段足够长的随机字符串"
  }
}
```

安卓安装 ntfy 应用后，订阅 `https://ntfy.sh/<ntfy_topic>`。随机主题名相当于
访问密钥，不应公开。

## 启动

在项目目录执行：

```bash
sudo /usr/local/bin/docker compose up -d --build
```

查看状态：

```bash
sudo /usr/local/bin/docker ps --filter name=nga-updater
sudo /usr/local/bin/docker logs --tail 100 nga-updater
```
