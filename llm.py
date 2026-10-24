"""The one Ollama client everything uses. It has a timeout because a stuck Ollama (seen when it swaps
between the main and coder models on a small GPU) otherwise freezes JARVIS forever, mid-sentence."""
import httpx
import ollama

import config

client = ollama.Client(timeout=config.OLLAMA_TIMEOUT)

# caps on generated tokens. Ollama has none by default, and a small model under a JSON constraint can
# emit whitespace forever — the request never ends and Ollama can't even unload the model
JSON_TOKENS = 200   # a router pick or a tool's arguments
REPLY_TOKENS = 400  # a spoken reply of a few sentences
CODE_TOKENS = 2500  # a whole plugin file

# what a dead, stuck or missing Ollama looks like to callers
ERRORS = (httpx.HTTPError, ollama.ResponseError, ConnectionError)
