import os
import asyncio
import ollama
import requests
from PIL import Image
from io import BytesIO
import nest_asyncio
import redis

nest_asyncio.apply()

DEFAULT_KEEP_ALIVE = -1
DEFAULT_ASSISTANT_AVATAR_URL = "https://raw.githubusercontent.com/MasterPhooey/Tater-Discord-WebUI/refs/heads/main/images/tater.png"

def load_image_from_url(url: str = DEFAULT_ASSISTANT_AVATAR_URL) -> Image.Image:
    response = requests.get(url)
    response.raise_for_status()
    return Image.open(BytesIO(response.content))

# Global variable to store our main event loop.
_main_loop = None

def set_main_loop(loop):
    global _main_loop
    _main_loop = loop

def run_async(coro):
    # Use the main event loop set previously.
    if _main_loop is None:
        # Fallback if not set: use the default event loop.
        loop = asyncio.get_event_loop()
    else:
        loop = _main_loop
    return loop.run_until_complete(coro)

async def send_waiting_message(
    ollama_client,
    prompt_text,
    model=None,
    context_length=None,
    save_callback=None,
    send_callback=None
):
    if model is None:
        model = getattr(ollama_client, "model", os.getenv("OLLAMA_MODEL", "command-r:35B"))
    if context_length is None:
        context_length = getattr(ollama_client, "context_length", int(os.getenv("CONTEXT_LENGTH", 10000)))
    # Read keep_alive from the client; default to -1 if not set.
    keep_alive = getattr(ollama_client, "keep_alive", DEFAULT_KEEP_ALIVE)
    
    waiting_response = await ollama_client.chat(
        model=model,
        messages=[{"role": "system", "content": prompt_text}],
        stream=False,
        keep_alive=keep_alive,
        options={"num_ctx": context_length}
    )
    waiting_text = waiting_response["message"].get("content", "").strip()
    if not waiting_text:
        waiting_text = prompt_text
    if save_callback:
        save_callback(waiting_text)
    if send_callback:
        ret = send_callback(waiting_text)
        if asyncio.iscoroutine(ret):
            await ret
    return waiting_text

# Also include the OllamaClientWrapper definition here.
DEFAULT_ASSISTANT_AVATAR_URL = "https://raw.githubusercontent.com/MasterPhooey/Tater-Discord-WebUI/refs/heads/main/images/tater.png"

class OllamaClientWrapper(ollama.AsyncClient):
    def __init__(self, host, model=None, context_length=None, keep_alive=-1, **kwargs):
        model = model or os.getenv("OLLAMA_MODEL", "command-r:35B")
        context_length = context_length or int(os.getenv("CONTEXT_LENGTH", 10000))
        super().__init__(host=host, **kwargs)
        self.host = host
        self.model = model
        self.context_length = context_length
        self.keep_alive = keep_alive

redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', '127.0.0.1'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    db=0,
    decode_responses=True
)
