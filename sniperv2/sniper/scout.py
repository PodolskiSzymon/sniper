"""Zwiadowca (Scout) - nieskończona, asynchroniczna pętla skanująca katalog Vinted.

Ewolucja main_vinted.py:
  * requests -> httpx.AsyncClient za rotacyjnym proxy,
  * PostgreSQL -> deque w RAM (RecentIds),
  * zero pobierania zdjęć na dysk - tylko URL-e,
  * weryfikacja item_closing_action (sold -> ignoruj) i koszt wysyłki,
  * alert e-mail w tle (aiosmtplib).
"""
import asyncio
import json
import logging
import random
import time
from pathlib import Path

import httpx

from .config import CATALOG_ONLY_HEADERS, CATALOG_URL, SHIPPING_URL, SIDEBAR_URL, ScoutConfig, get_catalog_params
from .dedup import RecentIds
from .extractor import build_offer, inactive_reason, item_url, unwrap_sidebar
from .notifier import EmailNotifier
from .proxy_relay import PROXY_AUTH_HELP
from .session import RateLimited, SessionExpired, VintedSession

log = logging.getLogger("sniper.scout")


def catalog_params(cfg):
    """Strona 1, najnowsze - parametry 1:1 z działającego zapytania do svc-catalogue (patrz config.get_catalog_params)."""
    return get_catalog_params(
        category=cfg.category,
        page=1,
        order='newest_first',
        search_text=cfg.search_text,
        price_from=cfg.price_from,
        price_to=cfg.price_to,
        per_page=cfg.per_page,
    )


def min_dedup(per_page):
    """Pamięć ID = co najmniej 5 stron katalogu (i nie mniej niż 100)."""
    return max(100, 5 * per_page)


def _catalog_price(item):
    price = item.get("price")
    if isinstance(price, dict):
        return f"{price.get('amount', '?')} {price.get('currency_code', '')}".strip()
    return str(price) if price is not None else "?"


def _to_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


class Scout:
    def __init__(self, cfg: ScoutConfig, session: VintedSession, notifier: EmailNotifier, evaluator=None):
        self.cfg = cfg
        self.session = session
        self.notifier = notifier
        # OfferEvaluator (sniper/evaluator.py) albo None = mail o każdej ofercie, jak przed modułem AI.
        self.evaluator = evaluator
        floor = min_dedup(cfg.per_page)
        if cfg.dedup_size < floor:
            log.warning("[SCOUT] SNIPER_DEDUP_SIZE=%d to za mało przy stronie %d ofert - używam %d.",
                        cfg.dedup_size, cfg.per_page, floor)
        self.seen = RecentIds(max(cfg.dedup_size, floor))
        # Kolejka "złapanych" ofert dla przyszłego modułu AI (słowniki z Offer.to_dict()).
        self.offers = asyncio.Queue(maxsize=200)
        self._detail_slots = asyncio.Semaphore(cfg.max_concurrent_details)
        self._tasks = set()
        self._first_batch = cfg.skip_initial_batch
        self._stats = {"polls": 0, "errors": 0, "new": 0, "caught": 0, "last_size": 0}
        self._skipped = {}        # powód -> liczba (od ostatniego heartbeatu)
        self._top_items = []      # pierwsze oferty z ostatniego skanu, w kolejności Vinted
        self._last_heartbeat = time.monotonic()

    # ------------------------------------------------------------------ katalog
    async def poll_catalog(self):
        data = await self.session.get_json(
            CATALOG_URL, params=catalog_params(self.cfg),
            referer="https://www.vinted.pl/", extra_headers=CATALOG_ONLY_HEADERS,  # jak w cURL z przeglądarki
        )
        items = data.get("items") or []
        self._stats["polls"] += 1
        self._stats["last_size"] = len(items)
        self._top_items = items[:5]
        if not items:
            log.warning("[SCOUT] Pusty katalog (klucze odpowiedzi: %s) - możliwy soft-ban. Odświeżam sesję.",
                        ", ".join(data) if isinstance(data, dict) else type(data).__name__)
            await self.session.refresh()
            return

        # Rosnąco po ID, żeby RecentIds wypychał najstarsze oferty jako pierwsze.
        fresh = [it for it in sorted(items, key=lambda it: it["id"]) if self.seen.add(it["id"])]

        if self._first_batch:
            self._first_batch = False
            log.info("[SCOUT] Rozgrzewka: zapamiętano %d ofert bez alertów.", len(fresh))
            return

        self._stats["new"] += len(fresh)
        for item in fresh:
            self._spawn(self.inspect(item))
        for it in fresh:
            log.info("[SCOUT] Nowe ogłoszenie %s | %s | %s | %s",
                     it["id"], it.get("title", "?"), _catalog_price(it), item_url(it["id"], it))

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ detale
    async def inspect(self, item):
        item_id = item["id"]
        referer = item_url(item_id, item)  # jak date_verification.get_sidebar_info: Referer = item['url']

        async with self._detail_slots:
            sidebar, shipping = await asyncio.gather(
                self.session.get_json(SIDEBAR_URL.format(item_id=item_id), referer=referer),
                self.session.get_json(SHIPPING_URL.format(item_id=item_id), referer=referer),
                return_exceptions=True,
            )

        if isinstance(sidebar, Exception):
            log.error("[SCOUT] Brak detali dla %s: %r", item_id, sidebar)
            self._skip("błąd detali")
            return
        if isinstance(shipping, Exception):
            log.warning("[SCOUT] Brak shipping_details dla %s: %r", item_id, shipping)
            shipping = None

        sidebar = unwrap_sidebar(sidebar)
        reason = inactive_reason(sidebar)
        if reason:
            log.info("[SCOUT] Pomijam %s - status: %s", item_id, reason)
            self._skip(f"status {reason}")
            return

        offer = build_offer(item_id, sidebar, shipping, catalog_item=item)
        # Druga linia obrony: gdyby API zignorowało filtr ceny, odsiewamy tutaj.
        low, high = _to_float(self.cfg.price_from), _to_float(self.cfg.price_to)
        if offer.price is not None and ((low is not None and offer.price < low) or
                                        (high is not None and offer.price > high)):
            log.info("[SCOUT] Pomijam %s - cena %.2f poza zakresem %s-%s", item_id, offer.price,
                     self.cfg.price_from or "0", self.cfg.price_to or "∞")
            self._skip("cena poza zakresem")
            return
        self.emit(offer)

    def _skip(self, reason):
        self._skipped[reason] = self._skipped.get(reason, 0) + 1

    def emit(self, offer):
        payload = offer.to_dict()
        log.info("[ZŁAPANO] %s | %s %s | %s", offer.title, offer.price, offer.currency, offer.url)
        log.debug(json.dumps(payload, ensure_ascii=False))

        self._stats["caught"] += 1
        self._save_offer(payload)
        if self.offers.full():
            self.offers.get_nowait()  # nikt jeszcze nie konsumuje - wyrzucamy najstarszą
        self.offers.put_nowait(payload)
        if self.evaluator:
            self.evaluator.submit(offer)   # ocena AI w tle; mail wysyła evaluator po ocenie
        else:
            self.notifier.notify(offer)

    def _save_traffic(self, row):
        """Wiersz do <log_dir>/traffic.csv - do porównania ustawień per_page / tempa skanów."""
        if not self.cfg.log_dir:
            return
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "per_page": self.cfg.per_page,
               "poll_interval": self.cfg.poll_interval, **row}
        try:
            path = Path(self.cfg.log_dir) / "traffic.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if new:
                    f.write(";".join(row) + "\n")
                f.write(";".join(str(v) for v in row.values()) + "\n")
        except OSError as exc:
            log.warning("[SCOUT] Nie zapisałem traffic.csv: %s", exc)

    def _save_offer(self, payload):
        """Dopisuje ofertę jako jedną linię JSON do <log_dir>/offers.jsonl (mikrosekundy, nie blokuje pętli)."""
        if not self.cfg.log_dir:
            return
        try:
            path = Path(self.cfg.log_dir) / "offers.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("[SCOUT] Nie zapisałem oferty do offers.jsonl: %s", exc)

    # ------------------------------------------------------------------ pętla
    async def run(self):
        log.info("=== ZWIADOWCA START | katalog=%s | cena %s-%s PLN | %d ofert/skan co %.0fs | proxy=%s ===",
                 self.cfg.catalog_id, self.cfg.price_from or "0", self.cfg.price_to or "∞",
                 self.cfg.per_page, self.cfg.poll_interval, "TAK" if self.cfg.proxy_url else "NIE")
        backoff = 0.0
        # Sesja z poprzedniego uruchomienia oszczędza wizytę przeglądarki; jeśli wygasła, pierwszy skan
        # dostanie 401/403 i get_json sam ją odświeży.
        needs_refresh = not self.session.load_state(self.cfg.session_max_age_min * 60)
        while True:
            started = time.monotonic()
            try:
                if needs_refresh:
                    await self.session.refresh()
                    needs_refresh = False
                await self.poll_catalog()
                backoff = 0.0
            except RateLimited:
                backoff = min(max(backoff * 2, 10.0), 120.0)
                log.warning("[SCOUT] 429 Too Many Requests - czekam %.0fs.", backoff)
            except SessionExpired as exc:
                backoff = self.cfg.refresh_backoff
                log.error("[SCOUT] %s - czekam %.0fs.", exc, backoff)
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
                # Błąd proxy/sieci/JSON - przy rotacyjnym proxy następne żądanie pójdzie z innego IP.
                backoff = min(max(backoff * 2, 2.0), 30.0)
                if isinstance(exc, httpx.ProxyError) and "407" in str(exc):
                    backoff = self.cfg.refresh_backoff
                    log.error("[SCOUT] %s Ponawiam za %.0fs.", PROXY_AUTH_HELP, backoff)
                else:
                    log.warning("[SCOUT] Błąd skanu: %r - ponawiam za %.0fs.", exc, backoff)
                self._stats["errors"] += 1
            except Exception:
                # Nieprzewidziany błąd nie może zatrzymać pętli - logujemy pełny traceback i jedziemy dalej.
                backoff = min(max(backoff * 2, 5.0), 60.0)
                log.exception("[SCOUT] Nieoczekiwany błąd - ponawiam za %.0fs.", backoff)
                self._stats["errors"] += 1

            self._heartbeat()
            if backoff:
                await asyncio.sleep(backoff)
            else:
                # Stałe tempo liczone od startu skanu: czas zapytania nie wydłuża odstępu.
                target = self.cfg.poll_interval + random.uniform(-self.cfg.poll_jitter, self.cfg.poll_jitter)
                await asyncio.sleep(max(0.0, target - (time.monotonic() - started)))

    def _heartbeat(self):
        """Co heartbeat_interval sekund podsumowanie - żeby cisza w logu nie wyglądała na zawieszenie."""
        interval = self.cfg.heartbeat_interval
        if not interval or time.monotonic() - self._last_heartbeat < interval:
            return
        s = self._stats
        skipped = ", ".join(f"{k}: {v}" for k, v in self._skipped.items()) or "0"
        log.info("[SCOUT] Żyję (%.0fs): %d skanów, %d błędów | nowych %d, złapanych %d, pominiętych: %s | "
                 "maile: wysłane %d, błędy %d | katalog: %d ofert",
                 interval, s["polls"], s["errors"], s["new"], s["caught"], skipped,
                 self.notifier.sent, self.notifier.failed, s["last_size"])
        if self._top_items:
            log.info("[SCOUT] Pierwsze oferty w katalogu (kolejność Vinted):")
            for it in self._top_items:
                log.info("    %s | %s | %s | %s", it.get("id"), it.get("title", "?"), _catalog_price(it),
                         item_url(it.get("id"), it))
        if self.evaluator:
            log.info("[SCOUT] %s", self.evaluator.window_report())
        text, row = self.session.traffic.window_report()
        log.info("[SCOUT] %s", text)
        self._save_traffic(row)
        for key in ("polls", "errors", "new", "caught"):
            s[key] = 0
        self._skipped = {}
        self._last_heartbeat = time.monotonic()

    async def shutdown(self):
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
