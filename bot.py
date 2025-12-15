import json
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Chicago")
STATE_FILE = "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

START_TIME = time(19, 0)   # 7:00 PM
CUTOFF_TIME = time(2, 0)   # 2:00 AM


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_update_id": 0, "last_done_date": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def tg_get_updates(offset: int):
    r = requests.get(
        f"{API_BASE}/getUpdates",
        params={"offset": offset, "timeout": 10},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def tg_send_message(text: str):
    r = requests.post(
        f"{API_BASE}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    r.raise_for_status()


def chicago_date_from_unix(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=TZ).date().isoformat()


def compute_target_date(now: datetime) -> str:
    """
    Reminder window: 7:00 PM -> 2:00 AM (crosses midnight).
    If it's after midnight but before/at 2:00 AM, we're still targeting "yesterday".
    """
    if now.time() <= CUTOFF_TIME:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def in_reminder_window(now: datetime) -> bool:
    # Window crosses midnight, so it's (>= 7 PM) OR (<= 2 AM)
    return (now.time() >= START_TIME) or (now.time() <= CUTOFF_TIME)


def main():
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))
    last_done_date = state.get("last_done_date")

    # 1) Read new messages and look for /done
    updates = tg_get_updates(offset=last_update_id + 1)
    for upd in updates:
        uid = upd.get("update_id", last_update_id)
        last_update_id = max(last_update_id, uid)

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        chat = msg.get("chat", {})
        if str(chat.get("id")) != CHAT_ID:
            continue

        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/done"):
            msg_date = msg.get("date")
            done_date = (
                chicago_date_from_unix(msg_date)
                if msg_date
                else datetime.now(TZ).date().isoformat()
            )
            last_done_date = done_date

    state["last_update_id"] = last_update_id
    state["last_done_date"] = last_done_date
    save_state(state)

    # 2) Decide whether to remind
    now = datetime.now(TZ)

    if not in_reminder_window(now):
        return

    target_date = compute_target_date(now)

    # If already done for the target date, stop.
    if last_done_date == target_date:
        return

    tg_send_message(
        f"Reminder: you haven't marked the task as done for {target_date}.\n"
        f"Reply with /done to stop reminders for that day."
    )


if __name__ == "__main__":
    main()