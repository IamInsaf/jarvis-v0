SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_folder",
        "description": "Create a new folder with a given name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "folder name, with its parent folder in front if it goes inside one, e.g. 'NewFolder' or 'projects/NewFolder'"}
            },
            "required": ["name"]
        }
    }
}

TEST_ARGS = {"name": "NewFolder"}


def run(args: dict, ctx) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "What should I call the folder?"
    try:
        path = ctx.files.make_folder(name)
    except Exception as e:
        return f"I couldn't create that folder: {e}"
    return f"Created folder {path}."