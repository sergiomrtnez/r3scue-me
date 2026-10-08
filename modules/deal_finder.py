"""
modules/deal_finder.py - Lightweight Deal & Price Alert Monitor.

Scrapes target e-commerce or classified listing pages using strict lightweight
HTTP requests and BeautifulSoup (zero headless browser overhead).
Leverages AI to filter out false positives, evaluate real value discounts,
and push instant alerts via TelegramOutbound.
"""

from typing import Any, Dict, List
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

from core.base_module import BaseModule
from core.ai_handler import AIHandler
from core.telegram_outbound import TelegramOutbound


class DealFinder(BaseModule):
    """
    Automated deal hunter and false-positive filter using lightweight scraping.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        ai_handler: AIHandler,
        telegram_outbound: TelegramOutbound
    ) -> None:
        super().__init__(config, ai_handler, telegram_outbound)
        self.module_cfg: Dict[str, Any] = self.config.get("modules", {}).get("deal_finder", {})

    def _scrape_candidates(self, url: str, keywords: List[str]) -> List[Dict[str, str]]:
        """
        Perform zero-overhead HTML parsing to isolate matching product cards or links.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8"
        }
        matches = []

        try:
            self.logger.info(f"Scanning target URL: {url}")
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Look across anchor links and list/card elements
            elements = soup.find_all(["a", "div", "li", "article"])
            lowered_keywords = [kw.lower().strip() for kw in keywords if kw.strip()]

            seen_texts = set()

            for el in elements:
                text = el.get_text(" ", strip=True)
                if not text or len(text) < 15 or len(text) > 350:
                    continue

                lowered_text = text.lower()
                # Check keyword presence
                if any(kw in lowered_text for kw in lowered_keywords):
                    if text in seen_texts:
                        continue
                    seen_texts.add(text)

                    # Extract associated link if present
                    link = el.get("href") or (el.find("a") and el.find("a").get("href")) or url
                    full_link = urljoin(url, link)

                    matches.append({
                        "raw_snippet": text,
                        "link": full_link
                    })

                    if len(matches) >= 10:
                        break

            return matches

        except Exception as e:
            self.logger.warning(f"Error scraping deals from {url}: {e}")
            return []

    def execute(self) -> None:
        """
        Search for matching items, use AI to filter false positives, and dispatch alerts.
        """
        urls: List[str] = self.module_cfg.get("urls", [])
        keywords: List[str] = self.module_cfg.get("keywords", [])

        if not urls or not keywords:
            self.logger.warning("DealFinder requires both 'urls' and 'keywords' configured. Skipping.")
            return

        all_candidates: List[Dict[str, str]] = []
        for url in urls:
            candidates = self._scrape_candidates(url, keywords)
            all_candidates.extend(candidates)

        if not all_candidates:
            self.logger.info("No candidate items matching keywords were found.")
            return

        formatted_candidates = "\n".join(
            f"Candidate #{idx+1}:\nSnippet: {c['raw_snippet']}\nLink: {c['link']}"
            for idx, c in enumerate(all_candidates[:12])
        )

        system_prompt = (
            "Eres un analista experto en compras, tecnología y detección de chollos. "
            "Evalúa los fragmentos web encontrados para las palabras clave del usuario.\n"
            "Instrucciones:\n"
            "1. Filtra falsos positivos (accesorios irrelevantes, productos agotados o textos de navegación).\n"
            "2. Si encuentras ofertas reales o bajadas de precio legítimas, redacta una alerta clara y atractiva "
            "con título, precio estimado, motivo por el que vale la pena y enlace directo.\n"
            "3. Si no hay ofertas reales o todo es irrelevante, responde estrictamente: NO_DEALS_FOUND.\n"
            "4. Si hay chollos legítimos, responde ÚNICAMENTE con el mensaje final listo para Telegram (en español)."
        )

        user_prompt = (
            f"Palabras clave buscadas: {', '.join(keywords)}\n\n"
            f"Candidatos encontrados:\n{formatted_candidates}"
        )

        self.logger.info("Evaluating candidates with AI to filter false positives...")
        texto_ia = self.ai_handler.prompt(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=650
        )

        texto_ia = (texto_ia or "").strip()
        if not texto_ia or "NO_DEALS_FOUND" in texto_ia:
            self.logger.info("AI determined no legitimate deals among candidates. Suppressing alert.")
            return

        self.logger.info("Legitimate deals confirmed by AI. Dispatching Telegram alert...")
        self.telegram.send_message(texto_ia)
