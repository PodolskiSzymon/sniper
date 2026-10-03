"""Zarządzanie sesją (Anty-Ban): httpx.AsyncClient za proxy + odświeżanie tokenów przez Playwright.

Ewolucja cookies_management.py / session_management.py: zamiast requests i
sync_playwright mamy w pełni asynchroniczny klient i async API Playwrighta.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .config import BASE_HEADERS, BASE_URL, BROWSER_USER_AGENT
from .proxy_relay import PROXY_AUTH_HELP, ProxyAuthRejected, ProxyRelay
from .traffic import TrafficMeter

log = logging.getLogger("sniper.session")

TOKEN_HEADERS = ("x-csrf-token", "x-anon-id")
AUTH_ERRORS = (401, 403)


class SessionExpired(Exception):
    """Vinted odrzucił żądanie (401/403) nawet po odświeżeniu sesji."""


class RateLimited(Exception):
    """Vinted zwrócił 429."""


def playwright_proxy(proxy_url):
    """Zamienia http://USER:PASS@host:port na słownik proxy Playwrighta."""
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


# Lekka przeglądarka: do ciastek i tokenów wystarczy HTML + skrypty Vinted i zabezpieczeń (Datadome,
# Cloudflare). Obrazki, wideo, fonty i skrypty reklamowo-analityczne to większość z ~9 MB na wizytę.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_HOST_PARTS = (
    "googletagmanager.", "google-analytics.", "analytics.google.", "doubleclick.", "googlesyndication.",
    "googleadservices.", "adservice.google.", "facebook.", "fbcdn.", "criteo.", "adnxs.", "taboola.",
    "outbrain.", "hotjar.", "scorecardresearch.", "amazon-adsystem.", "pubmatic.", "rubiconproject.",
    "openx.", "casalemedia.", "smartadserver.", "teads.", "quantserve.", "tiktok.", "snapchat.",
    "pinterest.", "clarity.ms", "bing.com", "yandex.", "adform.", "rtbhouse.", "onetag.", "sharethrough.",
)


def _should_block(request):
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        return True
    host = urlsplit(request.url).hostname or ""
    return any(part in host for part in BLOCKED_HOST_PARTS)


async def _browse_vinted(playwright_proxy_cfg, wait_ms, light=True):
    """Jedna wizyta headless Chromium na Vinted. Zwraca (ciastka, przechwycone_nagłówki)."""
    from playwright.async_api import async_playwright

    captured = {}
    api_seen = asyncio.Event()

    def on_request(request):
        if "/api/v2/" not in request.url and "api.vinted.pl" not in request.url:
            return
        headers = request.headers
        for name in TOKEN_HEADERS:
            if headers.get(name):
                captured[name] = headers[name]
        if "x-csrf-token" in captured:
            api_seen.set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=playwright_proxy_cfg)
        try:
            context = await browser.new_context(user_agent=BROWSER_USER_AGENT)  # jak cookies_management.py
            if light:
                async def route(r):
                    if _should_block(r.request):
                        await r.abort()
                    else:
                        await r.continue_()
                await context.route("**/*", route)
            page = await context.new_page()
            page.on("request", on_request)

            # Jak cookies_management.py: wejście + czekanie na "load" + 4 s na strzały API w tle.
            # Przez proxy pełne "load" (reklamy, trackery) potrafi trwać >30 s - wtedy idziemy dalej,
            # bo ciastka i tokeny są dostępne już po załadowaniu dokumentu.
            await page.goto(f"{BASE_URL}/catalog", wait_until="domcontentloaded", timeout=wait_ms * 4)
            try:
                await page.wait_for_load_state("load", timeout=wait_ms * 2)
            except Exception:
                log.info("[AUTH] Strona nie doszła do 'load' w %ds - kontynuuję z tym, co już jest.", wait_ms * 2 // 1000)
            await page.wait_for_timeout(4000)

            if not api_seen.is_set():
                try:
                    await asyncio.wait_for(api_seen.wait(), timeout=wait_ms / 1000)
                except asyncio.TimeoutError:
                    log.warning("[AUTH] Nie złapano żądania API z tokenem - próbuję odczytać go ze strony.")

            if "x-csrf-token" not in captured:
                token = await page.evaluate(
                    "() => document.querySelector('meta[name=\"csrf-token\"]')?.content || null"
                )
                if token:
                    captured["x-csrf-token"] = token

            cookies = await context.cookies()
        finally:
            await browser.close()
    return cookies, captured


async def _browse_via_proxy(proxy_url, wait_ms, meter=None, light=True):
    cfg = playwright_proxy(proxy_url)
    if "username" not in cfg and "password" not in cfg:
        return await _browse_vinted(cfg, wait_ms, light)
    # Chromium nie wysyła loginu/hasła do proxy przy HTTPS (ERR_PROXY_AUTH_UNSUPPORTED),
    # więc idziemy przez lokalny przekaźnik, który sam dokleja Proxy-Authorization.
    on_bytes = (lambda sent, received: meter.add("browser", sent, received, requests=0)) if meter else None
    async with ProxyRelay(proxy_url, on_bytes=on_bytes) as relay:
        try:
            return await _browse_vinted({"server": relay.server}, wait_ms, light)
        except Exception as exc:
            if relay.auth_rejected:
                raise ProxyAuthRejected(PROXY_AUTH_HELP) from exc
            raise


async def fetch_fresh_tokens(proxy_url=None, wait_ms=15000, meter=None, light=True):
    """Odpala headless Chromium, wchodzi na Vinted i przechwytuje ciastka + nagłówki.

    Zwraca (lista_ciastek_playwrighta, {"x-csrf-token": ..., "x-anon-id": ...}).
    Przeglądarka idzie wyłącznie przez proxy - nie ma ścieżki "bez proxy".
    """
    log.info("[AUTH] Playwright (headless) wchodzi na Vinted po świeże tokeny (proxy: %s)...",
             "TAK" if proxy_url else "NIE")
    if proxy_url:
        cookies, captured = await _browse_via_proxy(proxy_url, wait_ms, meter, light)
        if meter:
            meter.add("browser", requests=1)
    else:
        cookies, captured = await _browse_vinted(None, wait_ms, light)

    if "x-anon-id" not in captured:
        anon = next((c["value"] for c in cookies if c["name"] == "anon_id"), None)
        if anon:
            captured["x-anon-id"] = anon

    log.info(
        "[AUTH] Zdobyto %d ciastek, nagłówki: %s",
        len(cookies), ", ".join(sorted(captured)) or "brak",
    )
    log.info("[AUTH] Ciastka: %s", ", ".join(sorted(c["name"] for c in cookies)))
    return cookies, captured


class VintedSession:
    """Globalna sesja httpx. Przy 401/403 wstrzymuje wszystkie żądania i odświeża tokeny.

    Każde żądanie czeka na `_ready`. Odświeżanie czyści flagę, więc reszta
    współbieżnych zadań grzecznie stoi, aż Playwright skończy. Licznik
    `_generation` sprawia, że gdy kilka zadań dostanie 401 naraz, przeglądarka
    odpala się tylko raz.
    """

    def __init__(self, proxy_url=None, timeout=10.0, browser_wait_ms=15000,
                 refresh_attempts=6, refresh_retry_delay=5.0, refresh_timeout=90.0,
                 browser_light=True, state_file=None):
        self._proxy_url = proxy_url or None
        self._browser_light = browser_light
        self._state_file = Path(state_file) if state_file else None
        self._browser_wait_ms = browser_wait_ms
        self._refresh_attempts = max(1, int(refresh_attempts))
        self._refresh_retry_delay = max(0.0, float(refresh_retry_delay))
        self._refresh_timeout = float(refresh_timeout)
        self._ready = asyncio.Event()
        self._ready.set()
        self._lock = asyncio.Lock()
        self._generation = 0
        self.traffic = TrafficMeter()
        self.client = httpx.AsyncClient(
            proxy=self._proxy_url,
            headers=BASE_HEADERS,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            http2=False,
        )

    async def close(self):
        await self.client.aclose()

    async def refresh(self, seen_generation=None):
        """Odświeża ciastka i tokeny. Pomija, jeśli ktoś inny już to zrobił w międzyczasie."""
        async with self._lock:
            if seen_generation is not None and seen_generation != self._generation:
                return
            self._ready.clear()
            try:
                cookies, tokens = await self._fetch_with_retries()
                self._apply(cookies, tokens)
                self._save_state(cookies, tokens)
                log.info("[AUTH] Sesja httpx zaktualizowana (generacja %d).", self._generation)
            finally:
                self._ready.set()

    def _apply(self, cookies, tokens):
        self.client.cookies.clear()
        for c in cookies:
            self.client.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        for name in TOKEN_HEADERS:
            self.client.headers.pop(name, None)
        self.client.headers.update(tokens)
        self._generation += 1

    def _save_state(self, cookies, tokens):
        """Zapisuje ciastka i tokeny na dysk, żeby restart programu nie kosztował wizyty przeglądarki (~MB)."""
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            keep = ("name", "value", "domain", "path")
            data = {"saved_at": time.time(), "tokens": tokens,
                    "cookies": [{k: c.get(k) for k in keep} for c in cookies]}
            self._state_file.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.warning("[AUTH] Nie zapisałem sesji na dysk: %s", exc)

    def load_state(self, max_age_s):
        """Wczytuje zapisaną sesję, jeśli jest świeższa niż max_age_s. Zwraca True, gdy się udało."""
        if not self._state_file or max_age_s <= 0 or not self._state_file.exists():
            return False
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            age = time.time() - float(data["saved_at"])
            if age > max_age_s or not data.get("cookies"):
                log.info("[AUTH] Zapisana sesja ma %.0f min - za stara, odświeżam przeglądarką.", age / 60)
                return False
            self._apply(data["cookies"], data.get("tokens") or {})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("[AUTH] Nie wczytałem zapisanej sesji (%s) - odświeżam przeglądarką.", exc)
            return False
        log.info("[AUTH] Wczytano zapisaną sesję sprzed %.0f min (%d ciastek) - bez uruchamiania przeglądarki.",
                 age / 60, len(data["cookies"]))
        return True

    async def _fetch_with_retries(self):
        """Do SNIPER_REFRESH_ATTEMPTS prób, każda z limitem SNIPER_REFRESH_TIMEOUT sekund.

        Przy rotacyjnym proxy każde nowe połączenie idzie przez inne IP, więc szybka ponowna próba
        zwykle pomaga, gdy węzeł wyjściowy zerwie połączenie (WinError 10054) albo strona wisi.
        """
        attempts = self._refresh_attempts
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(fetch_fresh_tokens(self._proxy_url, self._browser_wait_ms,
                                                                 meter=self.traffic,
                                                                 light=self._browser_light),
                                              self._refresh_timeout)
            except ProxyAuthRejected as exc:
                # Kolejne próby nic nie dadzą - złe dane albo brak transferu na koncie proxy.
                raise SessionExpired(str(exc)) from exc
            except Exception as exc:
                last_error = exc
                reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
                if attempt < attempts:
                    log.warning("[AUTH] Próba %d/%d odświeżenia nieudana (%s) - ponawiam za %.0fs (nowe IP).",
                                attempt, attempts, reason, self._refresh_retry_delay)
                    await asyncio.sleep(self._refresh_retry_delay)
        reason = (str(last_error).splitlines() or [""])[0] or type(last_error).__name__
        raise SessionExpired(f"Odświeżenie sesji nie powiodło się po {attempts} próbach: {reason}") from last_error

    async def get_json(self, url, params=None, referer=None, extra_headers=None):
        """GET z automatycznym odświeżeniem sesji przy 401/403 (jedna ponowna próba)."""
        headers = dict(extra_headers or {})
        if referer:
            headers["referer"] = referer
        for attempt in range(2):
            await self._ready.wait()
            generation = self._generation
            response = await self.client.get(url, params=params, headers=headers or None)
            self.traffic.add_http("catalog" if response.url.host == "api.vinted.pl" else "details", response)

            if response.status_code in AUTH_ERRORS:
                if attempt == 0:
                    log.warning("[AUTH] %s dla %s - wstrzymuję HTTP i odświeżam sesję.", response.status_code, url)
                    await self.refresh(seen_generation=generation)
                    continue
                raise SessionExpired(f"{response.status_code} po odświeżeniu sesji: {url}")
            if response.status_code == 429:
                raise RateLimited(url)
            if response.is_error:
                log.warning("[HTTP] %s %s | server=%s | body: %s", response.status_code, response.url.path,
                            response.headers.get("server", "?"), response.text[:300].replace("\n", " "))
            response.raise_for_status()
            return response.json()
