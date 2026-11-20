import asyncio
import json
import os
import threading
import time
from typing import Any, Dict

import requests
import serial
import websockets

# ============================================================
# CONFIGURATION
# Set these as environment variables before running.
# ============================================================
MCP_ENDPOINT_URL = os.getenv("XIAOZHI_MCP_ENDPOINT_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SERIAL_PORT = os.getenv("ESP32_SERIAL_PORT", "COM9").strip()
BAUD_RATE = int(os.getenv("ESP32_BAUD_RATE", "115200"))

ROOMS = ("bedroom", "living_room", "kitchen", "hall")
ROOM_LABELS = {
    "bedroom": "Bedroom",
    "living_room": "Living Room",
    "kitchen": "Kitchen",
    "hall": "Hall",
}

# ============================================================
# SHARED HARDWARE STATE
# A single background reader owns serial reads. No other
# function calls readline(), preventing serial-read conflicts.
# ============================================================
ser = None
serial_lock = threading.Lock()
state_lock = threading.Lock()
latest_sensors: Dict[str, Any] = {}
light_states = {room: "UNKNOWN" for room in ROOMS}
last_sensor_update = 0.0
serial_error = ""
stop_reader = threading.Event()


def connect_serial():
    global ser, serial_error
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        # ESP32 boards often reset when the serial port opens.
        time.sleep(2)
        ser.reset_input_buffer()
        serial_error = ""
        print(f"[Serial] Connected to ESP32 on {SERIAL_PORT} at {BAUD_RATE}.")
        return True
    except Exception as exc:
        ser = None
        serial_error = str(exc)
        print(f"[Serial] Could not open {SERIAL_PORT}: {exc}")
        return False


def send_serial_command(payload: dict) -> bool:
    """Send one newline-terminated JSON command to the ESP32."""
    if ser is None or not ser.is_open:
        return False
    try:
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with serial_lock:
            ser.write(line.encode("utf-8"))
            ser.flush()
        return True
    except Exception as exc:
        print(f"[Serial] Write error: {exc}")
        return False


def _update_from_esp32(data: dict):
    global latest_sensors, light_states, last_sensor_update

    with state_lock:
        event = data.get("event")

        if event == "touch_pressed":
            print("[Hardware Event] Touch sensor pressed.")
            # Telegram is sent outside this lock by the reader thread.
            return "touch"

        if event == "light_status":
            for room in ROOMS:
                value = data.get(room)
                if isinstance(value, str) and value.upper() in ("ON", "OFF"):
                    light_states[room] = value.upper()

        # Telemetry lines contain sensor fields and current light states.
        sensor_keys = ("temp", "humidity", "mq135", "mq3", "sound", "bpm", "spo2")
        if any(key in data for key in sensor_keys):
            latest_sensors = {key: data.get(key) for key in sensor_keys if key in data}
            last_sensor_update = time.time()

        lights = data.get("lights")
        if isinstance(lights, dict):
            for room in ROOMS:
                value = lights.get(room)
                if isinstance(value, str) and value.upper() in ("ON", "OFF"):
                    light_states[room] = value.upper()

    return None


def serial_reader():
    """The only function that reads from the serial port."""
    if ser is None:
        return

    while not stop_reader.is_set():
        try:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("{"):
                if line:
                    print(f"[ESP32] {line}")
                continue

            data = json.loads(line)
            event_type = _update_from_esp32(data)
            if event_type == "touch":
                send_telegram_message(
                    "🚨 *XIAOZHI SMART HOME ALERT*\n\n"
                    "👆 Touch sensor was pressed on your ESP32 hub."
                )
        except json.JSONDecodeError:
            print("[Serial] Ignored invalid JSON line.")
        except Exception as exc:
            if not stop_reader.is_set():
                print(f"[Serial] Reader error: {exc}")
            time.sleep(0.2)


def start_serial_reader():
    if ser is None:
        return
    thread = threading.Thread(target=serial_reader, name="esp32-serial-reader", daemon=True)
    thread.start()


def send_telegram_message(message: str) -> str:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            return f"Telegram returned an error: {result}"
        return "Telegram message sent successfully."
    except Exception as exc:
        return f"Could not send Telegram message: {exc}"


def get_state_snapshot():
    with state_lock:
        return dict(latest_sensors), dict(light_states), last_sensor_update


def format_sensor_report(sensors: dict) -> str:
    if not sensors:
        return "No sensor readings have been received from the ESP32 yet."

    def value(key, fallback="N/A"):
        item = sensors.get(key)
        return fallback if item is None else item

    try:
        bpm = round(float(value("bpm", 0)))
    except (TypeError, ValueError):
        bpm = value("bpm")
    try:
        spo2 = round(float(value("spo2", 0)))
    except (TypeError, ValueError):
        spo2 = value("spo2")

    return (
        "🌡️ *ENVIRONMENT*\n"
        f"• Temperature: {value('temp')} °C\n"
        f"• Humidity: {value('humidity')}%\n\n"
        "❤️ *HEALTH SENSORS*\n"
        f"• Heart rate: {bpm} BPM\n"
        f"• Blood oxygen: {spo2}%\n\n"
        "🌫️ *AIR / GAS SENSORS*\n"
        f"• MQ135: {value('mq135')}\n"
        f"• MQ3: {value('mq3')}\n"
        f"• Sound sensor: {value('sound')}"
    )


def format_light_report(lights: dict) -> str:
    lines = ["💡 *LIGHT STATUS*"]
    for room in ROOMS:
        state = lights.get(room, "UNKNOWN")
        icon = "🟢" if state == "ON" else ("🔴" if state == "OFF" else "⚪")
        lines.append(f"{icon} {ROOM_LABELS[room]}: *{state}*")
    return "\n".join(lines)


def control_room_light(room: str, state: str) -> str:
    room = (room or "").strip().lower().replace(" ", "_")
    state = (state or "").strip().lower()

    # Accept common room-name variants.
    aliases = {"livingroom": "living_room", "living": "living_room"}
    room = aliases.get(room, room)

    if room not in ROOMS:
        return "Unknown room. Use bedroom, living_room, kitchen, or hall."
    if state not in ("on", "off"):
        return "Invalid light state. Use on or off."

    if not send_serial_command({"cmd": "light", "room": room, "state": state}):
        return "I could not communicate with the ESP32. Check USB and the COM port."

    return f"{ROOM_LABELS[room]} light {state.upper()} command sent to the ESP32."


def control_all_lights(state: str) -> str:
    state = (state or "").strip().lower()
    if state not in ("on", "off"):
        return "Invalid light state. Use on or off."

    if not send_serial_command({"cmd": "all_lights", "state": state}):
        return "I could not communicate with the ESP32. Check USB and the COM port."

    return f"All four lights {state.upper()} command sent to the ESP32."


def get_sensor_readings(sensor_type: str) -> str:
    sensors, _, updated = get_state_snapshot()
    if not sensors:
        return "No sensor data received yet. Check that the ESP32 is powered and sending serial data."

    age = max(0, int(time.time() - updated))
    if age > 15:
        freshness = f" Warning: the last reading is {age} seconds old."
    else:
        freshness = ""

    sensor_type = (sensor_type or "all").lower()
    if sensor_type == "health":
        return (
            f"Heart rate is {sensors.get('bpm', 'N/A')} BPM and "
            f"blood oxygen is {sensors.get('spo2', 'N/A')} percent.{freshness}"
        )
    if sensor_type == "air_quality":
        return (
            f"MQ135 air sensor value is {sensors.get('mq135', 'N/A')} and "
            f"MQ3 sensor value is {sensors.get('mq3', 'N/A')}.{freshness}"
        )
    if sensor_type == "environment":
        return (
            f"Temperature is {sensors.get('temp', 'N/A')} degrees Celsius and "
            f"humidity is {sensors.get('humidity', 'N/A')} percent.{freshness}"
        )
    return format_sensor_report(sensors) + freshness


def send_smart_home_report() -> str:
    sensors, lights, updated = get_state_snapshot()
    sensor_text = format_sensor_report(sensors)
    light_text = format_light_report(lights)
    if updated:
        age = max(0, int(time.time() - updated))
        sensor_text += f"\n\n🕒 Reading age: {age} seconds"
    message = (
        "🏠 *XIAOZHI SMART HOME REPORT*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{light_text}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{sensor_text}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 _Generated by XIAOZHI SMART HOME HUB_"
    )
    return send_telegram_message(message)


def tool_definitions():
    return [
        {
            "name": "get_sensor_readings",
            "description": "Returns the latest ESP32 sensor readings. Use when the user asks about temperature, humidity, heart rate, SpO2, air quality, gas, or sensor readings. Does not send Telegram.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sensor_type": {
                        "type": "string",
                        "enum": ["health", "air_quality", "environment", "all"],
                        "description": "Which sensor category to report."
                    }
                },
                "required": ["sensor_type"]
            }
        },
        {
            "name": "control_room_light",
            "description": "Turns one room light on or off. Use for voice requests such as turn on bedroom light or switch off kitchen light.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "room": {
                        "type": "string",
                        "enum": ["bedroom", "living_room", "kitchen", "hall"],
                        "description": "Room whose LED should be controlled."
                    },
                    "state": {
                        "type": "string",
                        "enum": ["on", "off"],
                        "description": "Desired light state."
                    }
                },
                "required": ["room", "state"]
            }
        },
        {
            "name": "control_all_lights",
            "description": "Turns all four room lights on or off when the user asks to turn on or off all lights.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["on", "off"],
                        "description": "Desired state for all four lights."
                    }
                },
                "required": ["state"]
            }
        },
        {
            "name": "get_light_status",
            "description": "Reports whether the bedroom, living room, kitchen, and hall lights are on or off.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "send_smart_home_report",
            "description": "Sends the current four-light status and latest available ESP32 sensor readings to the configured Telegram chat. Use only when the user explicitly asks to send the report/status to Telegram.",
            "inputSchema": {"type": "object", "properties": {}}
        }
    ]


async def listen_to_xiaozhi_mcp():
    if not MCP_ENDPOINT_URL:
        raise RuntimeError(
            "Set XIAOZHI_MCP_ENDPOINT_URL to your complete Xiaozhi MCP WebSocket URL."
        )

    print("[Xiaozhi] Connecting to MCP endpoint...")
    async with websockets.connect(MCP_ENDPOINT_URL, subprotocols=["mcp"], ping_interval=20) as websocket:
        print("[Xiaozhi] Connected.")

        async for message in websocket:
            try:
                data = json.loads(message)
                msg_id = data.get("id")
                method = data.get("method")

                if method == "initialize":
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "xiaozhi-smart-home-hub", "version": "2.0.0"}
                        }
                    }
                    await websocket.send(json.dumps(response))

                elif method == "notifications/initialized":
                    continue

                elif method == "ping":
                    await websocket.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}}))

                elif method == "tools/list":
                    await websocket.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"tools": tool_definitions()}
                    }))

                elif method == "tools/call":
                    params = data.get("params", {})
                    tool_name = params.get("name")
                    args = params.get("arguments", {}) or {}

                    if tool_name == "get_sensor_readings":
                        output = get_sensor_readings(args.get("sensor_type", "all"))
                    elif tool_name == "control_room_light":
                        output = control_room_light(args.get("room", ""), args.get("state", ""))
                    elif tool_name == "control_all_lights":
                        output = control_all_lights(args.get("state", ""))
                    elif tool_name == "get_light_status":
                        _, lights, _ = get_state_snapshot()
                        output = format_light_report(lights)
                    elif tool_name == "send_smart_home_report":
                        output = send_smart_home_report()
                    else:
                        output = f"Unsupported tool: {tool_name}"

                    await websocket.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": output}],
                            "isError": False
                        }
                    }))

            except Exception as exc:
                print(f"[MCP] Error handling message: {exc}")


async def main():
    connect_serial()
    start_serial_reader()

    if ser is None:
        print("[Warning] Running without ESP32 serial connection. Hardware tools will not work.")

    try:
        await listen_to_xiaozhi_mcp()
    finally:
        stop_reader.set()
        if ser is not None and ser.is_open:
            ser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Xiaozhi] Hub stopped.")
    except Exception as exc:
        print(f"[Fatal] {exc}")
