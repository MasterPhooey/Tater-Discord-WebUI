# plugins/comfyui_video_plugin.py
import os
import json
import uuid
import urllib.request
import urllib.parse
import asyncio
import time
import websocket
from io import BytesIO
from plugin_base import ToolPlugin
import discord
import streamlit as st
from helpers import redis_client, send_waiting_message, load_image_from_url
import base64

client_id = str(uuid.uuid4())

class ComfyUIVideoPlugin(ToolPlugin):
    name = "comfyui_video_plugin"
    usage = (
        "{\n"
        '  "function": "comfyui_video_plugin",\n'
        '  "arguments": {"prompt": "<Text prompt for the video>"}\n'
        "}\n"
    )
    description = "Generates a video using ComfyUI."
    settings_category = "ComfyUI Video"
    required_settings = {
        "COMFYUI_VIDEO_URL": {
            "label": "ComfyUI Video URL",
            "type": "string",
            "default": "http://localhost:8188",
            "description": "The base URL for the ComfyUI Video API (do not include endpoint paths)."
        },
        "COMFYUI_VIDEO_WORKFLOW": {
            "label": "Workflow Template (JSON)",
            "type": "file",
            "default": "",
            "description": "Upload your JSON workflow template file for video generation. This field is required."
        }
    }
    waiting_prompt_template = "Generate a message telling the user to please wait while you assemble a film crew and direct your cinematic masterpiece!, Only generate the message. Do not respond to this message."
    platforms = ["discord", "webui"]
    assistant_avatar = load_image_from_url()

    @staticmethod
    def get_server_address():
        settings = redis_client.hgetall("plugin_settings:ComfyUI Video")
        url = settings.get("COMFYUI_VIDEO_URL", "").strip()
        if not url:
            return "localhost:8188"
        if url.startswith("http://"):
            return url[len("http://"):]
        elif url.startswith("https://"):
            return url[len("https://"):]
        else:
            return url

    @staticmethod
    def queue_prompt(prompt):
        server_address = ComfyUIVideoPlugin.get_server_address()
        p = {"prompt": prompt, "client_id": client_id}
        data = json.dumps(p).encode("utf-8")
        req = urllib.request.Request("http://{}/prompt".format(server_address),
                                     data=data,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    @staticmethod
    def get_image(filename, subfolder, folder_type):
        # Although this is a video plugin, ComfyUI outputs an animated WebP,
        # which we can retrieve using the same method as for images.
        server_address = ComfyUIVideoPlugin.get_server_address()
        data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url_values = urllib.parse.urlencode(data)
        with urllib.request.urlopen("http://{}/view?{}".format(server_address, url_values)) as response:
            return response.read()

    @staticmethod
    def get_history(prompt_id):
        server_address = ComfyUIVideoPlugin.get_server_address()
        with urllib.request.urlopen("http://{}/history/{}".format(server_address, prompt_id)) as response:
            return json.loads(response.read())

    @staticmethod
    def get_images(ws, prompt):
        prompt_id = ComfyUIVideoPlugin.queue_prompt(prompt)["prompt_id"]
        output_images = {}
        while True:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                if message["type"] == "executing":
                    data = message["data"]
                    if data["node"] is None and data["prompt_id"] == prompt_id:
                        break  # Execution is done
            else:
                continue  # skip binary data
        history = ComfyUIVideoPlugin.get_history(prompt_id)[prompt_id]
        for node_id in history["outputs"]:
            node_output = history["outputs"][node_id]
            images_output = []
            if "images" in node_output:
                for image in node_output["images"]:
                    image_data = ComfyUIVideoPlugin.get_image(image["filename"], image["subfolder"], image["type"])
                    images_output.append(image_data)
            output_images[node_id] = images_output
        return output_images

    @staticmethod
    def get_workflow_template():
        settings = redis_client.hgetall("plugin_settings:ComfyUI Video")
        workflow_str = settings.get("COMFYUI_VIDEO_WORKFLOW", "").strip()
        if not workflow_str:
            raise Exception("No workflow template set in COMFYUI_VIDEO_WORKFLOW. Please provide a valid JSON template.")
        return json.loads(workflow_str)

    @staticmethod
    def process_prompt(user_prompt: str) -> bytes:
        # Retrieve the workflow template from settings and update it with the user prompt
        workflow = ComfyUIVideoPlugin.get_workflow_template()
        workflow["6"]["inputs"]["text"] = user_prompt
        workflow["6"]["widgets_values"] = [user_prompt]
        ws = websocket.WebSocket()
        server_address = ComfyUIVideoPlugin.get_server_address()
        ws.connect("ws://{}/ws?clientId={}".format(server_address, client_id))
        images = ComfyUIVideoPlugin.get_images(ws, workflow)
        ws.close()
        # Return the first animated WebP found (i.e., the generated video)
        for node_id, imgs in images.items():
            if imgs:
                return imgs[0]
        raise Exception("No images returned from ComfyUI.")

    async def handle_discord(self, message, args, ollama_client, context_length, max_response_length):
        user_prompt = args.get("prompt")
        if not user_prompt:
            return "No prompt provided for ComfyUI Video."
        await send_waiting_message(
            ollama_client=ollama_client,
            prompt_text=self.waiting_prompt_template,
            save_callback=lambda x: None,
            send_callback=lambda x: message.channel.send(x)
        )
        try:
            video_bytes = await asyncio.to_thread(ComfyUIVideoPlugin.process_prompt, user_prompt)
            # Note the filename remains ".webp" so the animation is preserved.
            file = discord.File(BytesIO(video_bytes), filename="generated_video.webp")
            await message.channel.send(file=file)
        except Exception as e:
            await message.channel.send(f"Failed to queue prompt: {e}")
        return ""

    async def handle_webui(self, args, ollama_client, context_length):
        user_prompt = args.get("prompt")
        if not user_prompt:
            return "No prompt provided for ComfyUI Video."
        await send_waiting_message(
            ollama_client=ollama_client,
            prompt_text=self.waiting_prompt_template,
            save_callback=lambda x: None,
            send_callback=lambda x: st.chat_message("assistant", avatar=self.assistant_avatar).write(x)
        )
        try:
            video_bytes = await asyncio.to_thread(ComfyUIVideoPlugin.process_prompt, user_prompt)
            # Encode the animated WebP into a base64 string
            b64_video = base64.b64encode(video_bytes).decode("utf-8")
            # Build an HTML image element using a data URL. Make sure to label it as "image/webp".
            html_img = f'<img src="data:image/webp;base64,{b64_video}" alt="Generated Video" style="max-width:100%;">'
            st.markdown(html_img, unsafe_allow_html=True)
            return ""
        except Exception as e:
            return f"Failed to queue prompt: {e}"

plugin = ComfyUIVideoPlugin()