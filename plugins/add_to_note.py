SCHEMA = {
    "type": "function",
    "function": {
        "name": "add_to_note",
        "description": "Add text to an existing note file.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "note file name, with its folder in front if it goes inside one, e.g. 'notes.txt' or 'projects/notes.txt'"},
                "text": {"type": "string", "description": "text to add to the note"}
            },
            "required": ["name", "text"]
        }
    }
}

TEST_ARGS = {"name": "example.txt", "text": "This is an addition."}


def run(args: dict, ctx) -> str:
    name = str(args.get("name", "")).strip()
    text = str(args.get("text", "")).strip()
    if not name:
        return "What should I call the note?"
    if "." not in name:
        name += ".txt"
    try:
        path = ctx.files.append(name, text + "\n")
    except Exception as e:
        return f"I couldn't add to that note: {e}"
    return f"Added to {path}."