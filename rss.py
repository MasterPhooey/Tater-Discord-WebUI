# rss.py
import asyncio
import time
import os
import feedparser
import logging
import redis
import discord
import requests
import html
import re
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import ollama
from plugin_registry import plugin_registry
from plugin_settings import get_plugin_enabled, get_plugin_settings

logger = logging.getLogger("discord.rss")
logger.setLevel(logging.DEBUG)

# Load settings from environment variables
load_dotenv()
redis_host = os.getenv('REDIS_HOST', '127.0.0.1')
redis_port = int(os.getenv('REDIS_PORT', 6379))
response_channel_id = int(os.getenv("RESPONSE_CHANNEL_ID", 0))
max_response_length = int(os.getenv("MAX_RESPONSE_LENGTH", 1500))
POLL_INTERVAL = int(os.getenv("RSS_POLL_INTERVAL", 60))  # seconds between polls

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "127.0.0.1").strip()
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434").strip()
OLLAMA_URL = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2").strip()
context_length = int(os.getenv("CONTEXT_LENGTH", 10000))

# Create a Redis client
redis_client = redis.Redis(host=redis_host, port=redis_port, db=0, decode_responses=True)

#############################
# Helper Functions
#############################

def fetch_web_summary(webpage_url, model=OLLAMA_MODEL):
    """
    Extract the main textual content from a webpage URL using requests and BeautifulSoup.
    Cleans the HTML by removing unwanted elements and returns the cleaned article text.
    """
    try:
        response = requests.get(webpage_url, timeout=10)
        if response.status_code != 200:
            return None
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        # Remove unwanted elements.
        for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
            element.decompose()
        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        article_text = "\n".join(lines)
        return article_text
    except Exception as e:
        logger.error(f"Error in fetch_web_summary: {e}")
        return None

def format_summary_for_discord(summary):
    """
    Optionally format the summary text for Discord.
    """
    return summary.replace("### ", "# ")

def split_message(message_content, chunk_size=1500):
    """
    Split a long message into chunks so it can be sent as multiple Discord messages.
    """
    message_parts = []
    while len(message_content) > chunk_size:
        split_point = message_content.rfind('\n', 0, chunk_size)
        if split_point == -1:
            split_point = message_content.rfind(' ', 0, chunk_size)
        if split_point == -1:
            split_point = chunk_size
        message_parts.append(message_content[:split_point])
        message_content = message_content[split_point:].strip()
    message_parts.append(message_content)
    return message_parts

#############################
# RSS Manager Class
#############################
class RSSManager:
    def __init__(self, bot: discord.Client, rss_channel_id: int, ollama_client):
        self.bot = bot
        self.rss_channel_id = rss_channel_id
        self.ollama_client = ollama_client
        self.redis = redis_client
        self.feeds_key = "rss:feeds"

    def add_feed(self, feed_url: str) -> bool:
        """Attempts to parse the feed and adds it. Sets its last processed timestamp to avoid reprocessing old entries."""
        parsed_feed = feedparser.parse(feed_url)
        if parsed_feed.bozo:
            logger.error(f"Failed to parse feed: {feed_url}")
            return False

        last_ts = 0.0
        if parsed_feed.entries:
            for entry in parsed_feed.entries:
                if 'published_parsed' in entry:
                    entry_ts = time.mktime(entry.published_parsed)
                    if entry_ts > last_ts:
                        last_ts = entry_ts
        else:
            last_ts = time.time()

        try:
            self.redis.hset(self.feeds_key, feed_url, last_ts)
            logger.info(f"Added feed: {feed_url} with last_ts: {last_ts}")
            return True
        except Exception as e:
            logger.error(f"Error adding feed {feed_url}: {e}")
            return False

    def remove_feed(self, feed_url: str) -> bool:
        """Removes a feed URL from the watched feeds."""
        try:
            removed = self.redis.hdel(self.feeds_key, feed_url)
            if removed:
                logger.info(f"Removed feed: {feed_url}")
                return True
            else:
                logger.info(f"Feed not found: {feed_url}")
                return False
        except Exception as e:
            logger.error(f"Error removing feed {feed_url}: {e}")
            return False

    def get_feeds(self) -> dict:
        """Returns a dictionary mapping feed URLs to their last seen published timestamp."""
        try:
            feeds = self.redis.hgetall(self.feeds_key)
            return feeds
        except Exception as e:
            logger.error(f"Error fetching feeds: {e}")
            return {}

    async def process_entry(self, feed_title: str, entry: dict):
        """
        For a given feed entry, scrape the article, send it to Ollama for summarization,
        format the summary, and then send an announcement to Discord.
        """
        entry_title = entry.get("title", "No Title")
        link = entry.get("link", "")
        logger.info(f"Processing entry: {entry_title} from {feed_title}")
        
        # First, scrape the full article text.
        loop = asyncio.get_running_loop()
        article_text = await loop.run_in_executor(None, fetch_web_summary, link, OLLAMA_MODEL)
        
        if not article_text:
            summary_text = "Could not retrieve a summary for this article."
        else:
            # Now, send the article text to Ollama for summarization.
            summarization_prompt = (
                "Please summarize the following article in a clear and engaging format for Discord. "
                "Include a title and use bullet points for the main takeaways:\n\n"
                f"{article_text}\n\nSummary:"
            )
            try:
                summarization_response = await self.ollama_client.chat(
                    model=OLLAMA_MODEL,
                    messages=[{"role": "system", "content": summarization_prompt}],
                    stream=False,
                    keep_alive=-1,
                    options={"num_ctx": context_length}
                )
                summary_text = summarization_response['message'].get('content', '').strip()
                if not summary_text:
                    summary_text = "Failed to generate a summary from the article."
            except Exception as e:
                logger.error(f"Error summarizing article {link}: {e}")
                summary_text = f"Error summarizing article: {e}"

        # Format the summary for Discord.
        formatted_summary = format_summary_for_discord(summary_text)

        announcement = (
            f"📰 **New article from {feed_title}**\n"
            f"**{entry_title}**\n"
            f"{link}\n\n"
            f"{formatted_summary}"
        )

        # Split the announcement if it exceeds Discord's message length limits.
        chunks = split_message(announcement, chunk_size=max_response_length)

        try:
            channel = self.bot.get_channel(self.rss_channel_id)
            if channel:
                for chunk in chunks:
                    await channel.send(chunk)
            else:
                logger.warning("RSS Discord channel not found. Skipping Discord post.")
        except Exception as e:
            logger.error(f"Error sending announcement for article {link}: {e}")

        # Also send to Telegram if plugin enabled
        if get_plugin_enabled("telegram_notifier"):
            settings = get_plugin_settings("Telegram")
            bot_token = settings.get("telegram_bot_token")
            chat_id = settings.get("telegram_chat_id")
            if bot_token and chat_id:
                try:
                    import re  # Add this at the top of rss.py if not already imported

                    telegram_message = announcement

                    # Replace all pairs of **bold** with <b>bold</b>
                    telegram_message = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", telegram_message)

                    # Escape HTML-sensitive characters (preserve our <b> tags)
                    telegram_message = html.escape(telegram_message, quote=False)
                    telegram_message = telegram_message.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")

                    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": telegram_message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False
                    }
                    requests.post(telegram_url, json=payload, timeout=10)
                except Exception as e:
                    logger.warning(f"Failed to send Telegram message: {e}")

    async def poll_feeds(self):
        logger.info("Starting RSS feed polling...")
        while True:
            feeds = self.get_feeds()  # {feed_url: last_processed_timestamp (as string)}
            for feed_url, last_ts_str in feeds.items():
                try:
                    last_ts = float(last_ts_str) if last_ts_str else 0.0
                    # Use asyncio.to_thread to offload feed parsing to a separate thread.
                    parsed_feed = await asyncio.to_thread(feedparser.parse, feed_url)
                    if parsed_feed.bozo:
                        logger.error(f"Error parsing feed {feed_url}: {parsed_feed.bozo_exception}")
                        continue
                    feed_title = parsed_feed.feed.get("title", feed_url)
                    new_last_ts = last_ts
                    # Sort entries by published time (oldest first)
                    sorted_entries = sorted(
                        parsed_feed.entries,
                        key=lambda e: time.mktime(e.published_parsed) if 'published_parsed' in e else 0
                    )
                    for entry in sorted_entries:
                        if 'published_parsed' not in entry:
                            continue
                        entry_ts = time.mktime(entry.published_parsed)
                        if entry_ts > last_ts:
                            await self.process_entry(feed_title, entry)
                            if entry_ts > new_last_ts:
                                new_last_ts = entry_ts
                    # Update the stored timestamp if new articles were processed
                    if new_last_ts > last_ts:
                        self.redis.hset(self.feeds_key, feed_url, new_last_ts)
                except Exception as e:
                    logger.error(f"Error processing feed {feed_url}: {e}")
            await asyncio.sleep(POLL_INTERVAL)

def setup_rss_manager(bot: discord.Client, rss_channel_id: int, ollama_client) -> RSSManager:
    rss_manager = RSSManager(bot, rss_channel_id, ollama_client)
    asyncio.create_task(rss_manager.poll_feeds())
    return rss_manager