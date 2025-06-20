# plugins/web_search.py
import os
import json
import asyncio
import logging
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from dotenv import load_dotenv
import streamlit as st
from duckduckgo_search import DDGS
import requests

from plugin_base import ToolPlugin
from helpers import load_image_from_url, send_waiting_message

load_dotenv()
assistant_avatar = load_image_from_url()

logger = logging.getLogger("web_search")
logger.setLevel(logging.INFO)

class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    usage = (
        "{\n"
        '  "function": "web_search",\n'
        '  "arguments": {"query": "<search query>"}\n'
        "}\n"
    )
    description = "Searches the web and returns summarized answers to user questions."
    waiting_prompt_template = (
        "Generate a brief message to {mention} telling them to wait a moment while I search the web for additional information. Only generate the message. Do not respond to this message."
    )
    platforms = ["discord", "webui"]

    @staticmethod
    def extract_json(text):
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        return match.group(1) if match else None

    def search_web(self, query, num_results=10):
        try:
            with DDGS() as ddgs:
                return ddgs.text(query, max_results=num_results)
        except Exception as e:
            logger.error(f"[search_web error] {e}")
            return []

    def format_search_results(self, results):
        formatted = ""
        for idx, result in enumerate(results, start=1):
            title = result.get("title", "No Title")
            link = result.get("href", "No Link")
            snippet = result.get("body", "")
            formatted += f"{idx}. {title} - {link}\n"
            if snippet:
                formatted += f"   {snippet}\n"
        return formatted

    @staticmethod
    def fetch_web_summary(webpage_url, model):
        headers = {'User-Agent': 'Mozilla/5.0 (TaterBot WebSearch)'}
        try:
            resp = requests.get(webpage_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.error(f"Request failed: {resp.status_code} - {webpage_url}")
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
                element.decompose()

            container = soup.find("article") or soup.find("main") or soup.body
            if not container:
                return None

            text = container.get_text(separator="\n")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            article_text = "\n".join(lines)

            if len(article_text.split()) > 3000:
                article_text = " ".join(article_text.split()[:3000])
            logger.info(f"[fetch_web_summary] Extracted {len(article_text)} characters from {webpage_url}")
            return article_text
        except Exception as e:
            logger.error(f"[fetch_web_summary error] {e}")
            return None

    @staticmethod
    def split_message(text, chunk_size=1500):
        parts = []
        while len(text) > chunk_size:
            split = text.rfind('\n', 0, chunk_size)
            if split == -1:
                split = text.rfind(' ', 0, chunk_size)
            if split == -1:
                split = chunk_size
            parts.append(text[:split])
            text = text[split:].strip()
        parts.append(text)
        return parts

    async def safe_send(self, channel, content):
        if len(content) <= 2000:
            await channel.send(content)
        else:
            for chunk in self.split_message(content, 1900):
                await channel.send(chunk)

    async def handle_discord(self, message, args, ollama_client, context_length, max_response_length):
        query = args.get("query")
        if not query:
            return "No search query provided."

        mention = message.author.mention
        await send_waiting_message(
            ollama_client,
            prompt_text=self.waiting_prompt_template.format(mention=mention),
            save_callback=lambda _: None,
            send_callback=lambda text: asyncio.create_task(self.safe_send(message.channel, text))
        )

        results = self.search_web(query)
        if not results:
            return await self.safe_send(message.channel, "I couldn't find any relevant search results.")

        formatted_results = self.format_search_results(results)
        user_question = message.content

        choice_prompt = (
            f"Your name is Tater Totterson, you are looking for more information on the topic '{query}', because the user asked: '{user_question}'.\n\n"
            f"Here are the top search results:\n\n{formatted_results}\n\n"
            "Pick the most relevant link. Respond ONLY with this JSON format:\n"
            "{\n"
            '  "function": "web_fetch",\n'
            '  "arguments": {\n'
            '    "link": "<chosen link>",\n'
            f'    "query": "{query}",\n'
            f'    "user_question": "{user_question}"\n'
            "  }\n"
            "}"
        )

        choice_response = await ollama_client.chat(
            model=ollama_client.model,
            messages=[{"role": "system", "content": choice_prompt}],
            stream=False,
            keep_alive=-1,
            options={"num_ctx": ollama_client.context_length}
        )

        choice_text = choice_response['message'].get('content', '').strip()
        try:
            choice_json = json.loads(choice_text)
        except:
            json_str = self.extract_json(choice_text)
            choice_json = json.loads(json_str) if json_str else None

        if not choice_json or choice_json.get("function") != "web_fetch":
            return await self.safe_send(message.channel, "Failed to parse a valid link from search results.")

        args_choice = choice_json["arguments"]
        link = args_choice.get("link")
        original_query = args_choice.get("query", query)

        if not link:
            return await self.safe_send(message.channel, "No link was selected for detailed info.")

        logger.info(f"[web_search] AI selected link: {link}")

        summary = await asyncio.to_thread(self.fetch_web_summary, link, ollama_client.model)
        if not summary:
            return await self.safe_send(message.channel, "Failed to extract text from the selected page.")

        info_prompt = (
            f"Your name is Tater Totterson, you are answering a question based on the following web page content.\n\n"
            f"Original Query: {original_query}\n"
            f"User Question: {user_question}\n\n"
            f"Web Content:\n{summary}\n\n"
            f"Please provide a concise answer:"
        )

        final_response = await ollama_client.chat(
            model=ollama_client.model,
            messages=[{"role": "system", "content": info_prompt}],
            stream=False,
            keep_alive=-1,
            options={"num_ctx": ollama_client.context_length}
        )

        final_answer = final_response['message'].get('content', '').strip()
        if not final_answer:
            final_answer = "The assistant couldn't generate a response based on the web content."

        await self.safe_send(message.channel, final_answer)
        return ""

    async def handle_webui(self, args, ollama_client, context_length):
        await send_waiting_message(
            ollama_client,
            prompt_text=self.waiting_prompt_template.format(mention="User"),
            save_callback=lambda _: None,
            send_callback=lambda text: st.chat_message("assistant", avatar=assistant_avatar).write(text)
        )

        query = args.get("query")
        if not query:
            return "No search query provided."

        results = self.search_web(query)
        if not results:
            return "No results found."

        formatted_results = self.format_search_results(results)
        user_question = args.get("user_question", "")

        choice_prompt = (
            f"Your name is Tater Totterson, you are researching the topic '{query}' because the user asked: '{user_question}'.\n\n"
            f"Here are search results:\n\n{formatted_results}\n\n"
            "Respond with:\n"
            "{\n"
            '  "function": "web_fetch",\n'
            '  "arguments": {\n'
            '    "link": "<chosen link>",\n'
            f'    "query": "{query}",\n'
            f'    "user_question": "{user_question}"\n'
            "  }\n"
            "}"
        )

        choice_response = await ollama_client.chat(
            model=ollama_client.model,
            messages=[{"role": "system", "content": choice_prompt}],
            stream=False,
            keep_alive=-1,
            options={"num_ctx": ollama_client.context_length}
        )

        choice_text = choice_response['message'].get('content', '').strip()
        try:
            choice_json = json.loads(choice_text)
        except:
            json_str = self.extract_json(choice_text)
            choice_json = json.loads(json_str) if json_str else None

        if not choice_json or choice_json.get("function") != "web_fetch":
            return "Failed to parse function response."

        link = choice_json["arguments"].get("link")
        if not link:
            return "No link was selected."

        logger.info(f"[webui] AI selected link: {link}")

        summary = await asyncio.to_thread(self.fetch_web_summary, link, ollama_client.model)
        if not summary:
            return "Failed to extract content from page."

        info_prompt = (
            f"Your name is Tater Totterson, Answer the user's question using this content.\n\n"
            f"Query: {query}\n"
            f"User Question: {user_question}\n\n"
            f"Content:\n{summary}\n\n"
            "Do not introduce yourself only answer:"
        )

        final_response = await ollama_client.chat(
            model=ollama_client.model,
            messages=[{"role": "system", "content": info_prompt}],
            stream=False,
            keep_alive=-1,
            options={"num_ctx": ollama_client.context_length}
        )

        return final_response['message'].get('content', '').strip()

plugin = WebSearchPlugin()