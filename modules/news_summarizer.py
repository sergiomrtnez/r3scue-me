"""
modules/news_summarizer.py - Static Scraping & RSS News Digest Module.

Fetches headlines and articles from user-provided URLs or RSS feeds using
lightweight static extraction (feedparser + BeautifulSoup + requests).
Leverages AI to synthesize key trends into a distraction-free executive summary
dispatched via TelegramOutbound.
"""

from typing import Any, Dict, List
import requests
from bs4 import BeautifulSoup
import feedparser

from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.telegram_outbound import TelegramOutbound


class NewsSummarizer(BaseModule):
    """
    Automated lightweight news curator and synthesizer.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        ai_handler: AIHandler,
        telegram_outbound: TelegramOutbound
    ) -> None:
        super().__init__(config, ai_handler, telegram_outbound)
        self.module_cfg: Dict[str, Any] = self.config.get("modules", {}).get("news_summarizer", {})

    def _fetch_content(self, url: str) -> str:
        """
        Extract readable items from an RSS feed or standard static HTML page.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        }
        extracted_text = []

        try:
            # First check if the target URL is an RSS/Atom feed
            feed = feedparser.parse(url)
            if feed.entries:
                self.logger.info(f"Parsing RSS feed: {url} ({len(feed.entries)} entries found)")
                for entry in feed.entries[:5]:  # Top 5 articles
                    title = getattr(entry, "title", "Untitled")
                    summary = getattr(entry, "summary", "")
                    clean_summary = BeautifulSoup(summary, "html.parser").get_text(strip=True)
                    extracted_text.append(f"Title: {title}\nSummary: {clean_summary[:300]}")
                return "\n\n".join(extracted_text)

            # Fallback to static HTML scraping
            self.logger.info(f"Scraping static HTML page: {url}")
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Decompose heavy or non-informative tags
            for element in soup(["script", "style", "nav", "footer", "aside", "form"]):
                element.decompose()

            headlines = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:8]]
            paragraphs = [p.get_text(strip=True) for p in soup.find_all("p")[:6] if len(p.get_text(strip=True)) > 40]

            content_blocks = []
            if headlines:
                content_blocks.append("Headlines:\n" + "\n".join(f"- {h}" for h in headlines))
            if paragraphs:
                content_blocks.append("Context Paragraphs:\n" + "\n".join(paragraphs))

            return "\n\n".join(content_blocks)

        except Exception as e:
            self.logger.warning(f"Could not scrape {url}: {e}")
            return ""

    def execute(self) -> None:
        """
        Retrieve news sources, summarize key events via AI, and notify user via Telegram.
        """
        urls: List[str] = self.module_cfg.get("urls", [])
        if not urls:
            self.logger.warning("No news URLs configured in modules.news_summarizer.urls.")
            return

        aggregated_data = []
        for url in urls:
            content = self._fetch_content(url.strip())
            if content:
                aggregated_data.append(f"Source [{url}]:\n{content}")

        if not aggregated_data:
            self.logger.warning("No news content could be extracted from the specified sources.")
            return

        combined_news = "\n\n===\n\n".join(aggregated_data)[:4000]

        system_prompt = (
            "Eres un analista de información y actualidad objetivo, perspicaz y directo. "
            "A partir de los titulares y resúmenes recopilados de las fuentes, genera un resumen ejecutivo "
            "de alto valor para el usuario.\n"
            "Instrucciones:\n"
            "1. Agrupa las noticias más importantes en 3 a 5 puntos directos con emojis (en español).\n"
            "2. Explica qué ha pasado y su relevancia de forma clara y sin texto de relleno.\n"
            "3. Filtra publicidad, clickbait y noticias duplicadas.\n"
            "4. Responde ÚNICAMENTE con el mensaje final listo para enviar a Telegram."
        )

        user_prompt = f"Información recopilada de las fuentes:\n\n{combined_news}"

        self.logger.info("Synthesizing news with AI...")
        texto_ia = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.5,
            max_tokens=700
        )

        texto_ia = (texto_ia or "").strip()
        if not texto_ia:
            self.logger.warning("AI returned empty summary for news.")
            return

        self.logger.info("Delivering news briefing via Telegram...")
        self.telegram.send_message(texto_ia)
