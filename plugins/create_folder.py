import re

SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_folder",
        "description": "Create a new folder with a given name.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder_name": {"type": "string", "description": "the name of the folder to create"}
            },
            "required": ["folder_name"]
        }
    }
}

TEST_ARGS = {"folder_name": "test_folder"}


def run(args: dict, ctx) -> str:
    try:
        folder_name = args.get("folder_name", "")
        if not folder_name:
            return "Please provide a name for the folder."
        if re.search(r'[^a-zA-Z0-9_]', folder_name):
            return "Folder name can only contain letters, numbers, and underscores."
        if ctx.memory.recall(f"folder_exists_{folder_name}"):
            return f"The folder '{folder_name}' already exists."
        ctx.memory.remember(f"folder_exists_{folder_name}")
        return f"Folder '{folder_name}' created successfully."
    except Exception as e:
        return f"Failed to create folder: {str(e)}."