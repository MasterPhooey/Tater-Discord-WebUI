# plugins/premiumize_download.py
import os
import aiohttp
import logging
import asyncio
from urllib.parse import quote
from plugin_base import ToolPlugin
from discord import ui, ButtonStyle
from io import BytesIO
import requests
import streamlit as st
from PIL import Image

# Import helper functions and shared redis client from helpers.py.
from helpers import load_image_from_url, send_waiting_message, redis_client

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Remove local load_dotenv; we'll rely on plugin settings from Redis.

class PremiumizeDownloadPlugin(ToolPlugin):
    name = "premiumize_download"
    usage = (
        "{\n"
        '  "function": "premiumize_download",\n'
        '  "arguments": {"url": "<URL to check>"}\n'
        "}\n"
    )
    description = "Checks if a file link provided by the user is cached on Premiumize.me."
    settings_category = "Premiumize"
    required_settings = {
        "PREMIUMIZE_API_KEY": {
            "label": "Premiumize API Key",
            "type": "password",
            "default": "",
            "description": "Your Premiumize.me API key."
        }
    }
    waiting_prompt_template = (
        "Generate a brief message to {mention} telling them to wait a moment while I check Premiumize for that URL and retrieve download links. Only generate the message. Do not respond to this message."
    )
    platforms = ["discord", "webui"]

    # Use the default assistant avatar loaded from helpers.
    assistant_avatar = load_image_from_url()  # Uses default URL from helpers.py

    @staticmethod
    async def get_premiumize_download_links(item: str, api_key: str):
        """
        Fetch download links for an item (URL or magnet link) from Premiumize.me.
        Returns a list of file dictionaries if successful; otherwise, returns None.
        """
        api_url = "https://www.premiumize.me/api/transfer/directdl"
        payload = {
            "apikey": api_key,
            "src": item
        }
        logger.debug(f"Fetching download links for item: {item} with payload: {payload}")
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, data=payload) as response:
                logger.debug(f"Download links response status: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"Download links response: {data}")
                    if data.get("status") == "success":
                        return data.get("content", [])
                    else:
                        logger.error(f"Download links error: {data.get('message')}")
                        return None
                else:
                    logger.error(f"Failed to connect to Premiumize.me: {response.status}")
                    return None

    @staticmethod
    def encode_filename(filename: str) -> str:
        return quote(filename)

    @classmethod
    async def process_download_web(cls, url: str, max_response_length=2000):
        """
        Process a Premiumize download request for the Web UI.
        Returns a text message with download links.
        """
        # Retrieve API key from plugin settings in Redis.
        key = "plugin_settings:Premiumize"
        settings = redis_client.hgetall(key)
        api_key = settings.get("PREMIUMIZE_API_KEY", "")
        if not api_key:
            return "Premiumize API key not configured."
        logger.debug(f"Processing web download for URL: {url}")
        download_links = await cls.get_premiumize_download_links(url, api_key)
        if download_links:
            links_message = f"**Download Links for `{url}`:**\n"
            for file in download_links:
                encoded_filename = cls.encode_filename(file['path'])
                encoded_link = file['link'].replace(file['path'], encoded_filename)
                new_line = f"- [{file['path']}]({encoded_link})\n"
                if len(links_message) + len(new_line) > max_response_length:
                    break
                links_message += new_line
            return links_message
        else:
            return f"The URL `{url}` is not cached on Premiumize.me."

    @classmethod
    async def process_download_discord(cls, channel, url: str, max_response_length=2000):
        """
        Process a Premiumize download request for Discord.
        Sends download links to the provided channel.
        """
        key = "plugin_settings:Premiumize"
        settings = redis_client.hgetall(key)
        api_key = settings.get("PREMIUMIZE_API_KEY", "")
        if not api_key:
            await channel.send("Premiumize API key not configured.")
            return
        logger.debug(f"Processing download for URL: {url}")
        download_links = await cls.get_premiumize_download_links(url, api_key)
        if download_links:
            if len(download_links) > 10:
                view = cls.PaginatedLinks(download_links, f"Download Links for `{url}`")
                await channel.send(content=view.get_page_content(), view=view)
            else:
                links_message = f"**Download Links for `{url}`:**\n"
                for file in download_links:
                    encoded_filename = cls.encode_filename(file['path'])
                    encoded_link = file['link'].replace(file['path'], encoded_filename)
                    new_line = f"- [{file['path']}]({encoded_link})\n"
                    if len(links_message) + len(new_line) > max_response_length:
                        break
                    links_message += new_line
                await channel.send(content=links_message)
        else:
            await channel.send(content=f"The URL `{url}` is not cached on Premiumize.me.")

    class PaginatedLinks(ui.View):
        def __init__(self, links, title, page_size=10):
            super().__init__()
            self.links = links
            self.title = title
            self.page_size = page_size
            self.current_page = 0
            self.update_buttons()

        def get_page_content(self):
            start = self.current_page * self.page_size
            end = start + self.page_size
            page_links = self.links[start:end]
            links_message = f"**{self.title} (Page {self.current_page + 1}):**\n"
            for file in page_links:
                encoded_filename = PremiumizeDownloadPlugin.encode_filename(file['path'])
                encoded_link = file['link'].replace(file['path'], encoded_filename)
                new_line = f"- [{file['path']}]({encoded_link})\n"
                if len(links_message) + len(new_line) > 2000:
                    break
                links_message += new_line
            return links_message

        def update_buttons(self):
            self.previous_button.disabled = self.current_page == 0
            self.next_button.disabled = (self.current_page + 1) * self.page_size >= len(self.links)

        @ui.button(label="Previous", style=ButtonStyle.grey)
        async def previous_button(self, interaction, button):
            if self.current_page > 0:
                self.current_page -= 1
                self.update_buttons()
                await interaction.response.edit_message(content=self.get_page_content(), view=self)

        @ui.button(label="Next", style=ButtonStyle.grey)
        async def next_button(self, interaction, button):
            if (self.current_page + 1) * self.page_size < len(self.links):
                self.current_page += 1
                self.update_buttons()
                await interaction.response.edit_message(content=self.get_page_content(), view=self)

    async def handle_discord(self, message, args, ollama_client, context_length, max_response_length):
        url = args.get("url")
        if url:
            waiting_prompt = self.waiting_prompt_template.format(mention=message.author.mention)
            await send_waiting_message(
                ollama_client=ollama_client,
                prompt_text=waiting_prompt,
                save_callback=lambda text: None,
                send_callback=lambda text: message.channel.send(text)
            )
            async with message.channel.typing():
                try:
                    await PremiumizeDownloadPlugin.process_download_discord(message.channel, url, max_response_length)
                    return ""
                except Exception as e:
                    prompt = f"Generate an error message to {message.author.mention} explaining that I was unable to retrieve the Premiumize download links for the URL. Only generate the message. Do not respond to this message."
                    error_msg = await self.generate_error_message(prompt, f"Failed to retrieve Premiumize download links: {e}", message)
                    return error_msg
        else:
            prompt = f"Generate an error message to {message.author.mention} explaining that no URL was provided for Premiumize download check. Only generate the message. Do not respond to this message."
            error_msg = await self.generate_error_message(prompt, "No URL provided for Premiumize download check.", message)
            return error_msg

    async def handle_webui(self, args, ollama_client, context_length):
        waiting_prompt = self.waiting_prompt_template.format(mention="User")
        await send_waiting_message(
            ollama_client=ollama_client,
            prompt_text=waiting_prompt,
            save_callback=lambda text: None,
            send_callback=lambda text: st.chat_message("assistant", avatar=assistant_avatar).write(text)
        )
        url = args.get("url")
        if not url:
            return "No URL provided for Premiumize download check."
        result = await PremiumizeDownloadPlugin.process_download_web(url)
        return result

    async def generate_error_message(self, prompt, fallback, message):
        return fallback

plugin = PremiumizeDownloadPlugin()