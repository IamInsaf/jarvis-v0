SCHEMA = {
    "type": "function",
    "function": {
        "name": "add_functionality",
        "description": "Add a new functionality to the assistant.",
        "parameters": {
            "type": "object",
            "properties": {
                "functionality": {"type": "string", "description": "the functionality to add"}
            },
            "required": ["functionality"]
        }
    }
}

TEST_ARGS = {"functionality": "weather updates"}


def run(args: dict, ctx) -> str:
    functionality = args.get("functionality", "")
    if not functionality:
        return "Please specify a functionality to add."
    ctx.memory.remember(f"Functionality added: {functionality}")
    return f"Functionality '{functionality}' added successfully."