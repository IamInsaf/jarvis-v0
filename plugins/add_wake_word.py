SCHEMA = {
    "type": "function",
    "function": {
        "name": "add_wake_word",
        "description": "Add a new wake word for JARVIS.",
        "parameters": {
            "type": "object",
            "properties": {
                "word": {"type": "string", "description": "the new wake word, e.g. 'jarvish'"}
            },
            "required": ["word"]
        }
    }
}

TEST_ARGS = {"word": "jarvish"}


def run(args: dict, ctx) -> str:
    word = str(args.get("word", "")).strip().lower()
    if not word:
        return "What should the new wake word be?"
    try:
        path = ctx.files.append("wake_words.txt", word + "\n")
    except Exception as e:
        return f"I couldn't add that wake word: {e}"
    return f"Added '{word}' as a new wake word in {path}."