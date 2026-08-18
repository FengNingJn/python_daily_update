# B站UP主观点汇总

每天08:00和21:00检查7个指定UP主的新投稿，优先取得B站人工字幕或AI字幕，使用DeepSeek生成结构化观点，并同时保存到SQLite和Markdown。

## 已实现

- 按UID查询投稿，不依赖UP主名称搜索。
- 首次运行自动保存UP主名称。
- BVID + CID作为视频/分P唯一键。
- 字幕内容哈希与版本表；字幕变化时新增版本，不覆盖旧版本。
- 字幕顺序：人工字幕 → `ai-zh/ai-en` → AI总结里的完整字幕 → 本地Whisper。
- AI总结接口同时保存官方摘要、提纲和`part_subtitle`逐句时间轴。
- DeepSeek长视频分段总结、历史观点对比和时段共识/分歧汇总。
- SQLite、逐视频Markdown、逐时段Markdown双存。
- 每个UP主单独检查点、失败重试状态、运行锁和任务日志。
- Windows任务计划程序安装脚本（认证测试通过后再执行）。

## 目录

```text
bilibili_digest/
├── config/
│   ├── creators.yaml
│   ├── settings.yaml
│   └── bilibili_cookies.txt   # 本地私密文件，已被.gitignore忽略
├── data/bilibili_digest.db
├── transcripts/YYYY-MM-DD/
├── summaries/YYYY-MM-DD/
├── digests/YYYY-MM-DD/0800.md
├── state/scheduler_state.json
└── logs/bilibili_digest.log
```

## 初始化与检查

在 `C:\Users\19458\Desktop\DeepSeek` 执行：

```powershell
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py init
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py doctor
```

## 私密配置

1. B站Cookie保存为 `config/bilibili_cookies.txt`。支持浏览器导出的Netscape格式，也支持一行Cookie请求头格式。至少需要有效的`SESSDATA`。
2. 复制`.env.example`为`.env`，设置`DEEPSEEK_API_KEY`。两个文件均不会进入Git。
3. Cookie和API Key不会写入SQLite、Markdown或日志。

## 手动运行

```powershell
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py run --slot 08:00
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py run --slot 21:00
```

## 全量本地归档（不调用AI、不消耗模型Token）

按三个阶段运行，均可中断后重跑：

```powershell
# 1. 保存7个UP的全部公开视频清单和分P详情
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py backfill --phase inventory

# 2. 优先保存B站人工字幕、AI字幕和B站AI总结字幕
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py backfill --phase subtitles

# 3. 对仍无字幕的视频进行本地Whisper转写
.\.venv\Scripts\python.exe .\bilibili_digest\bilibili_digest.py backfill --phase whisper
```

`--limit 20` 可用于小批量验证。视频、字幕和状态均以 SQLite 为准，重复运行不会重复保存；临时音频默认在转写后移除。

首次运行只处理最近3天、每位UP最多3条；若最近3天没有投稿，则用最新3条建立可验证基线。后续按每位UP的成功检查点增量处理，同时对最近投稿有限复查，以发现B站稍后补生成的AI字幕。

## Whisper回退

只有B站没有任何人工/AI字幕和AI总结文本时才需要：

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\bilibili_digest\requirements-whisper.txt
```

默认使用`faster-whisper small`、CPU int8，只下载音频；临时音频默认不保留。

## 定时任务

认证和完整实测通过后再执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\bilibili_digest\scripts\install_windows_tasks.ps1
```

脚本会建立08:00和21:00两个任务，允许唤醒、错过后补跑、最长2小时、失败后30分钟重试最多3次。

## 测试

```powershell
$env:PYTHONPATH='.\bilibili_digest'
.\.venv\Scripts\python.exe -m unittest discover -s .\bilibili_digest\tests -v
```

## 群晖 NAS 运行方式

NAS 服务使用现有统一消息库与飞书机器人：

- 每 2 小时检查 7 个 UP 主的新投稿并抓取 B 站人工字幕、AI 字幕或 AI 总结字幕；该阶段不调用大模型。
- 每天 08:00、21:00 只处理上次汇总后出现的新字幕；没有新内容时不调用 AI，也不推送空消息。
- 单视频结构化摘要写入统一 SQLite 但不逐条推送；每个时段只推送一条汇总，避免刷屏。
- BVID + CID + 字幕版本用于去重；同一字幕不会重复总结或推送。
- DS918+ 上不运行 Whisper。缺少 B 站字幕的视频保留等待状态，并在最近 7 天内继续复查；本地电脑可继续执行 Whisper 历史补全。
- NAS 状态文件每次更新前保存一份时间戳历史副本；部署时也先备份原 Compose、机器人代码和统一数据库。

Compose 服务名为 `bilibili-digest`，配置的采集间隔是 `2` 小时，汇总时点为 `08:00,21:00`。
