"""
Xiaozhi Windows Time Assistant (MCP)
------------------------------------
Tools exposed to Xiaozhi:
- set_timer, list_timers, cancel_timer
- start_stopwatch, pause_stopwatch, resume_stopwatch, stop_stopwatch, lap_stopwatch
- set_alarm, add_reminder, list_reminders, delete_reminder
- get_local_time

Install:
    py -m pip install websocket-client pyttsx3 pygame requests

Set XIAOZHI_WSS_URL below to your NEW Xiaozhi MCP endpoint URL.
Keep the token private. Do not share this file with the token filled in.

Windows alerts:
- A Windows popup and beep are generated when a timer/alarm/reminder is due.
- The MCP tool response confirms the request to Xiaozhi.
- A later alert cannot automatically make Xiaozhi speak through standard MCP
  tool responses. That requires a separate Xiaozhi-supported outbound speech/
  notification mechanism.
"""

import json
import logging
import os
import threading
import time
import uuid
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import websocket

# ========================= CONFIGURATION =========================
XIAOZHI_WSS_URL = os.getenv("XIAOZHI_WSS_URL", "").strip()

APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "XiaozhiTimeAssistant"
DATA_FILE = APP_DIR / "scheduled_items.json"
CHECK_INTERVAL_SECONDS = 0.5
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

lock = threading.RLock()
timers = {}
scheduled_items = []
stopwatch = {
    "running": False,
    "started_at": None,
    "elapsed_before_start": 0.0,
    "laps": []
}


def now_local():
    return datetime.now().astimezone()


def parse_local_datetime(value):
    """Accept ISO date/time or common local formats."""
    value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d %I:%M %p"):
        try:
            return datetime.strptime(value, fmt).astimezone()
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone()
    except ValueError:
        raise ValueError(
            "Use a date and time such as 2026-11-15 18:30, "
            "or 15-11-2026 06:30 PM."
        )


def load_scheduled_items():
    global scheduled_items
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            scheduled_items = data
        else:
            scheduled_items = []
    except FileNotFoundError:
        scheduled_items = []
    except Exception as exc:
        logging.error("Could not load saved reminders: %s", exc)
        scheduled_items = []


def save_scheduled_items():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = DATA_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(scheduled_items, f, indent=2, ensure_ascii=False)
    temp_file.replace(DATA_FILE)


def windows_alert(title, message):
    """Show a Windows message box and play a short alert sound."""
    def alert_worker():
        try:
            import winsound
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.35)
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, str(message), str(title), 0x00001000 | 0x00000040
            )
        except Exception as exc:
            logging.warning("Windows popup unavailable: %s", exc)
            print(f"\n*** {title}: {message} ***\n")

    threading.Thread(target=alert_worker, daemon=True).start()



def telegram_send(message):
    if not TELEGRAM_ENABLED: return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)
        return False

def telegram_event(title, message):
    d = now_local()
    telegram_send(f"🔔 <b>{title}</b>\\n\\n{message}\\n\\n🗓 {d.strftime('%A, %d %B %Y • %I:%M:%S %p')}\\n🌐 {d.tzname() or d.tzinfo}")

def play_event_mp3(kind):
    """Play a saved MPEG/MP3 recording for an event.

    Accepts .mpeg, .mpg and .mp3 files. The default recording can be placed
    either beside this Python file or inside its sounds folder.
    """
    try:
        import pygame

        app_dir = Path(__file__).resolve().parent
        extensions = (".mpeg", ".mpg", ".mp3")
        candidates = []

        # First look for an event-specific recording, then the default one.
        for stem in (str(kind), "default"):
            for folder in (SOUNDS_DIR, app_dir):
                for extension in extensions:
                    candidate = folder / f"{stem}{extension}"
                    if candidate not in candidates:
                        candidates.append(candidate)

        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            logging.warning(
                "No voice file found. Put default.mpeg in %s or %s",
                SOUNDS_DIR, app_dir
            )
            return

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.15)
        logging.info("Played voice recording: %s", path)
    except ImportError:
        logging.warning("Install pygame for MPEG/MP3 playback: py -m pip install pygame")
    except Exception as exc:
        logging.exception("Voice playback failed: %s", exc)

def alert_audio_and_speech(kind, message):
    play_event_mp3(kind)
    speak_local(message)

def send_current_time_to_telegram():
    info = get_local_time()
    msg = f"🕒 <b>Current Local Time</b>\\n⏰ {info['time']}\\n📅 {info['date']}\\n🌐 {info['timezone']}"
    return {"status": "sent" if telegram_send(msg) else "not_configured_or_failed", **info}

def handle_telegram_command(command):
    parts = command.split()
    cmd = parts[0].lower().split("@")[0]
    try:
        if cmd in ("/start", "/help"):
            return ("🤖 <b>Xiaozhi Time Assistant</b>\\n/time\\n/timer SECONDS [label]\\n/timers\\n"
                    "/alarm HH:MM [label]\\n/reminders\\n/remind YYYY-MM-DD HH:MM | message\\n"
                    "/stopwatch start|pause|resume|stop|lap|status")
        if cmd == "/time":
            v = get_local_time()
            return f"🕒 {v['time']}\\n📅 {v['date']}\\n🌐 {v['timezone']}"
        if cmd == "/timer":
            if len(parts) < 2: return "Usage: /timer SECONDS [label]"
            label = " ".join(parts[2:]) or "Telegram timer"
            r = set_timer(int(parts[1]), label)
            return f"⏱ Timer started: {label} (ID {r['timer_id']})"
        if cmd == "/timers":
            rows = list_timers()["active_timers"]
            return "⏱ <b>Active timers</b>\\n" + ("\\n".join(
                f"• {x['label']}: {x['remaining_seconds']} sec — {x['timer_id']}" for x in rows) if rows else "No active timers.")
        if cmd == "/alarm":
            if len(parts) < 2: return "Usage: /alarm HH:MM [label]"
            label = " ".join(parts[2:]) or "Telegram alarm"
            r = set_alarm(parts[1], label)
            return f"⏰ Alarm set: {r['date_time']} — {label}"
        if cmd == "/reminders":
            rows = list_reminders()["scheduled_items"]
            return "📌 <b>Upcoming</b>\\n" + ("\\n".join(
                f"• {x['type']}: {x['message']} — {x['date_time']} (ID {x['id']})" for x in rows) if rows else "No upcoming reminders.")
        if cmd == "/remind":
            if "|" not in command: return "Usage: /remind YYYY-MM-DD HH:MM | reminder message"
            left, message = command[len(parts[0]):].split("|", 1)
            r = add_reminder(message.strip(), left.strip())
            return f"📌 Saved: {r['date_time']} — {message.strip()} (ID {r['id']})"
        if cmd == "/stopwatch":
            action = parts[1].lower() if len(parts) > 1 else "status"
            if action == "start": r = start_stopwatch()
            elif action == "pause": r = pause_stopwatch()
            elif action == "resume": r = resume_stopwatch()
            elif action == "stop":
                r = stop_stopwatch()
            elif action == "lap": r = lap_stopwatch()
            elif action == "status":
                with lock: r = {"status": "running" if stopwatch["running"] else "paused/stopped", "elapsed": format_duration(stopwatch_elapsed())}
            else: return "Use /stopwatch start|pause|resume|stop|lap|status"
            return f"⏱ Stopwatch {r.get('status')}: {r.get('elapsed', r.get('final_time', r.get('lap_time', '')))}"
        return "Unknown command. Send /help."
    except Exception as exc:
        return f"⚠️ Command failed: {exc}"

def telegram_command_loop():
    if not TELEGRAM_ENABLED:
        logging.info("Telegram disabled: configure bot token and chat ID.")
        return
    import requests
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = None
    logging.info("Telegram command listener started.")
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None: params["offset"] = offset
            data = requests.get(base + "/getUpdates", params=params, timeout=35).json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != TELEGRAM_CHAT_ID: continue
                command = (msg.get("text") or "").strip()
                if command:
                    reply = handle_telegram_command(command)
                    if reply: telegram_send(reply)
        except Exception as exc:
            logging.warning("Telegram polling failed: %s", exc)
            time.sleep(5)


def notify_due(title, message, kind="reminder"):
    logging.info("%s: %s", title, message)
    threading.Thread(target=alert_audio_and_speech, args=(kind, message), daemon=True).start()
    windows_alert(title, message)
    telegram_event(title, message)



# ========================= WINDOWS VOICE ANNOUNCEMENTS =========================
speech_queue = queue.Queue()

def speech_worker():
    """Speak announcements locally on Windows using pyttsx3."""
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        while True:
            phrase = speech_queue.get()
            if phrase is None:
                break
            try:
                engine.say(phrase)
                engine.runAndWait()
            except Exception as exc:
                logging.warning("Speech announcement failed: %s", exc)
    except ImportError:
        logging.warning(
            "pyttsx3 is not installed. Voice announcements are disabled; "
            "install it with: py -m pip install pyttsx3"
        )
        while True:
            phrase = speech_queue.get()
            if phrase is None:
                break
            logging.info("[VOICE DISABLED] %s", phrase)
    except Exception:
        logging.exception("Could not initialize Windows speech engine")

def speak_local(text):
    speech_queue.put(str(text))


# ========================= TIMER FUNCTIONS =========================
def set_timer(duration_seconds, label="Timer"):
    seconds = int(duration_seconds)
    if seconds < 1 or seconds > 31_536_000:
        raise ValueError("Timer duration must be between 1 second and 365 days.")
    timer_id = uuid.uuid4().hex[:8]
    due = time.time() + seconds
    with lock:
        timers[timer_id] = {
            "id": timer_id,
            "label": str(label or "Timer"),
            "due_at": due,
            "duration_seconds": seconds,
            "announced": []
        }
    return {
        "status": "started",
        "timer_id": timer_id,
        "label": str(label or "Timer"),
        "duration_seconds": seconds,
        "finishes_at": datetime.fromtimestamp(due).astimezone().strftime("%I:%M:%S %p")
    }


def list_timers():
    with lock:
        result = []
        for item in timers.values():
            remaining = max(0, int(item["due_at"] - time.time()))
            result.append({
                "timer_id": item["id"],
                "label": item["label"],
                "remaining_seconds": remaining
            })
    return {"active_timers": result, "count": len(result)}


def cancel_timer(timer_id):
    with lock:
        if timer_id:
            removed = timers.pop(str(timer_id), None)
        elif len(timers) == 1:
            key = next(iter(timers))
            removed = timers.pop(key)
        else:
            return {
                "status": "needs_timer_id",
                "message": "Please provide the timer ID, or ask to cancel all timers."
            }
    if removed:
        return {"status": "cancelled", "timer_id": removed["id"], "label": removed["label"]}
    return {"status": "not_found", "message": "I could not find that active timer."}


def cancel_all_timers():
    with lock:
        count = len(timers)
        timers.clear()
    return {"status": "cancelled", "cancelled_count": count}


# ========================= STOPWATCH FUNCTIONS =========================
def stopwatch_elapsed():
    elapsed = stopwatch["elapsed_before_start"]
    if stopwatch["running"] and stopwatch["started_at"] is not None:
        elapsed += time.monotonic() - stopwatch["started_at"]
    return elapsed


def format_duration(seconds):
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def start_stopwatch():
    with lock:
        if stopwatch["running"]:
            return {"status": "already_running", "elapsed": format_duration(stopwatch_elapsed())}
        stopwatch["started_at"] = time.monotonic()
        stopwatch["running"] = True
        return {"status": "started", "elapsed": format_duration(stopwatch_elapsed())}


def pause_stopwatch():
    with lock:
        if not stopwatch["running"]:
            return {"status": "not_running", "elapsed": format_duration(stopwatch_elapsed())}
        stopwatch["elapsed_before_start"] = stopwatch_elapsed()
        stopwatch["started_at"] = None
        stopwatch["running"] = False
        return {"status": "paused", "elapsed": format_duration(stopwatch["elapsed_before_start"])}


def resume_stopwatch():
    return start_stopwatch()


def stop_stopwatch():
    with lock:
        elapsed = stopwatch_elapsed()
        stopwatch.update({
            "running": False,
            "started_at": None,
            "elapsed_before_start": 0.0,
            "laps": []
        })
    final_text = format_duration(elapsed)
    telegram_event("Stopwatch stopped", f"Final time: {final_text}")
    threading.Thread(
        target=alert_audio_and_speech,
        args=("stopwatch_complete", f"Stopwatch stopped. Final time: {final_text}"),
        daemon=True
    ).start()
    return {"status": "stopped_and_reset", "final_time": final_text}


def lap_stopwatch():
    with lock:
        if not stopwatch["running"]:
            return {"status": "not_running", "message": "The stopwatch is not running."}
        elapsed = stopwatch_elapsed()
        previous = stopwatch["laps"][-1]["total_seconds"] if stopwatch["laps"] else 0
        lap = {
            "lap": len(stopwatch["laps"]) + 1,
            "lap_time": format_duration(elapsed - previous),
            "total_time": format_duration(elapsed),
            "total_seconds": elapsed
        }
        stopwatch["laps"].append(lap)
        return {"status": "lap_recorded", **lap}


# ========================= ALARMS AND REMINDERS =========================
def add_scheduled_item(kind, message, when, repeat="none"):
    due = parse_local_datetime(when)
    if due <= now_local():
        raise ValueError("Please choose a future date and time.")
    repeat = str(repeat or "none").lower()
    if repeat not in ("none", "daily", "weekly"):
        raise ValueError("Repeat must be none, daily, or weekly.")
    item_id = uuid.uuid4().hex[:8]
    item = {
        "id": item_id,
        "kind": kind,
        "message": str(message or ("Alarm" if kind == "alarm" else "Reminder")),
        "due_at": due.isoformat(),
        "repeat": repeat,
        "created_at": now_local().isoformat()
    }
    with lock:
        scheduled_items.append(item)
        save_scheduled_items()
    return {
        "status": "scheduled",
        "id": item_id,
        "type": kind,
        "message": item["message"],
        "date_time": due.strftime("%A, %d %B %Y at %I:%M %p"),
        "repeat": repeat
    }


def set_alarm(time_or_datetime, label="Alarm", repeat="none"):
    value = str(time_or_datetime).strip()
    # For a time-only value, schedule the next occurrence.
    parsed = None
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            parsed_time = datetime.strptime(value, fmt).time()
            current = now_local()
            parsed = current.replace(
                hour=parsed_time.hour, minute=parsed_time.minute,
                second=0, microsecond=0
            )
            if parsed <= current:
                parsed += timedelta(days=1)
            break
        except ValueError:
            continue
    if parsed is None:
        parsed = parse_local_datetime(value)
    return add_scheduled_item("alarm", label, parsed.isoformat(), repeat)


def add_reminder(message, date_time, repeat="none"):
    return add_scheduled_item("reminder", message, date_time, repeat)


def list_reminders():
    with lock:
        items = sorted(scheduled_items, key=lambda x: x.get("due_at", ""))
        result = []
        for item in items:
            try:
                due = datetime.fromisoformat(item["due_at"]).astimezone()
                due_text = due.strftime("%d %b %Y, %I:%M %p")
            except Exception:
                due_text = item.get("due_at", "Unknown")
            result.append({
                "id": item.get("id"),
                "type": item.get("kind"),
                "message": item.get("message"),
                "date_time": due_text,
                "repeat": item.get("repeat", "none")
            })
    return {"scheduled_items": result, "count": len(result)}


def delete_reminder(item_id):
    with lock:
        before = len(scheduled_items)
        scheduled_items[:] = [x for x in scheduled_items if x.get("id") != str(item_id)]
        changed = len(scheduled_items) != before
        if changed:
            save_scheduled_items()
    return {"status": "deleted" if changed else "not_found", "id": str(item_id)}


def get_local_time():
    current = now_local()
    return {
        "time": current.strftime("%I:%M:%S %p"),
        "date": current.strftime("%A, %d %B %Y"),
        "timezone": str(current.tzinfo)
    }


def scheduler_loop():
    while True:
        current_ts = time.time()
        due_timers = []
        due_scheduled = []
        milestone_speech = []
        with lock:
            for timer_id, item in list(timers.items()):
                remaining = max(0, int(item["due_at"] - current_ts))
                duration = int(item.get("duration_seconds", 0))
                announced = item.setdefault("announced", [])

                # For a 60-second timer: announce at 30 seconds and 10 seconds.
                # For longer timers: announce halfway, then at 5 minutes,
                # 1 minute, 30 seconds and 10 seconds where applicable.
                milestones = set()
                if duration >= 40:
                    milestones.add(max(10, duration // 2))
                for mark in (300, 60, 30, 10):
                    if duration > mark and mark < duration:
                        milestones.add(mark)

                for mark in sorted(milestones, reverse=True):
                    if remaining <= mark and mark not in announced:
                        announced.append(mark)
                        if mark == duration // 2 and duration < 300:
                            phrase = f"{item['label']}: halfway done. {mark} seconds remaining."
                        else:
                            phrase = f"{item['label']}: {mark} seconds remaining."
                        milestone_speech.append(phrase)

                if current_ts >= item["due_at"]:
                    due_timers.append(timers.pop(timer_id))

            for item in list(scheduled_items):
                try:
                    due = datetime.fromisoformat(item["due_at"]).astimezone()
                except Exception:
                    continue
                if now_local() >= due:
                    due_scheduled.append(dict(item))
                    repeat = item.get("repeat", "none")
                    if repeat == "daily":
                        next_due = due + timedelta(days=1)
                        item["due_at"] = next_due.isoformat()
                    elif repeat == "weekly":
                        next_due = due + timedelta(days=7)
                        item["due_at"] = next_due.isoformat()
                    else:
                        scheduled_items.remove(item)
                    save_scheduled_items()

        for phrase in milestone_speech:
            logging.info("Timer announcement: %s", phrase)
            speak_local(phrase)

        for item in due_timers:
            notify_due("Xiaozhi Timer Finished", f"{item['label']} is complete!", "timer_complete")
        for item in due_scheduled:
            title = "Xiaozhi Alarm" if item.get("kind") == "alarm" else "Xiaozhi Reminder"
            notify_due(title, item.get("message", "Your scheduled event is due."), item.get("kind", "reminder"))
        time.sleep(CHECK_INTERVAL_SECONDS)



# ========================= WINDOWS DASHBOARD =========================
class TimeAssistantDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Xiaozhi Personal Time Assistant")
        self.root.geometry("900x650")
        self.root.minsize(760, 540)
        self.root.configure(bg="#101827")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=29, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=7)
        style.configure("TLabel", font=("Segoe UI", 10))

        header = tk.Frame(root, bg="#101827")
        header.pack(fill="x", padx=22, pady=(18, 8))
        tk.Label(
            header, text="XIAOZHI TIME ASSISTANT",
            bg="#101827", fg="#f8fafc",
            font=("Segoe UI", 19, "bold")
        ).pack(anchor="w")
        self.connection_label = tk.Label(
            header, text="Starting MCP connection...",
            bg="#101827", fg="#93c5fd", font=("Segoe UI", 10)
        )
        self.connection_label.pack(anchor="w", pady=(4, 0))

        self.clock_label = tk.Label(
            root, text="", bg="#101827", fg="#cbd5e1",
            font=("Segoe UI", 12)
        )
        self.clock_label.pack(anchor="w", padx=24, pady=(4, 12))

        content = tk.Frame(root, bg="#101827")
        content.pack(fill="both", expand=True, padx=22, pady=5)

        timer_frame = tk.LabelFrame(
            content, text=" Active timers ", bg="#172337",
            fg="#f8fafc", font=("Segoe UI", 11, "bold"),
            padx=10, pady=10
        )
        timer_frame.pack(fill="both", expand=True, pady=(0, 12))

        columns = ("label", "remaining", "id")
        self.timer_tree = ttk.Treeview(
            timer_frame, columns=columns, show="headings", height=6
        )
        self.timer_tree.heading("label", text="Timer")
        self.timer_tree.heading("remaining", text="Time remaining")
        self.timer_tree.heading("id", text="Timer ID")
        self.timer_tree.column("label", width=300)
        self.timer_tree.column("remaining", width=180, anchor="center")
        self.timer_tree.column("id", width=150, anchor="center")
        self.timer_tree.pack(fill="both", expand=True)

        timer_buttons = tk.Frame(timer_frame, bg="#172337")
        timer_buttons.pack(fill="x", pady=(9, 0))
        ttk.Button(
            timer_buttons, text="Cancel selected timer",
            command=self.cancel_selected_timer
        ).pack(side="left")
        ttk.Button(
            timer_buttons, text="Cancel all timers",
            command=self.cancel_all
        ).pack(side="left", padx=8)

        lower = tk.Frame(content, bg="#101827")
        lower.pack(fill="x")

        sw_frame = tk.LabelFrame(
            lower, text=" Stopwatch ", bg="#172337",
            fg="#f8fafc", font=("Segoe UI", 11, "bold"),
            padx=12, pady=10
        )
        sw_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.stopwatch_label = tk.Label(
            sw_frame, text="00:00:00.000", bg="#172337",
            fg="#67e8f9", font=("Consolas", 25, "bold")
        )
        self.stopwatch_label.pack(pady=8)
        ttk.Button(
            sw_frame, text="Start / Resume", command=start_stopwatch
        ).pack(side="left", padx=3)
        ttk.Button(
            sw_frame, text="Pause", command=pause_stopwatch
        ).pack(side="left", padx=3)
        ttk.Button(
            sw_frame, text="Stop / Reset", command=stop_stopwatch
        ).pack(side="left", padx=3)

        reminder_frame = tk.LabelFrame(
            lower, text=" Upcoming reminders and alarms ",
            bg="#172337", fg="#f8fafc",
            font=("Segoe UI", 11, "bold"), padx=12, pady=10
        )
        reminder_frame.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.reminder_list = tk.Listbox(
            reminder_frame, height=6, font=("Segoe UI", 9),
            bg="#0f172a", fg="#e2e8f0",
            selectbackground="#2563eb", relief="flat"
        )
        self.reminder_list.pack(fill="both", expand=True)

        footer = tk.Label(
            root,
            text="Voice commands are handled by Xiaozhi. Keep this dashboard open.",
            bg="#101827", fg="#94a3b8", font=("Segoe UI", 9)
        )
        footer.pack(anchor="w", padx=24, pady=(8, 15))

        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def cancel_selected_timer(self):
        selected = self.timer_tree.selection()
        if not selected:
            messagebox.showinfo("Select timer", "Select a timer first.")
            return
        values = self.timer_tree.item(selected[0], "values")
        result = cancel_timer(values[2])
        self.refresh()
        messagebox.showinfo("Timer", result.get("status", "Done"))

    def cancel_all(self):
        if messagebox.askyesno("Cancel timers", "Cancel all active timers?"):
            cancel_all_timers()
            self.refresh()

    def refresh(self):
        self.clock_label.config(
            text="Local time: " + now_local().strftime("%A, %d %B %Y   |   %I:%M:%S %p")
        )

        for row in self.timer_tree.get_children():
            self.timer_tree.delete(row)
        with lock:
            timer_snapshot = list(timers.values())
        for item in timer_snapshot:
            remaining = max(0, item["due_at"] - time.time())
            mins, secs = divmod(int(remaining), 60)
            hours, mins = divmod(mins, 60)
            display = f"{hours:02d}:{mins:02d}:{secs:02d}"
            self.timer_tree.insert(
                "", "end",
                values=(item["label"], display, item["id"])
            )

        with lock:
            elapsed = stopwatch_elapsed()
            is_running = stopwatch["running"]
            items = sorted(scheduled_items, key=lambda x: x.get("due_at", ""))[:8]
        millis = int((elapsed - int(elapsed)) * 1000)
        self.stopwatch_label.config(
            text=f"{format_duration(elapsed)}.{millis:03d}"
        )

        self.reminder_list.delete(0, tk.END)
        for item in items:
            try:
                dt = datetime.fromisoformat(item["due_at"]).astimezone()
                date_text = dt.strftime("%d %b %I:%M %p")
            except Exception:
                date_text = item.get("due_at", "")
            self.reminder_list.insert(
                tk.END,
                f"{item.get('kind', 'reminder').title()}: "
                f"{item.get('message', '')}  |  {date_text}"
            )

        self.root.after(200, self.refresh)

    def set_connection(self, connected):
        self.connection_label.config(
            text="MCP connected to Xiaozhi" if connected else "MCP disconnected — reconnecting",
            fg="#86efac" if connected else "#fca5a5"
        )

    def close(self):
        if messagebox.askyesno(
            "Close dashboard",
            "Closing this window will stop the assistant and its timers. Exit?"
        ):
            self.root.destroy()


# ========================= MCP TOOL DEFINITIONS =========================
TOOLS = [
    {
        "name": "set_timer",
        "description": "Start a countdown timer on the Windows computer. Use duration_seconds for the requested duration and an optional label.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "integer", "minimum": 1, "description": "Timer duration in seconds."},
                "label": {"type": "string", "description": "Optional timer name, such as tea or study."}
            },
            "required": ["duration_seconds"]
        }
    },
    {
        "name": "list_timers",
        "description": "List all currently running countdown timers and their remaining seconds.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "cancel_timer",
        "description": "Cancel a specific timer using its timer_id. If only one timer exists, timer_id may be omitted.",
        "inputSchema": {
            "type": "object",
            "properties": {"timer_id": {"type": "string", "description": "ID of the timer to cancel."}}
        }
    },
    {
        "name": "cancel_all_timers",
        "description": "Cancel every currently active countdown timer.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "start_stopwatch",
        "description": "Start the Windows stopwatch, or continue it if it was paused.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "pause_stopwatch",
        "description": "Pause the running stopwatch without resetting its elapsed time.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "resume_stopwatch",
        "description": "Resume a paused stopwatch.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "stop_stopwatch",
        "description": "Stop and reset the stopwatch, and report its final elapsed time.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "lap_stopwatch",
        "description": "Record a lap on the running stopwatch.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "set_alarm",
        "description": "Set a Windows alarm. Accepts a time such as 6:30 AM or a full local date and time such as 2026-11-16 06:30. Optional repeat is none, daily, or weekly.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "time_or_datetime": {"type": "string", "description": "Alarm time or full local date and time."},
                "label": {"type": "string", "description": "Alarm label."},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly"]}
            },
            "required": ["time_or_datetime"]
        }
    },
    {
        "name": "add_reminder",
        "description": "Create a persistent Windows reminder for a future local date and time. Use YYYY-MM-DD HH:MM format. Optional repeat is none, daily, or weekly.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What the user should be reminded about."},
                "date_time": {"type": "string", "description": "Local date and time, for example 2026-11-15 17:00."},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly"]}
            },
            "required": ["message", "date_time"]
        }
    },
    {
        "name": "list_reminders",
        "description": "List all saved future alarms and reminders.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "delete_reminder",
        "description": "Delete an alarm or reminder by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "string", "description": "ID of the alarm or reminder."}},
            "required": ["item_id"]
        }
    },
    {
        "name": "get_local_time",
        "description": "Get current local Windows time, date, weekday and timezone.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "send_current_time_to_telegram",
        "description": "Send current local time, date, weekday and timezone to configured Telegram.",
        "inputSchema": {"type": "object", "properties": {}}
    }
]


def dispatch_tool(name, args):
    if name == "set_timer":
        return set_timer(args["duration_seconds"], args.get("label", "Timer"))
    if name == "list_timers":
        return list_timers()
    if name == "cancel_timer":
        return cancel_timer(args.get("timer_id"))
    if name == "cancel_all_timers":
        return cancel_all_timers()
    if name == "start_stopwatch":
        return start_stopwatch()
    if name == "pause_stopwatch":
        return pause_stopwatch()
    if name == "resume_stopwatch":
        return resume_stopwatch()
    if name == "stop_stopwatch":
        return stop_stopwatch()
    if name == "lap_stopwatch":
        return lap_stopwatch()
    if name == "set_alarm":
        return set_alarm(args["time_or_datetime"], args.get("label", "Alarm"), args.get("repeat", "none"))
    if name == "add_reminder":
        return add_reminder(args["message"], args["date_time"], args.get("repeat", "none"))
    if name == "list_reminders":
        return list_reminders()
    if name == "delete_reminder":
        return delete_reminder(args["item_id"])
    if name == "get_local_time":
        return get_local_time()
    if name == "send_current_time_to_telegram":
        return send_current_time_to_telegram()
    raise ValueError(f"Unknown tool: {name}")


def send_jsonrpc(ws, message):
    ws.send(json.dumps(message, ensure_ascii=False))


def on_message(ws, message):
    try:
        data = json.loads(message)
        msg_id = data.get("id")
        method = data.get("method") or data.get("type")

        if method == "initialize":
            send_jsonrpc(ws, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xiaozhi-windows-time-assistant", "version": "1.0.0"}
                }
            })
        elif method == "notifications/initialized":
            logging.info("Xiaozhi MCP initialization completed.")
        elif method == "ping":
            send_jsonrpc(ws, {"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            send_jsonrpc(ws, {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = data.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            logging.info("Tool call: %s | %s", tool_name, args)
            try:
                result = dispatch_tool(tool_name, args)
                text_out = json.dumps(result, ensure_ascii=False)
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": text_out}], "isError": False}
                }
            except Exception as exc:
                logging.exception("Tool execution failed")
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Sorry, I couldn't complete that request: {exc}"}],
                        "isError": True
                    }
                }
            send_jsonrpc(ws, response)
    except Exception:
        logging.exception("Error processing Xiaozhi message")


def on_open(ws):
    logging.info("Connected to Xiaozhi MCP endpoint.")
    logging.info("Windows Time Assistant is ready. Keep this window running.")
    if dashboard is not None:
        dashboard.root.after(0, dashboard.set_connection, True)


def on_error(ws, error):
    logging.error("WebSocket error: %s", error)
    if dashboard is not None:
        dashboard.root.after(0, dashboard.set_connection, False)


dashboard = None


def run_client():
    while True:
        try:
            logging.info("Connecting to Xiaozhi MCP endpoint...")
            ws = websocket.WebSocketApp(
                XIAOZHI_WSS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error
            )
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except KeyboardInterrupt:
            logging.info("Stopping assistant.")
            break
        except Exception:
            logging.exception("Connection loop error")
        logging.warning("Disconnected. Reconnecting in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    if not XIAOZHI_WSS_URL.startswith("wss://"):
        print("Set XIAOZHI_WSS_URL in CMD to your NEW Xiaozhi MCP endpoint.")
        raise SystemExit(1)

    load_scheduled_items()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=speech_worker, daemon=True).start()
    if TELEGRAM_ENABLED:
        threading.Thread(target=telegram_command_loop, daemon=True).start()
    else:
        logging.info("Telegram not configured; local alerts remain enabled.")

    print("=" * 64)
    print("XIAOZHI PERSONAL TIME ASSISTANT")
    print("Timers | Stopwatch | Alarms | Reminders | Windows Voice | Telegram")
    print("Scheduled items are saved in:", DATA_FILE)
    print("=" * 64)

    # Run the MCP connection in the background so the Tkinter dashboard
    # remains responsive.
    threading.Thread(target=run_client, daemon=True).start()

    root = tk.Tk()
    dashboard = TimeAssistantDashboard(root)
    root.mainloop()

