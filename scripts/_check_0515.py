import os
import re
import sys
import sqlite3
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from modules.config_loader import load_config  # type: ignore
from modules import calendar  # type: ignore
from modules.notifier import _normalize_name_for_samhaengsi  # type: ignore

from main import _SAMHAENGSI_CALENDAR_TITLE_PAT as PAT  # type: ignore

target = date(2026, 5, 15)
cfg = load_config()
owner = cfg.get("naver_owner_calendar", "")
print(f"owner calendar: {owner!r}")

events = calendar.list_events_on_date(owner, target)
print(f"event count on {target}: {len(events)}")
for ev in events:
    summary = ev.get("summary", "")
    ev_id = ev.get("event_id", "")
    print()
    print(f"  summary: {summary!r}")
    print(f"  event_id: {ev_id[:24]}...")
    m = PAT.match(summary)
    if not m:
        print("  --> 정규식 매치 실패")
        continue
    name = m.group(2).strip()
    normalized = _normalize_name_for_samhaengsi(name)
    print(f"  추출된 이름: {name!r}")
    print(f"  정규화 후: {normalized!r}")

state_path = ROOT / "data" / "samhaengsi_sent_events.json"
if state_path.exists():
    import json
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    print()
    print(f"samhaengsi_sent_events.json 항목 수: {len(state)}")
    for ev in events:
        ev_id = ev.get("event_id", "")
        if ev_id in state:
            print(f"  ⚠ 이미 발송됨: {ev_id[:24]}... → {state[ev_id]}")

print()
db = sqlite3.connect("db/reservations.db")
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT booking_id, guest_name, status FROM reservations "
    "WHERE checkin='2026-05-15' AND status='confirmed'"
).fetchall()
print(f"DB confirmed 5/15: {len(rows)}건")
for r in rows:
    print(" ", dict(r))
