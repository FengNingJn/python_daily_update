#!/usr/bin/env python3
"""Offline checks for scheduled, aggregated NGA delivery."""

import tempfile
from datetime import datetime
from pathlib import Path

import feishu_bot as bot
import message_hub


root = Path(tempfile.mkdtemp(prefix="nga-push-schedule-"))
bot.DB_FILE = root / "hub.db"
bot.BACKUP_DIR = root / "backups"
sent = []
bot.send_text = lambda chat_id, text: sent.append((chat_id, text)) or [f"m{len(sent)}"]


def cst(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=bot.CST)


def add(event_id, source="nga", author="狼大", day=5, hour=9, minute=0):
    with bot.connect_db() as db:
        created = message_hub.insert_event(
            db,
            event_id=event_id,
            source=source,
            event_type="forum_post" if source == "nga" else "daily_report",
            event_time=f"2026-08-{day:02d}T{hour:02d}:{minute:02d}:00+08:00",
            author=author,
            title=event_id,
            content=f"content-{event_id}",
            dedupe_key=event_id,
            notify=True,
        )
        db.commit()
    assert created


with bot.connect_db() as db:
    bot.set_setting(db, "target_chat_id", "offline-chat")
    db.commit()

# 08:00 closes the 06:00-08:00 window. A post at/after 08:00 must not leak
# into that aggregate; it is released by the following realtime pass.
add("nga-1", hour=6, minute=1)
add("nga-2", hour=7, minute=2)
add("nga-3", hour=7, minute=59)
add("nga-after-8", hour=8, minute=1)
bot.push_pending_events(now=cst(5, 8, 2))
assert len(sent) == 1
assert all(f"content-nga-{number}" in sent[0][1] for number in (1, 2, 3))
assert "content-nga-after-8" not in sent[0][1]
bot.push_pending_events(now=cst(5, 8, 3))
assert len(sent) == 2 and "content-nga-after-8" in sent[1][1]

# 16:00-18:00 is quiet; 18:00 releases one aggregate and records the slot.
add("nga-4", hour=16, minute=4)
add("nga-5", hour=17, minute=59)
add("nga-after-18", hour=18, minute=1)
bot.push_pending_events(now=cst(5, 17))
assert len(sent) == 2
bot.push_pending_events(now=cst(5, 18))
assert len(sent) == 3
assert "content-nga-4" in sent[2][1] and "content-nga-5" in sent[2][1]
assert "content-nga-after-18" not in sent[2][1]

# Posts arriving after the 18:00 slot wait until 20:00.
add("nga-6", hour=19, minute=6)
bot.push_pending_events(now=cst(5, 18, 5))
assert len(sent) == 3
bot.push_pending_events(now=cst(5, 20))
assert len(sent) == 4
assert "content-nga-after-18" in sent[3][1] and "content-nga-6" in sent[3][1]

# Non-NGA reports remain immediate during the overnight NGA quiet window.
add("daily-1", source="daily_report", author="daily_report", hour=23, minute=7)
add("nga-7", hour=23, minute=7)
bot.push_pending_events(now=cst(5, 23))
assert len(sent) == 5 and "content-daily-1" in sent[4][1]
bot.push_pending_events(now=cst(6, 6))
assert len(sent) == 6 and "content-nga-7" in sent[5][1]

with bot.connect_db() as db:
    statuses = dict(db.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status").fetchall())
assert statuses == {"sent": 10}, statuses
print("NGA_PUSH_SCHEDULE_SELFTEST_OK sends=6 events=10")
