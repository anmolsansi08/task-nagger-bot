import json
import os
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Chicago")
STATE_FILE = "state.json"
TASKS_FILE = "tasks.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

START_TIME = time(19, 0)   # 7:00 PM
CUTOFF_TIME = time(2, 0)   # 2:00 AM

KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")  # task key rules


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


def compute_target_date(now: datetime) -> str:
    # After midnight until cutoff, still targeting "yesterday"
    if now.time() <= CUTOFF_TIME:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def in_reminder_window(now: datetime) -> bool:
    # Window crosses midnight: (>= 7 PM) OR (<= 2 AM)
    return (now.time() >= START_TIME) or (now.time() <= CUTOFF_TIME)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_update_id": 0, "done": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("last_update_id", 0)
    data.setdefault("done", {})
    return data


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_tasks_config():
    if not os.path.exists(TASKS_FILE):
        # default minimal config if missing
        return {
            "default": "task1",
            "tasks": [{"key": "task1", "label": "Task 1"}],
        }
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("tasks", [])
    data.setdefault("default", data["tasks"][0]["key"] if data["tasks"] else None)
    return data


def save_tasks_config(cfg):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def task_map(cfg):
    # key -> label
    return {t["key"]: t.get("label", t["key"]) for t in cfg.get("tasks", [])}


def get_pending(cfg, done_map, target_date: str):
    tmap = task_map(cfg)
    pending = []
    for key, label in tmap.items():
        if done_map.get(key) != target_date:
            pending.append((key, label))
    return pending


def format_tasks(cfg):
    lines = []
    default_key = cfg.get("default")
    for t in cfg.get("tasks", []):
        key = t["key"]
        label = t.get("label", key)
        star = " (default)" if key == default_key else ""
        lines.append(f"- {key}: {label}{star}")
    return "\n".join(lines) if lines else "(no tasks yet)"


def valid_key(key: str) -> bool:
    return bool(KEY_RE.match(key))


def normalize_label(label: str) -> str:
    label = label.strip()
    if len(label) > 80:
        label = label[:80]
    return label


def add_task(cfg, key: str, label: str) -> str:
    key = key.strip().lower()
    if not valid_key(key):
        return "Invalid key. Use 1–32 chars: lowercase letters, numbers, _ or -."
    label = normalize_label(label)
    if not label:
        return "Label cannot be empty."

    existing = task_map(cfg)
    if key in existing:
        return f"Task '{key}' already exists. Use /label {key} <new label> to rename."

    cfg["tasks"].append({"key": key, "label": label})
    if not cfg.get("default"):
        cfg["default"] = key
    return f"Added task: {key} = {label}"


def remove_task(cfg, done_map, key: str) -> str:
    key = key.strip().lower()
    tasks = cfg.get("tasks", [])
    before = len(tasks)
    cfg["tasks"] = [t for t in tasks if t.get("key") != key]
    after = len(cfg["tasks"])
    if before == after:
        return f"No task named '{key}'."

    done_map.pop(key, None)

    if cfg.get("default") == key:
        cfg["default"] = cfg["tasks"][0]["key"] if cfg["tasks"] else None

    return f"Removed task '{key}'."


def set_default(cfg, key: str) -> str:
    key = key.strip().lower()
    if key not in task_map(cfg):
        return f"No task named '{key}'. Use /tasks."
    cfg["default"] = key
    return f"Default task set to '{key}'."


def set_label(cfg, key: str, label: str) -> str:
    key = key.strip().lower()
    label = normalize_label(label)
    if not label:
        return "Label cannot be empty."
    updated = False
    for t in cfg.get("tasks", []):
        if t.get("key") == key:
            t["label"] = label
            updated = True
            break
    if not updated:
        return f"No task named '{key}'. Use /tasks."
    return f"Updated label: {key} = {label}"


def help_text(cfg):
    return (
        "Commands:\n"
        "- /tasks (show tasks)\n"
        "- /add <key> <label>   (example: /add gym Gym)\n"
        "- /remove <key>\n"
        "- /label <key> <label> (rename label)\n"
        "- /default <key>\n"
        "- /done <key>          (example: /done gym)\n"
        "- /status\n"
        "- /reset               (clears completion for the target day)\n"
        "- /resetall            (clears done + update offset)\n\n"
        "Current tasks:\n"
        f"{format_tasks(cfg)}"
    )


def main():
    state = load_state()
    cfg = load_tasks_config()

    last_update_id = int(state.get("last_update_id", 0))
    done_map = state.get("done", {})

    now = datetime.now(TZ)
    target_date = compute_target_date(now)

    updates = tg_get_updates(offset=last_update_id + 1)

    cfg_changed = False
    state_changed = False
    responded = False

    # Flags
    status_requested = False
    tasks_requested = False
    reset_today = False
    reset_all = False

    for upd in updates:
        uid = upd.get("update_id", last_update_id)
        last_update_id = max(last_update_id, uid)

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        chat = msg.get("chat", {})
        if str(chat.get("id")) != CHAT_ID:
            continue

        text = (msg.get("text") or "").strip()
        lower = text.lower()

        # /tasks or /help
        if lower.startswith("/tasks") or lower.startswith("/help"):
            tasks_requested = True
            continue

        # /status
        if lower.startswith("/status"):
            status_requested = True
            continue

        # /resetall
        if lower.startswith("/resetall"):
            reset_all = True
            continue

        # /reset
        if lower.startswith("/reset"):
            reset_today = True
            continue

        # /add <key> <label...>
        if lower.startswith("/add"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                tg_send_message("Usage: /add <key> <label>  (example: /add gym Gym)")
                responded = True
                continue
            key = parts[1]
            label = parts[2]
            msg_out = add_task(cfg, key, label)
            save_tasks_config(cfg)
            cfg_changed = True
            tg_send_message(msg_out)
            responded = True
            continue

        # /remove <key>
        if lower.startswith("/remove"):
            parts = lower.split(maxsplit=1)
            if len(parts) < 2:
                tg_send_message("Usage: /remove <key>")
                responded = True
                continue
            key = parts[1]
            msg_out = remove_task(cfg, done_map, key)
            save_tasks_config(cfg)
            cfg_changed = True
            state_changed = True
            tg_send_message(msg_out)
            responded = True
            continue

        # /label <key> <new label...>
        if lower.startswith("/label"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                tg_send_message("Usage: /label <key> <new label>")
                responded = True
                continue
            key = parts[1]
            label = parts[2]
            msg_out = set_label(cfg, key, label)
            save_tasks_config(cfg)
            cfg_changed = True
            tg_send_message(msg_out)
            responded = True
            continue

        # /default <key>
        if lower.startswith("/default"):
            parts = lower.split(maxsplit=1)
            if len(parts) < 2:
                tg_send_message("Usage: /default <key>")
                responded = True
                continue
            key = parts[1]
            msg_out = set_default(cfg, key)
            save_tasks_config(cfg)
            cfg_changed = True
            tg_send_message(msg_out)
            responded = True
            continue

        # /done <key> (or /done uses default)
        if lower.startswith("/done"):
            parts = lower.split(maxsplit=1)
            key = parts[1].strip().lower() if len(parts) > 1 else (cfg.get("default") or "")
            if not key:
                tg_send_message("No default task set. Use /add to create a task first.")
                responded = True
                continue
            if key not in task_map(cfg):
                tg_send_message(f"Unknown task '{key}'. Use /tasks.")
                responded = True
                continue
            done_map[key] = target_date
            state_changed = True
            tg_send_message(f"Marked done: {key} for {target_date}.")
            responded = True
            continue

    # Apply resets
    if reset_all:
        done_map = {}
        last_update_id = 0
        state_changed = True
        tg_send_message("Reset all memory (done + update offset).")
        responded = True

    elif reset_today:
        # Clear only the current target day
        done_map = {k: v for k, v in done_map.items() if v != target_date}
        state_changed = True
        tg_send_message(f"Reset completion for {target_date}.")
        responded = True

    # Save state if needed
    state["last_update_id"] = last_update_id
    state["done"] = done_map
    save_state(state)

    # Respond to /tasks or /status
    if tasks_requested:
        tg_send_message(help_text(cfg))
        return

    if status_requested:
        pending = get_pending(cfg, done_map, target_date)
        if not pending:
            tg_send_message(f"Status: ALL DONE for {target_date}.")
        else:
            pretty = ", ".join([label for _, label in pending])
            tg_send_message(
                f"Status for {target_date}:\n"
                f"Pending: {pretty}\n"
                "Mark done: /done <key>  (see keys with /tasks)"
            )
        return

    # If we already responded to a command this run, don’t also nag.
    if responded:
        return

    # Remind
    now = datetime.now(TZ)
    target_date = compute_target_date(now)

    if not in_reminder_window(now):
        return

    pending = get_pending(cfg, done_map, target_date)
    if not pending:
        return

    pretty = ", ".join([label for _, label in pending])
    tg_send_message(
        f"Reminder for {target_date}:\n"
        f"Pending: {pretty}\n\n"
        "Use /done <key> (see keys with /tasks)"
    )


if __name__ == "__main__":
    main()
