# plugins/comfyui_image_video_plugin.py
import os
import json
import uuid
import requests
import asyncio
import websocket
from io import BytesIO
from plugin_base import ToolPlugin
import discord
import base64
from helpers import redis_client, send_waiting_message, load_image_from_url

client_id = str(uuid.uuid4())

class ComfyUIImageVideoPlugin(ToolPlugin):
    name = "comfyui_image_video"
    usage = (
        "{\n"
        '  "function": "comfyui_image_video",\n'
        '  "arguments": {\n'
        '     "prompt": "<Text prompt for the animation>",\n'
        '     "filename": "<original filename.png>",\n'
        '     "image_bytes": "<base64-encoded-bytes>"\n'
        '  }\n'
        "}\n"
    )
    description = "Animate a user-provided image into a looping WebP."
    settings_category = "ComfyUI Animate Image"
    required_settings = {
        "COMFYUI_VIDEO_URL": {
            "label": "ComfyUI Server URL",
            "type": "text",
            "default": "http://localhost:8188",
            "description": "Base URL (host:port) for your ComfyUI instance."
        },
        "COMFYUI_VIDEO_WORKFLOW": {
            "label": "WanImageToVideo Workflow (JSON)",
            "type": "file",
            "default": "",
            "description": "Upload your ComfyUI JSON workflow template for WanImageToVideo."
        }
    }
    waiting_prompt_template = (
        "Generate a message telling the user to please wait while you bring your image to life! "
        "Only generate the message. Do not respond to this message."
    )
    platforms = ["discord"]
    assistant_avatar = load_image_from_url()

    @staticmethod
    def get_server_address():
        settings = redis_client.hgetall(
            f"plugin_settings:{ComfyUIImageVideoPlugin.settings_category}"
        )
        url = settings.get("COMFYUI_VIDEO_URL", "").strip() or "localhost:8188"
        return url.replace("http://", "").replace("https://", "")

    @staticmethod
    def upload_image(image_bytes: bytes, filename: str) -> str:
        server = ComfyUIImageVideoPlugin.get_server_address()
        data = {"overwrite": "true"}
        resp = requests.post(
            f"http://{server}/upload/image",
            files={"image": (filename, image_bytes)},
            data=data
        )
        resp.raise_for_status()
        result = resp.json()
        name = result.get("name") or result.get("filename")
        sub  = result.get("subfolder", "")
        return f"{sub}/{name}" if sub else name

    @staticmethod
    def get_workflow_template() -> dict:
        raw = redis_client.hget(
            f"plugin_settings:{ComfyUIImageVideoPlugin.settings_category}",
            "COMFYUI_VIDEO_WORKFLOW"
        ) or ""
        if not raw:
            raise RuntimeError(
                "No WanImageToVideo workflow template set in settings."
            )
        return json.loads(raw)

    @staticmethod
    def queue_workflow(workflow: dict) -> dict:
        server = ComfyUIImageVideoPlugin.get_server_address()
        payload = {"prompt": workflow, "client_id": client_id}
        resp = requests.post(
            f"http://{server}/prompt",
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def get_image(filename, subfolder, folder_type) -> bytes:
        server = ComfyUIImageVideoPlugin.get_server_address()
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url = f"http://{server}/view?{requests.compat.urlencode(params)}"
        return requests.get(url).content

    @staticmethod
    def collect_outputs(ws, workflow: dict) -> bytes:
        info = ComfyUIImageVideoPlugin.queue_workflow(workflow)
        pid = info["prompt_id"]
        while True:
            frame = ws.recv()
            if isinstance(frame, str):
                msg = json.loads(frame)
                if (
                    msg.get("type") == "executing" and
                    msg["data"].get("prompt_id") == pid and
                    msg["data"].get("node") is None
                ):
                    break
        server = ComfyUIImageVideoPlugin.get_server_address()
        hist = requests.get(f"http://{server}/history/{pid}").json()[pid]
        for node in hist["outputs"].values():
            if "images" in node:
                img = node["images"][0]
                return ComfyUIImageVideoPlugin.get_image(
                    img["filename"], img["subfolder"], img["type"]
                )
        raise RuntimeError("No output images returned by workflow.")

    @staticmethod
    def process_prompt(prompt: str, image_bytes: bytes, filename: str) -> bytes:
        uploaded = ComfyUIImageVideoPlugin.upload_image(image_bytes, filename)
        wf = ComfyUIImageVideoPlugin.get_workflow_template()
        for node in wf.values():
            if node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = uploaded
            if (
                node.get("class_type") == "CLIPTextEncode" and
                "Positive" in node.get("_meta", {}).get("title", "")
            ):
                node["inputs"]["text"] = prompt
        server = ComfyUIImageVideoPlugin.get_server_address()
        ws = websocket.WebSocket()
        ws.connect(f"ws://{server}/ws?clientId={client_id}")
        try:
            return ComfyUIImageVideoPlugin.collect_outputs(ws, wf)
        finally:
            ws.close()

    async def handle_discord(
        self, message, args, ollama_client, ctx_length, max_response_length
    ):
        image_bytes = None
        filename = None
        if message.attachments:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    image_bytes = await att.read()
                    filename = att.filename
                    break
        if not image_bytes and args.get("image_bytes"):
            try:
                image_bytes = base64.b64decode(args["image_bytes"])
                filename = args.get("filename", "input.png")
            except Exception:
                return "❌ Couldn’t decode `image_bytes`. Provide valid Base64."
        if not image_bytes:
            return (
                "❌ No image found. Attach an image or supply `image_bytes` + `filename`."
            )
        prompt = args.get("prompt", "").strip()
        if not prompt:
            return "❌ Please include a `prompt` describing the animation."
        await send_waiting_message(
            ollama_client=ollama_client,
            prompt_text=self.waiting_prompt_template,
            save_callback=lambda _: None,
            send_callback=lambda txt: message.channel.send(txt)
        )
        try:
            animated = await asyncio.to_thread(
                self.process_prompt, prompt, image_bytes, filename
            )
        except Exception as e:
            return f"❌ Failed to generate animation: {e}"
        await message.channel.send(
            file=discord.File(BytesIO(animated), filename="animated.webp")
        )
        return ""

    async def handle_webui(self, args, ollama_client, ctx_length):
        return "❌ This plugin only works in Discord."

plugin = ComfyUIImageVideoPlugin()