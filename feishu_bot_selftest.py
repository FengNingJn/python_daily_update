#!/usr/bin/env python3
"""Offline smoke test for the unified Feishu message hub."""

import json
import tempfile
from pathlib import Path

import arkvol_push
import feishu_bot as bot
import message_hub


tmp_path = Path(tempfile.mkdtemp(prefix="feishu-hub-selftest-"))
bot.DB_FILE = tmp_path / "feishu_bot.db"
bot.AI_CONFIG_FILE = tmp_path / "ai_config.json"
bot.AI_MEMORY_FILE = tmp_path / "ai_memory.md"
bot.nas_runner.REPORT_FILE = tmp_path / "nga_daily_report.md"
reports = [
    {
        "key": "100:1001",
        "tid": "100",
        "pid": "1001",
        "date": "2099-01-01",
        "time": "09:00",
        "author": "狼大",
        "text": "历史基线消息",
    }
]
bot.nas_runner.parse_report = lambda: list(reports)
bot.nas_runner.REPORT_FILE.write_text(
    """## 每日市场数据 - 2099-01-01

### 主要指数

- 上证指数 +1.00%

## 明日展望 - 2099-01-01 夜盘

- 保持条件化计划。
""",
    encoding="utf-8",
)

# NGA delivery schedule: close the 08:00 window before realtime market-hour
# delivery, aggregate again at 06:00/18:00/20:00/22:00, and remain quiet in
# the other windows.
def cst_time(hour, minute=0):
    return bot.datetime(2026, 8, 5, hour, minute, tzinfo=bot.CST)


assert bot._nga_push_plan(cst_time(5, 59), "")[0] is False
assert bot._nga_push_plan(cst_time(6), "")[:3] == (True, "2026-08-05T06:00", "aggregate")
assert bot._nga_push_plan(cst_time(7, 59), "2026-08-05T06:00")[0] is False
assert bot._nga_push_plan(cst_time(8), "")[1:3] == ("2026-08-05T08:00", "aggregate")
assert bot._nga_push_plan(cst_time(8, 1), "2026-08-05T08:00")[2] == "realtime"
assert bot._nga_push_plan(cst_time(15, 59), "2026-08-05T08:00")[2] == "realtime"
assert bot._nga_push_plan(cst_time(16), "")[0] is False
assert bot._nga_push_plan(cst_time(17, 59), "")[0] is False
assert bot._nga_push_plan(cst_time(18), "")[:3] == (True, "2026-08-05T18:00", "aggregate")
assert bot._nga_push_plan(cst_time(19), "2026-08-05T18:00")[0] is False
assert bot._nga_push_plan(cst_time(20), "2026-08-05T18:00")[1] == "2026-08-05T20:00"
assert bot._nga_push_plan(cst_time(22), "2026-08-05T20:00")[1] == "2026-08-05T22:00"
assert bot._nga_push_plan(cst_time(23), "2026-08-05T22:00")[0] is False

# Existing delivery smoke tests run in realtime mode regardless of wall clock.
bot._nga_push_plan = lambda now, last_batch_slot="": (True, "", "realtime", None)

# Runtime AI configuration is stored outside the source tree and can be
# reloaded after a process restart without exposing the key in conversations.
bot.configure_deepseek("sk-selftest-key-that-is-never-sent")
assert bot.AI_CONFIG_FILE.exists()
bot.OPENAI_API_KEY = ""
assert bot.load_runtime_ai_config() is True
assert bot.OPENAI_API_KEY == "sk-selftest-key-that-is-never-sent"
assert bot.OPENAI_BASE_URL == "https://api.deepseek.com"
assert bot.OPENAI_MODEL == "deepseek-v4-flash"
assert bot.OPENAI_API_MODE == "chat_completions"
bot.AI_MEMORY_FILE.write_text("永远不要删除 NAS 上的任何文件。", encoding="utf-8")
assert "永远不要删除 NAS 上的任何文件" in bot.effective_system_instructions()

# First indexing pass backfills history without sending it.
bot.index_report()
bot.index_daily_market_reports()
with bot.connect_db() as db:
    stats = message_hub.database_stats(db)
    assert stats["events_by_source"] == {"daily_market": 1, "nga": 1}
    assert stats["deliveries"] == {"baseline": 2}
    bot.set_setting(db, "target_chat_id", "offline-chat")
    db.commit()

# The same logical NGA post can have a different pid in an author-filtered
# view. It must remain one event and must not enter the push queue again.
reports.append(
    {
        "key": "999:9999",
        "tid": "999",
        "pid": "9999",
        "date": "2099-01-01",
        "time": "09:00",
        "author": "狼大",
        "text": "历史基线消息",
    }
)
bot.index_report()
with bot.connect_db() as db:
    stats = message_hub.database_stats(db)
    assert stats["events_by_source"] == {"daily_market": 1, "nga": 1}
    assert stats["deliveries"] == {"baseline": 2}
    assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1

# A new report becomes one pending event and is sent only once.
reports.append(
    {
        "key": "100:1002",
        "tid": "100",
        "pid": "1002",
        "date": "2099-01-01",
        "time": "10:00",
        "author": "狼大",
        "text": "这是被跟踪 UP 的明确回复 [引用: 这是楼友提出的问题]",
    }
)
bot.index_report()
sent = []


def fake_send(chat_id, text):
    sent.append((chat_id, text))
    return [f"om_{len(sent)}"]


bot.send_text = fake_send
bot.push_pending_events()
bot.push_pending_events()
assert len(sent) == 1, f"expected one NGA push, got {len(sent)}"
assert "这是被跟踪 UP 的明确回复" in sent[0][1]
assert "────【被回复内容 / 提问】────" in sent[0][1]
assert "────【UP 回复】────" in sent[0][1]
assert sent[0][1].index("这是楼友提出的问题") < sent[0][1].index("这是被跟踪 UP 的明确回复")

# Arkvol publisher writes into the same database; a second run is deduplicated.
today = bot.datetime.now(bot.CST).date().isoformat()
arkvol_push.MESSAGE_HUB_DB_FILE = str(bot.DB_FILE)
arkvol_push.generate_combined_report = lambda: (
    {"current_version": "selftest"},
    "## 每日市场数据\n上证 +1.00%\n\n## 明日展望\n保持条件化计划。\n\n"
    "## Arkvol A股贪婪指数\n数据日期：2099-01-01\n宽基：42.18（中性）",
)
first_ids = arkvol_push.send_report("17:00")
second_ids = arkvol_push.send_report("17:00")
assert first_ids[0].startswith("daily_report:")
assert second_ids[0].endswith(":duplicate")
bot.push_pending_events()
bot.push_pending_events()
assert len(sent) == 2, f"expected one combined push, got {len(sent) - 1}"

tool_result = json.loads(bot.execute_history_tool("get_market_snapshot", {"date": today}))
assert tool_result["count"] == 1
assert "42.18" in tool_result["messages"][0]["content"]
assert "每日市场数据" in tool_result["messages"][0]["content"]

# A new daily report is one database record and one Feishu push. Re-indexing
# the same day must not create or send a duplicate.
bot.nas_runner.REPORT_FILE.write_text(
    """## 每日市场数据 - 2099-01-02

### 主要指数

- 上证指数 +0.50%

## 明日展望 - 2099-01-02 夜盘

- 关注量价确认。
""",
    encoding="utf-8",
)
assert bot.index_daily_market_reports() == 1
assert bot.index_daily_market_reports() == 0
bot.push_pending_events()
bot.push_pending_events()
assert len(sent) == 3, f"expected one daily-market push, got {len(sent) - 2}"
daily_result = json.loads(bot.execute_history_tool("get_daily_market_report", {"date": "2099-01-02"}))
assert daily_result["count"] == 1
assert "关注量价确认" in daily_result["messages"][0]["content"]

# Verify the Responses API function-call loop sends local tool output back to the model.
class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


api_payloads = []
api_responses = [
    {
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "get_market_snapshot",
                "arguments": json.dumps({"date": today}),
            }
        ]
    },
    {"output": [{"type": "message", "content": [{"type": "output_text", "text": "工具调用正常"}]}]},
]


def fake_post(url, **kwargs):
    assert url.endswith("/responses")
    api_payloads.append(kwargs["json"])
    return FakeResponse(api_responses.pop(0))


bot.OPENAI_API_KEY = "selftest-key"
bot.OPENAI_BASE_URL = "https://api.openai.com/v1"
bot.OPENAI_API_MODE = "responses"
bot.requests.post = fake_post
answer = bot.ask_openai("offline-chat", f"{today} 的贪婪指数是什么？")
assert answer == "工具调用正常"
assert any(
    item.get("type") == "function_call_output"
    for item in api_payloads[1]["input"]
    if isinstance(item, dict)
)

# Verify DeepSeek/OpenAI-compatible Chat Completions tool calling as well.
chat_payloads = []
chat_responses = [
    {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "chat_call_1",
                            "type": "function",
                            "function": {
                                "name": "get_market_snapshot",
                                "arguments": json.dumps({"date": today}),
                            },
                        }
                    ],
                }
            }
        ]
    },
    {"choices": [{"message": {"role": "assistant", "content": "DeepSeek 工具调用正常"}}]},
]


def fake_chat_post(url, **kwargs):
    assert url.endswith("/chat/completions")
    chat_payloads.append(kwargs["json"])
    return FakeResponse(chat_responses.pop(0))


bot.OPENAI_API_MODE = "chat_completions"
bot.OPENAI_BASE_URL = "https://api.deepseek.com"
bot.OPENAI_MODEL = "deepseek-v4-flash"
bot.requests.post = fake_chat_post
chat_answer = bot.ask_openai("offline-chat", f"{today} 的贪婪指数是什么？")
assert chat_answer == "DeepSeek 工具调用正常"
assert any(item.get("role") == "tool" for item in chat_payloads[1]["messages"])

with bot.connect_db() as db:
    final_stats = message_hub.database_stats(db)
assert final_stats["deliveries"]["sent"] == 3
assert final_stats["deliveries"]["baseline"] == 2
backup = message_hub.backup_database(bot.DB_FILE, tmp_path / "backups")
assert backup.exists() and backup.stat().st_size > 0
print("FEISHU_SELFTEST_OK baseline=2 nga_push=1 combined_push=1 daily_market_push=1 duplicate=0 responses_tool_loop=1 chat_tool_loop=1 backup=1")
