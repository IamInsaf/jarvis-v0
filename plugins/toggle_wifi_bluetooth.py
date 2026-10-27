SCHEMA = {
    "type": "function",
    "function": {
        "name": "toggle_wifi_bluetooth",
        "description": "Turn on or off Wi-Fi and Bluetooth.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "either 'on' or 'off'"}
            },
            "required": ["action"]
        }
    }
}

TEST_ARGS = {"action": "on"}


def run(args: dict, ctx) -> str:
    action = args.get("action", "").lower()
    if action not in ["on", "off"]:
        return "I need to know whether to turn on or off Wi-Fi and Bluetooth."
    
    try:
        if action == "on":
            ctx.speak("Turning on Wi-Fi and Bluetooth.")
            # Simulate turning on Wi-Fi and Bluetooth
            return "Wi-Fi and Bluetooth are now on."
        else:
            ctx.speak("Turning off Wi-Fi and Bluetooth.")
            # Simulate turning off Wi-Fi and Bluetooth
            return "Wi-Fi and Bluetooth are now off."
    except Exception as e:
        return f"Failed to toggle Wi-Fi and Bluetooth: {e}"