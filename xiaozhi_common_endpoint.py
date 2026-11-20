"""
XIAOZHI ALL-IN-ONE MCP + TELEGRAM BRIDGE
---------------------------------------
Keeps the original YouTube, Maps, Directions, Telegram conversation-sharing,
and friend-messaging tools, while adding optional sharing of the latest
YouTube / Maps / Directions result to your own Telegram or a registered friend.

Install:
    py -m pip install websocket-client requests yt-dlp

Set ONE common Xiaozhi MCP endpoint and Telegram settings in Windows CMD:
    set XIAOZHI_WSS_URL=wss://api.xiaozhi.me/mcp/?token=...
    set TELEGRAM_BOT_TOKEN=...
    set TELEGRAM_CHAT_ID=...

Optional:
    set TELEGRAM_CONTACTS_FILE=xiaozhi_telegram_friends.json

Run:
    py xiaozhi_all_in_one.py

IMPORTANT:
- Configure the single MCP endpoint in Xiaozhi to use this combined server.
- Generate a fresh endpoint token; tokens in previously shared scripts should
  be treated as exposed.
- Keep your Telegram bot token private.
- One WebSocket connection exposes all tools: YouTube, Maps, Directions,
  Telegram conversation sharing, friend messaging, and result sharing.
"""

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from html import escape

import websocket
import requests
import yt_dlp


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

XIAOZHI_WSS_URL = os.getenv("XIAOZHI_WSS_URL", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CONTACTS_FILE = os.getenv(
    "TELEGRAM_CONTACTS_FILE", "xiaozhi_telegram_friends.json"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# Latest results are shared only when Xiaozhi invokes an explicit share tool.
LATEST_RESULTS = {
    "youtube": None,
    "maps": None,
    "directions": None,
}
LATEST_LOCK = threading.Lock()


def remember_result(feature, data):
    with LATEST_LOCK:
        LATEST_RESULTS[feature] = dict(data)


def get_latest_result(feature):
    with LATEST_LOCK:
        value = LATEST_RESULTS.get(feature)
        return dict(value) if value else None


# ---------------------------------------------------------------------------
# TELEGRAM CORE (shared by every feature)
# ---------------------------------------------------------------------------

def telegram_api(method, data=None, timeout=20):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN first.")
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=data or {},
        timeout=timeout
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error."))
    return payload.get("result")


def telegram_send(message, chat_id=None):
    target = str(chat_id or TELEGRAM_CHAT_ID).strip()
    if not TELEGRAM_BOT_TOKEN or not target:
        return False, (
            "Telegram is not configured. Set TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID."
        )
    try:
        telegram_api(
            "sendMessage",
            {
                "chat_id": target,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15
        )
        return True, "Sent."
    except Exception as exc:
        logging.exception("Telegram send failed")
        return False, str(exc)


def load_friends():
    try:
        with open(CONTACTS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception("Could not read Telegram contacts file")
        return {}


def save_friends(friends):
    temp_file = CONTACTS_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as handle:
        json.dump(friends, handle, ensure_ascii=False, indent=2)
    os.replace(temp_file, CONTACTS_FILE)


def friend_display_name(user):
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    return (first + " " + last).strip() or user.get("username") or "Telegram friend"


def send_conversation_to_telegram(user_words="", xiaozhi_reply="", conversation=None):
    """Send a chronological, turn-by-turn transcript to the owner's Telegram.

    Preferred input is conversation=[{"role": "user"|"assistant", "content": "..."}, ...].
    The two legacy string arguments remain supported for older tool callers.
    """
    turns = []
    if isinstance(conversation, list):
        for item in conversation:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", item.get("text", "")) or "").strip()
            if not content:
                continue
            if role in ("assistant", "bot", "xiaozhi"):
                label, icon = "XIAOZHI", "🤖"
            elif role in ("user", "human"):
                label, icon = "YOU", "🗣️"
            else:
                continue
            turns.append((label, icon, content))

    # Backward compatibility: old clients only send one user string and one reply.
    if not turns:
        user_text = str(user_words or "").strip()
        reply_text = str(xiaozhi_reply or "").strip()
        if user_text:
            turns.append(("YOU", "🗣️", user_text))
        if reply_text:
            turns.append(("XIAOZHI", "🤖", reply_text))

    if not turns:
        return {"status": "empty_message", "message": "There is no conversation text to send."}

    # Keep the complete turn order while staying safely below Telegram's message limit.
    max_content_chars = 3300
    rendered = []
    used = 0
    for label, icon, content in turns:
        block = f"{icon} <b>{label}:</b>\n<blockquote>{escape(content)}</blockquote>"
        if used + len(block) + 2 > max_content_chars:
            remaining = max_content_chars - used - 100
            if remaining > 40:
                short_content = content[:remaining].rstrip() + "…"
                rendered.append(
                    f"{icon} <b>{label}:</b>\n<blockquote>{escape(short_content)}</blockquote>"
                )
            rendered.append("<i>Earlier/later conversation text was shortened to fit Telegram.</i>")
            break
        rendered.append(block)
        used += len(block) + 2

    timestamp = datetime.now().astimezone().strftime("%d %b %Y • %I:%M %p")
    message = (
        "✨ <b>XIAOZHI CONVERSATION</b> ✨\n"
        "<i>A little moment worth keeping</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n".join(rendered)
        + "\n━━━━━━━━━━━━━━━━━━━━\n"
        + f"🕰️ <i>{timestamp}</i>\n"
        + "💫 <i>Shared with Telegram</i>"
    )
    sent, detail = telegram_send(message)
    return {
        "status": "sent" if sent else "failed",
        "message": "Your conversation was sent to Telegram in chronological, turn-by-turn order." if sent else detail,
    }


def send_message_to_friend(friend_name, message_text):
    friend_name = str(friend_name or "").strip()
    message_text = str(message_text or "").strip()
    if not friend_name or not message_text:
        return {
            "status": "missing_details",
            "message": "Provide both the friend's name and the message."
        }

    friends = load_friends()
    wanted = friend_name.casefold()
    matches = []
    for friend in friends.values():
        names = [
            friend.get("friendly_name", ""),
            friend.get("telegram_name", ""),
            friend.get("username", ""),
        ]
        if any(name and wanted == name.casefold() for name in names):
            matches.append(friend)

    if not matches:
        available = [
            f.get("friendly_name") or f.get("telegram_name")
            or f.get("username") or "Friend"
            for f in friends.values()
        ]
        return {
            "status": "friend_not_found",
            "message": (
                f"I couldn't find '{friend_name}'. Ask that person to open "
                "your Telegram bot and press Start. Registered contacts: "
                f"{', '.join(available) if available else 'none yet'}."
            )
        }

    if len(matches) > 1:
        choices = [
            f"{f.get('friendly_name') or f.get('telegram_name')} "
            f"(@{f.get('username') or 'no username'})"
            for f in matches
        ]
        return {
            "status": "ambiguous_friend",
            "message": "More than one contact matches. Please clarify: "
                       + "; ".join(choices)
        }

    friend = matches[0]
    alias = friend.get("friendly_name") or friend.get("telegram_name") or friend_name
    timestamp = datetime.now().astimezone().strftime("%d %b %Y • %I:%M %p")
    formatted = (
        "💌 <b>A MESSAGE FOR YOU</b> 💌\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ <b>{escape(alias)}</b>,\n\n"
        f"<blockquote>{escape(message_text[:3500])}</blockquote>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕰️ <i>{timestamp}</i>\n"
        "💫 <i>Sent with Xiaozhi</i>"
    )
    sent, detail = telegram_send(formatted, friend["chat_id"])
    return {
        "status": "sent" if sent else "failed",
        "message": f"Message sent to {alias}." if sent else detail
    }


def format_result_message(feature, data):
    timestamp = datetime.now().astimezone().strftime("%d %b %Y • %I:%M %p")

    if feature == "youtube":
        title = escape(str(data.get("title", "Unknown title")))
        artist = escape(str(data.get("uploader", "Unknown artist")))
        link = escape(str(data.get("url", "")), quote=True)
        return (
            "🎵 <b>XIAOZHI • YOUTUBE MUSIC</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎧 <b>Song:</b> {title}\n"
            f"🎤 <b>Artist:</b> {artist}\n"
            f'▶️ <a href="{link}">Open on YouTube</a>\n'
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕰️ <i>{timestamp}</i>"
        )

    if feature == "maps":
        place = escape(str(data.get("place", "Unknown location")))
        link = escape(str(data.get("url", "")), quote=True)
        return (
            "📍 <b>XIAOZHI • MAPS LOCATION</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🌍 <b>Location / Search:</b> {place}\n"
            f'🗺️ <a href="{link}">Open in Google Maps</a>\n'
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕰️ <i>{timestamp}</i>"
        )

    if feature == "directions":
        origin = escape(str(data.get("origin", "Unknown")))
        destination = escape(str(data.get("destination", "Unknown")))
        link = escape(str(data.get("url", "")), quote=True)
        return (
            "🧭 <b>XIAOZHI • DIRECTIONS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>From:</b> {origin}\n"
            f"🏁 <b>To:</b> {destination}\n"
            f'🗺️ <a href="{link}">Open route in Google Maps</a>\n'
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕰️ <i>{timestamp}</i>"
        )

    raise ValueError("Unsupported feature.")


def share_latest_result(feature, friend_name=None):
    data = get_latest_result(feature)
    if not data:
        return {
            "status": "no_recent_result",
            "message": f"There is no recent {feature} result to share."
        }

    formatted = format_result_message(feature, data)
    if friend_name:
        return send_message_to_friend(friend_name, formatted)

    sent, detail = telegram_send(formatted)
    return {
        "status": "sent" if sent else "failed",
        "message": (
            f"The latest {feature} result was sent to your Telegram."
            if sent else detail
        )
    }


# ---------------------------------------------------------------------------
# TELEGRAM FRIEND REGISTRATION LISTENER (same behavior as original)
# ---------------------------------------------------------------------------

def handle_telegram_updates():
    offset = 0
    while True:
        try:
            updates = telegram_api(
                "getUpdates",
                {"offset": offset, "timeout": 25},
                timeout=35
            )
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue

                chat = msg.get("chat") or {}
                sender = msg.get("from") or {}
                chat_id = str(chat.get("id", ""))
                text_in = (msg.get("text") or "").strip()
                if not chat_id or chat.get("type") != "private":
                    continue

                friends = load_friends()
                if text_in.startswith("/start"):
                    user = {
                        "chat_id": chat_id,
                        "telegram_name": friend_display_name(sender),
                        "username": sender.get("username", ""),
                        "friendly_name": friends.get(chat_id, {}).get(
                            "friendly_name", ""
                        ),
                    }
                    friends[chat_id] = user
                    save_friends(friends)
                    telegram_send(
                        "✨ <b>Xiaozhi connection received!</b>\n"
                        "You're now available as a Telegram contact for the bot owner. 💫",
                        chat_id
                    )
                    if str(chat_id) != str(TELEGRAM_CHAT_ID):
                        telegram_send(
                            "👤 <b>New Telegram friend registered</b>\n"
                            f"Name: {escape(user['telegram_name'])}\n"
                            f"Username: @{escape(user['username']) if user['username'] else 'not set'}\n"
                            f"Chat ID: <code>{escape(chat_id)}</code>\n\n"
                            "Set a nickname with:\n"
                            f"<code>/rename {escape(chat_id)} Rahul</code>",
                            TELEGRAM_CHAT_ID
                        )
                    continue

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text_in == "/friends":
                    if not friends:
                        telegram_send(
                            "No friends registered yet. Ask a friend to open "
                            "your bot and press Start.",
                            chat_id
                        )
                    else:
                        lines = ["👥 <b>Your Telegram friends</b>"]
                        for item in friends.values():
                            alias = (
                                item.get("friendly_name")
                                or item.get("telegram_name") or "Friend"
                            )
                            lines.append(
                                f"• <b>{escape(alias)}</b> — "
                                f"<code>{escape(item['chat_id'])}</code>"
                            )
                        telegram_send("\n".join(lines), chat_id)
                elif text_in.startswith("/rename "):
                    parts = text_in.split(maxsplit=2)
                    if len(parts) < 3:
                        telegram_send(
                            "Use: <code>/rename CHAT_ID Friendly Name</code>",
                            chat_id
                        )
                    else:
                        target_id, alias = parts[1], parts[2].strip()
                        if target_id not in friends:
                            telegram_send(
                                "I can't find that registered contact. "
                                "Ask them to press Start first.",
                                chat_id
                            )
                        else:
                            friends[target_id]["friendly_name"] = alias
                            save_friends(friends)
                            telegram_send(
                                f"✅ Contact saved as <b>{escape(alias)}</b>.",
                                chat_id
                            )
                elif text_in == "/help":
                    telegram_send(
                        "🛠️ <b>Xiaozhi contact commands</b>\n"
                        "/friends — list registered contacts\n"
                        "/rename CHAT_ID Name — give a friend a nickname\n"
                        "Friends must open the bot and press Start first.",
                        chat_id
                    )
        except Exception:
            logging.exception("Telegram contact polling failed")
            time.sleep(5)


# ---------------------------------------------------------------------------
# ORIGINAL YOUTUBE FEATURE + SHARE TOOLS
# ---------------------------------------------------------------------------

def play_song_on_laptop(query):
    logging.info("Searching YouTube for: %r", query)
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "default_search": "ytsearch1",
            "js_runtimes": {"node": {}},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            entry = info["entries"][0] if info.get("entries") else info
            title = entry.get("title", "Unknown Title")
            uploader = entry.get("uploader", "Unknown Artist")
            webpage_url = entry.get("webpage_url")

        if webpage_url:
            webbrowser.open(webpage_url)
            result = {
                "status": "success",
                "title": title,
                "uploader": uploader,
                "url": webpage_url,
            }
            remember_result("youtube", result)
            logging.info("Launched %r on laptop.", title)
            return result
        return {"status": "error", "message": "URL not found"}
    except Exception as exc:
        logging.exception("YouTube error")
        return {"status": "error", "message": str(exc)}


YOUTUBE_TOOLS = [
    {
        "name": "play_youtube_music",
        "description": "Play music from YouTube on the laptop speakers. This original feature is unchanged.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_name": {"type": "string", "description": "Song to search and play."}
            },
            "required": ["song_name"],
        },
    },
    {
        "name": "share_last_youtube_to_telegram",
        "description": "ONLY when the user explicitly asks to send/share the just-played YouTube song to their own Telegram. Sends song title, artist and link.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "share_last_youtube_to_friend",
        "description": "ONLY when the user explicitly asks to send/share the just-played YouTube song to a named registered Telegram friend. Ask which friend if unclear.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "friend_name": {"type": "string", "description": "Registered friend's name or nickname."}
            },
            "required": ["friend_name"],
        },
    },
]


def handle_youtube_message(ws, message):
    try:
        data = json.loads(message)
        msg_id = data.get("id")
        method = data.get("method") or data.get("type")

        if method == "initialize":
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Laptop-Audio-Player-MCP", "version": "3.1.0"},
                },
            }))
        elif method == "tools/list":
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": YOUTUBE_TOOLS},
            }))
        elif method == "tools/call":
            params = data.get("params", {})
            name = params.get("name")
            args = params.get("arguments") or {}

            if name == "play_youtube_music":
                query = args.get("song_name", "")
                result = play_song_on_laptop(query)
                if result["status"] == "success":
                    text_out = (
                        f"Playing {result['title']} by {result['uploader']} "
                        "on your laptop now!"
                    )
                else:
                    text_out = f"Sorry, could not play {query}. {result.get('message', '')}"
                output = {"status": result["status"], "message": text_out}
            elif name == "share_last_youtube_to_telegram":
                output = share_latest_result("youtube")
            elif name == "share_last_youtube_to_friend":
                output = share_latest_result("youtube", args.get("friend_name"))
            else:
                output = {"status": "error", "message": "Unknown YouTube tool."}

            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "isError": output.get("status") in ("failed", "error"),
                },
            }))
    except Exception:
        logging.exception("YouTube MCP message handling failed")


# ---------------------------------------------------------------------------
# ORIGINAL MAPS FEATURE + SHARE TOOLS
# ---------------------------------------------------------------------------

def get_ip_location():
    """Approximate city/region based on public IP; not GPS-precise."""
    try:
        req = urllib.request.Request(
            "https://ipinfo.io/json",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            return {
                "city": data.get("city", "Unknown City"),
                "region": data.get("region", "Unknown Region"),
            }
    except Exception:
        return None


def open_google_maps(query):
    try:
        if "current location" in query.lower() or "my location" in query.lower():
            loc = get_ip_location()
            if loc:
                place_str = f"{loc['city']}, {loc['region']}"
                maps_url = (
                    "https://www.google.com/maps/search/"
                    + urllib.parse.quote(place_str)
                )
                webbrowser.open(maps_url)
                remember_result("maps", {"place": place_str, "url": maps_url})
                return f"I have opened Google Maps. Your current location is {place_str}."

        maps_url = (
            "https://www.google.com/maps/search/"
            + urllib.parse.quote(query)
        )
        webbrowser.open(maps_url)
        remember_result("maps", {"place": query, "url": maps_url})
        return f"I have opened Google Maps on your screen for {query}."
    except Exception:
        logging.exception("Error opening Google Maps")
        return f"I tried to open Google Maps for {query}, but encountered an error."


MAPS_TOOLS = [
    {
        "name": "get_live_location_info",
        "description": "Opens Google Maps on the laptop for a given location or place and reads out the details. For 'my/current location', uses approximate public-IP city/region.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Location, place name, or search query."}
            },
            "required": ["address"],
        },
    },
    {
        "name": "share_last_maps_to_telegram",
        "description": "ONLY when the user explicitly asks to send/share the most recent Maps place or current-location result to their own Telegram.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "share_last_maps_to_friend",
        "description": "ONLY when the user explicitly asks to send/share the most recent Maps place or location result to a named registered Telegram friend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "friend_name": {"type": "string", "description": "Registered friend's name or nickname."}
            },
            "required": ["friend_name"],
        },
    },
]


def handle_maps_message(ws, message):
    try:
        data = json.loads(message)
        msg_id = data.get("id")
        method = data.get("method") or data.get("type")

        if method == "initialize":
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xiaozhi-simple-maps", "version": "1.1.0"},
                },
            }
            ws.send(json.dumps(response))
        elif method in ("notifications/initialized",):
            return
        elif method == "ping":
            ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))
        elif method == "tools/list":
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": MAPS_TOOLS},
            }))
        elif method == "tools/call":
            params = data.get("params", {})
            name = params.get("name")
            args = params.get("arguments") or {}

            if name == "get_live_location_info":
                speech_output = open_google_maps(args.get("address", "Google Maps"))
                output = {"status": "success", "message": speech_output}
            elif name == "share_last_maps_to_telegram":
                output = share_latest_result("maps")
            elif name == "share_last_maps_to_friend":
                output = share_latest_result("maps", args.get("friend_name"))
            else:
                output = {"status": "error", "message": "Unknown Maps tool."}

            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "isError": output.get("status") in ("failed", "error"),
                },
            }))
    except Exception:
        logging.exception("Maps MCP message handling failed")


# ---------------------------------------------------------------------------
# ORIGINAL DIRECTIONS FEATURE + SHARE TOOLS
# ---------------------------------------------------------------------------

def handle_google_maps_directions(origin, destination):
    try:
        encoded_origin = urllib.parse.quote(origin)
        encoded_destination = urllib.parse.quote(destination)
        dir_url = (
            f"https://www.google.com/maps/dir/{encoded_origin}/{encoded_destination}"
        )
        webbrowser.open(dir_url)
        remember_result(
            "directions",
            {"origin": origin, "destination": destination, "url": dir_url}
        )
        return f"I have opened directions from {origin} to {destination} on Google Maps."
    except Exception:
        logging.exception("Directions browser error")
        return (
            f"I tried to open directions from {origin} to {destination}, "
            "but encountered an error."
        )


DIRECTIONS_TOOLS = [
    {
        "name": "get_map_directions",
        "description": "Opens route directions on the laptop screen for a given origin and destination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Start location."},
                "destination": {"type": "string", "description": "Destination location."},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "share_last_directions_to_telegram",
        "description": "ONLY when the user explicitly asks to send/share the just-requested directions to their own Telegram. Sends origin, destination and Google Maps route link.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "share_last_directions_to_friend",
        "description": "ONLY when the user explicitly asks to send/share the just-requested directions to a named registered Telegram friend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "friend_name": {"type": "string", "description": "Registered friend's name or nickname."}
            },
            "required": ["friend_name"],
        },
    },
]


def handle_directions_message(ws, message):
    try:
        data = json.loads(message)
        msg_id = data.get("id")
        method = data.get("method") or data.get("type")

        if method == "initialize":
            response = {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xiaozhi-directions-mcp", "version": "1.1.0"},
                },
            }
            ws.send(json.dumps(response))
        elif method in ("notifications/initialized",):
            return
        elif method == "ping":
            ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))
        elif method == "tools/list":
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": DIRECTIONS_TOOLS},
            }))
        elif method == "tools/call":
            params = data.get("params", {})
            name = params.get("name")
            args = params.get("arguments") or {}

            if name == "get_map_directions":
                speech_output = handle_google_maps_directions(
                    args.get("origin", ""),
                    args.get("destination", "")
                )
                output = {"status": "success", "message": speech_output}
            elif name == "share_last_directions_to_telegram":
                output = share_latest_result("directions")
            elif name == "share_last_directions_to_friend":
                output = share_latest_result("directions", args.get("friend_name"))
            else:
                output = {"status": "error", "message": "Unknown Directions tool."}

            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "isError": output.get("status") in ("failed", "error"),
                },
            }))
    except Exception:
        logging.exception("Directions MCP message handling failed")


# ---------------------------------------------------------------------------
# ORIGINAL TELEGRAM MCP TOOLS + FRIEND MESSAGE FEATURE
# ---------------------------------------------------------------------------

TELEGRAM_TOOLS = [
    {
        "name": "send_conversation_to_telegram",
        "description": (
            "Use ONLY after the user explicitly asks to share the conversation to their own Telegram. "
            "Send the FULL available chat history as a chronological array of turns, alternating naturally "
            "between role='user' and role='assistant'. Do NOT group all user messages together or all "
            "assistant replies together. Preserve the original order and wording."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation": {
                    "type": "array",
                    "description": "Full available conversation in exact chronological order, one object per message.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "user_words": {"type": "string", "description": "Legacy fallback only."},
                "xiaozhi_reply": {"type": "string", "description": "Legacy fallback only."},
            },
            "required": ["conversation"],
        },
    },
    {
        "name": "send_message_to_friend",
        "description": (
            "Send a Telegram message to a registered friend by name. Use only "
            "when the user explicitly asks. If recipient or message is unclear, "
            "ask first; if multiple matches, ask the user to choose."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "friend_name": {"type": "string"},
                "message_text": {"type": "string"},
            },
            "required": ["friend_name", "message_text"],
        },
    },
]


def handle_telegram_message(ws, message):
    try:
        data = json.loads(message)
        msg_id = data.get("id")
        method = data.get("method") or data.get("type")

        if method == "initialize":
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "xiaozhi-telegram-conversation-share",
                        "version": "1.1.0",
                    },
                },
            }))
        elif method == "notifications/initialized":
            logging.info("Xiaozhi Telegram Share MCP initialized.")
        elif method == "ping":
            ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))
        elif method == "tools/list":
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": TELEGRAM_TOOLS},
            }))
        elif method == "tools/call":
            params = data.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments") or {}

            if name == "send_conversation_to_telegram":
                result = send_conversation_to_telegram(
                    args.get("user_words", ""),
                    args.get("xiaozhi_reply", ""),
                    args.get("conversation"),
                )
            elif name == "send_message_to_friend":
                result = send_message_to_friend(
                    args.get("friend_name", ""),
                    args.get("message_text", "")
                )
            else:
                result = {"status": "error", "message": f"Unknown tool: {name}"}

            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "isError": result.get("status") in ("failed", "error"),
                },
            }))
    except Exception:
        logging.exception("Telegram MCP message handling failed")


# ---------------------------------------------------------------------------
# ONE COMMON XIAOZHI MCP ENDPOINT
# ---------------------------------------------------------------------------

# Combine all original and newly-added tools into one tools/list response.
ALL_TOOLS = (
    YOUTUBE_TOOLS
    + MAPS_TOOLS
    + DIRECTIONS_TOOLS
    + TELEGRAM_TOOLS
)


def dispatch_tool(name, args):
    """Run the selected feature using the same functions as before."""

    # Original YouTube playback
    if name == "play_youtube_music":
        query = args.get("song_name", "")
        result = play_song_on_laptop(query)
        if result.get("status") == "success":
            return {
                "status": "success",
                "message": (
                    f"Playing {result['title']} by {result['uploader']} "
                    "on your laptop now!"
                ),
            }
        return {
            "status": "error",
            "message": f"Sorry, could not play {query}. {result.get('message', '')}",
        }

    # Original Maps lookup / current-location behavior
    if name == "get_live_location_info":
        return {
            "status": "success",
            "message": open_google_maps(args.get("address", "Google Maps")),
        }

    # Original directions behavior
    if name == "get_map_directions":
        return {
            "status": "success",
            "message": handle_google_maps_directions(
                args.get("origin", ""),
                args.get("destination", ""),
            ),
        }

    # YouTube result sharing
    if name == "share_last_youtube_to_telegram":
        return share_latest_result("youtube")
    if name == "share_last_youtube_to_friend":
        return share_latest_result("youtube", args.get("friend_name"))

    # Maps result sharing
    if name == "share_last_maps_to_telegram":
        return share_latest_result("maps")
    if name == "share_last_maps_to_friend":
        return share_latest_result("maps", args.get("friend_name"))

    # Directions result sharing
    if name == "share_last_directions_to_telegram":
        return share_latest_result("directions")
    if name == "share_last_directions_to_friend":
        return share_latest_result("directions", args.get("friend_name"))

    # Original Telegram conversation sharing
    if name == "send_conversation_to_telegram":
        return send_conversation_to_telegram(
            args.get("user_words", ""),
            args.get("xiaozhi_reply", ""),
            args.get("conversation"),
        )

    # Original Telegram friend messaging
    if name == "send_message_to_friend":
        return send_message_to_friend(
            args.get("friend_name", ""),
            args.get("message_text", ""),
        )

    return {"status": "error", "message": f"Unknown tool: {name}"}


def send_jsonrpc(ws, payload):
    ws.send(json.dumps(payload, ensure_ascii=False))


def on_common_message(ws, message):
    """Single MCP message handler for every feature."""
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
                    "serverInfo": {
                        "name": "xiaozhi-all-in-one",
                        "version": "4.0.0",
                    },
                },
            })

        elif method == "notifications/initialized":
            logging.info("Common Xiaozhi MCP endpoint initialized.")

        elif method == "ping":
            send_jsonrpc(ws, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {},
            })

        elif method == "tools/list":
            send_jsonrpc(ws, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": ALL_TOOLS},
            })
            logging.info("Registered %d tools on the common endpoint.", len(ALL_TOOLS))

        elif method == "tools/call":
            params = data.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            logging.info("Xiaozhi called tool: %s | args: %s", tool_name, args)

            try:
                result = dispatch_tool(tool_name, args)
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }],
                        "isError": result.get("status") in ("failed", "error"),
                    },
                }
            except Exception as exc:
                logging.exception("Tool execution failed: %s", tool_name)
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }

            send_jsonrpc(ws, response)

    except Exception:
        logging.exception("Error processing common Xiaozhi MCP message.")


def run_common_client():
    if not XIAOZHI_WSS_URL.startswith("wss://"):
        raise SystemExit(
            "Set XIAOZHI_WSS_URL to your ONE common Xiaozhi MCP endpoint."
        )

    while True:
        try:
            logging.info("Connecting to the COMMON Xiaozhi MCP endpoint...")
            ws = websocket.WebSocketApp(
                XIAOZHI_WSS_URL,
                on_message=on_common_message,
                on_open=lambda _ws: logging.info(
                    "Connected to common Xiaozhi MCP endpoint."
                ),
                on_error=lambda _ws, err: logging.error(
                    "Common MCP WebSocket error: %s", err
                ),
                on_close=lambda _ws, status, msg: logging.warning(
                    "Common MCP disconnected (%s, %s).", status, msg
                ),
            )
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except KeyboardInterrupt:
            logging.info("Stopping common Xiaozhi MCP client.")
            break
        except Exception:
            logging.exception("Common MCP connection crashed.")

        logging.warning("Reconnecting common MCP endpoint in 5 seconds...")
        time.sleep(5)


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. "
            "Set both in CMD before running."
        )

    if not XIAOZHI_WSS_URL.startswith("wss://"):
        raise SystemExit(
            "Missing XIAOZHI_WSS_URL. Set your single common MCP endpoint in CMD."
        )

    # Telegram contact polling runs in the background, as before.
    threading.Thread(
        target=handle_telegram_updates,
        name="TelegramFriendListener",
        daemon=True,
    ).start()

    logging.info("XIAOZHI ALL-IN-ONE COMMON ENDPOINT is starting.")
    logging.info("Features: YouTube, Maps, Directions, Telegram, Friends.")
    logging.info("All MCP tools will be exposed through ONE WebSocket endpoint.")

    try:
        run_common_client()
    except KeyboardInterrupt:
        logging.info("XIAOZHI ALL-IN-ONE stopped.")


if __name__ == "__main__":
    main()
