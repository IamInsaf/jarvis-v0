SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_file",
        "description": "Create a new file with a given name and content.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "name of the file, e.g. 'note.txt'"},
                "text": {"type": "string", "description": "content to write to the file"}
            },
            "required": ["name", "text"]
        }
    }
}

TEST_ARGS = {"name": "note.txt", "text": "This is a test note."}


def run(args: dict, ctx) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "What should I call the file?"
    if "." not in name:
        name += ".txt"
    try:
        path = ctx.files.write(name, str(args.get("text", "")))
    except Exception as e:
        return f"I couldn't create that file: {e}"
    return f"Created {path}."