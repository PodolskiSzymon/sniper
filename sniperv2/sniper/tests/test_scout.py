"""Testy na prawdziwych odpowiedziach API z api.docx. Uruchom: python -m pytest sniper/tests"""
import asyncio
import json
from pathlib import Path

import httpx

from sniper.config import ScoutConfig, SmtpConfig
from sniper.dedup import RecentIds
from sniper.extractor import build_offer, inactive_reason
from sniper.notifier import EmailNotifier, build_message
from sniper.scout import Scout
from sniper.session import VintedSession, playwright_proxy

FIX = json.loads(Path(__file__).with_name("fixtures.json").read_text(encoding="utf-8"))
ACTIVE_ID = 9238023547
SOLD_ID = 9272936873


def test_dedup_deque():
    seen = RecentIds(maxlen=3)
    assert all(seen.add(i) for i in (1, 2, 3))
    assert not seen.add(2)
    assert seen.add(4)            # wypycha 1
    assert 1 not in seen
    assert seen.snapshot() == [2, 3, 4]


def test_late_published_low_id_is_detected(monkeypatch):
    """Ogłoszenie z niższym ID (np. szkic opublikowany później) musi zostać wykryte jako nowe."""
    calls = {"n": 0}
    page1 = list(range(1000, 1096))                 # 96 ofert
    page2 = [500] + page1[:-1]                      # pojawia się ID 500 < wszystkich widzianych

    def handler(request):
        if request.url.host == "api.vinted.pl":
            calls["n"] += 1
            ids = page1 if calls["n"] == 1 else page2
            return httpx.Response(200, json={"items": [{"id": i, "user": {}} for i in ids]})
        return httpx.Response(500)

    async def scenario():
        session = VintedSession()
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=session.client.headers)
        scout = Scout(ScoutConfig(category="3580", dedup_size=20), session, EmailNotifier(SmtpConfig()))
        inspected = []

        async def fake_inspect(item):
            inspected.append(item["id"])
        scout.inspect = fake_inspect
        await scout.poll_catalog()      # rozgrzewka
        await scout.poll_catalog()
        await asyncio.gather(*scout._tasks)
        await session.close()
        return scout, inspected

    scout, inspected = asyncio.run(scenario())
    assert inspected == [500]
    assert scout.seen.maxlen >= 100     # SNIPER_DEDUP_SIZE=20 podniesione do minimum

def test_sold_and_active_status():
    assert inactive_reason(FIX["sidebar_sold"]) == "sold"
    assert inactive_reason(FIX["sidebar_active"]) is None


def test_build_offer_from_sidebar():
    catalog_item = {"id": ACTIVE_ID, "url": "https://www.vinted.pl/items/9238023547-samsung",
                    "user": {"id": 148344250, "login": "skestenyte.ska", "country_title": "Litwa"}}
    offer = build_offer(ACTIVE_ID, FIX["sidebar_active"], FIX["shipping_active"], catalog_item)
    d = offer.to_dict()
    assert d["title"] == "Samsung pro ultimate 512GB"
    assert d["price"] == 261.08 and d["currency"] == "PLN"
    assert d["description"] == "Naujas, nenaudotas."
    assert len(d["photo_urls"]) == 3 and all("/tc/" in u for u in d["photo_urls"])
    assert d["seller"]["name"] == "skestenyte.ska"
    assert d["seller"]["country"] == "Litwa"
    assert d["seller"]["feedback_count"] == 7 and d["seller"]["stars"] == 5.0
    assert d["shipping"]["price"] == 13.27 and not d["shipping"]["free_shipping"]
    assert d["total_price"] == 274.35
    assert d["url"].endswith("9238023547-samsung")
    msg = build_message(offer, "a@onet.pl", "b@onet.pl")
    assert "Samsung pro ultimate 512GB" in msg["Subject"] and "13.27 PLN" in msg["Subject"]
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    for body in (text, html):
        assert offer.url in body
        assert "Naujas, nenaudotas." in body                       # opis
        assert all(u.split("?")[0] in body for u in offer.photo_urls)  # linki do zdjęć
        assert "skestenyte.ska" in body and "https://www.vinted.pl/member/148344250" in body
    assert html.count("<img ") == 3


def test_playwright_proxy_parsing():
    p = playwright_proxy("http://USER:HASLO_country-pl@geo.iproyal.com:12321")
    assert p == {"server": "http://geo.iproyal.com:12321", "username": "USER", "password": "HASLO_country-pl"}


def test_scout_end_to_end(monkeypatch):
    """Katalog -> 401 -> odświeżenie -> detale; sprzedana pominięta, aktywna złapana."""
    calls = {"refresh": 0, "catalog": 0}

    async def fake_tokens(proxy_url=None, wait_ms=0, **kw):
        calls["refresh"] += 1
        return ([{"name": "anon_id", "value": "abc", "domain": ".vinted.pl", "path": "/"}],
                {"x-csrf-token": "tok", "x-anon-id": "abc"})

    monkeypatch.setattr("sniper.session.fetch_fresh_tokens", fake_tokens)

    def handler(request):
        if request.headers.get("x-csrf-token") != "tok" or "anon_id=abc" not in request.headers.get("cookie", ""):
            return httpx.Response(401)
        path = request.url.path
        if request.url.host == "api.vinted.pl" and path == "/svc-catalogue/items":
            calls["catalog"] += 1
            items = [{"id": 1, "user": {}}] if calls["catalog"] == 1 else [
                {"id": 1, "user": {}},
                {"id": SOLD_ID, "user": {"id": 170581459}},
                {"id": ACTIVE_ID, "url": "https://www.vinted.pl/items/9238023547",
                 "user": {"id": 148344250, "country_title": "Litwa", "country_iso_code": "LT"}},
            ]
            return httpx.Response(200, json={"items": items})
        if path == f"/api/v2/items/{ACTIVE_ID}/details/sidebar":
            return httpx.Response(200, json=FIX["sidebar_active"])
        if path == f"/api/v2/items/{SOLD_ID}/details/sidebar":
            return httpx.Response(200, json=FIX["sidebar_sold"])
        if path.endswith("/shipping_details"):
            return httpx.Response(200, json=FIX["shipping_active"])
        return httpx.Response(404)

    async def scenario():
        cfg = ScoutConfig(smtp=SmtpConfig(username="", password=""))
        session = VintedSession()
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=session.client.headers)
        scout = Scout(cfg, session, EmailNotifier(cfg.smtp))
        session.client.headers.pop("x-csrf-token", None)

        await scout.poll_catalog()                 # 401 -> refresh -> rozgrzewka (bez alertów)
        assert calls["refresh"] == 1
        await scout.poll_catalog()                 # dwie nowe oferty
        await asyncio.gather(*scout._tasks)
        await session.close()
        return scout

    scout = asyncio.run(scenario())
    assert scout.offers.qsize() == 1
    offer = scout.offers.get_nowait()
    assert offer["id"] == ACTIVE_ID
    assert offer["seller"]["country"] == "Litwa" and offer["seller"]["country_code"] == "LT"


def test_proxy_relay_injects_auth():
    """Przekaźnik dla Chromium dokleja Proxy-Authorization (fix ERR_PROXY_AUTH_UNSUPPORTED)."""
    import base64
    from sniper.proxy_relay import ProxyRelay

    expected = b"Proxy-Authorization: Basic " + base64.b64encode(b"USER:HASLO_country-pl")
    heads = []

    async def upstream(reader, writer):
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        writer.write(await reader.read(5))   # echo po zestawieniu tunelu
        await writer.drain()
        writer.close()

    async def scenario():
        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with ProxyRelay(f"http://USER:HASLO_country-pl@127.0.0.1:{port}") as relay:
            host, rport = relay.server.rsplit("/", 1)[1].split(":")
            reader, writer = await asyncio.open_connection(host, int(rport))
            writer.write(b"CONNECT www.vinted.pl:443 HTTP/1.1\r\nHost: www.vinted.pl:443\r\n\r\n")
            await writer.drain()
            status = await reader.readuntil(b"\r\n\r\n")
            writer.write(b"hello")
            await writer.drain()
            echoed = await reader.readexactly(5)
            writer.close()
        server.close()
        return status, echoed

    status, echoed = asyncio.run(scenario())
    assert status.startswith(b"HTTP/1.1 200")
    assert echoed == b"hello"
    assert heads[0].startswith(b"CONNECT www.vinted.pl:443") and expected in heads[0]


def test_iproyal_proxy_from_env(monkeypatch):
    """SNIPER_PROXY_HOST + SNIPER_PROXY_AUTH -> oficjalny słownik proxies IPRoyal."""
    import requests
    from sniper.config import build_proxy_url, requests_proxies

    monkeypatch.setenv("SNIPER_PROXY_HOST", "geo.iproyal.com:12321")
    monkeypatch.setenv("SNIPER_PROXY_AUTH", "LOGIN:HASLO_country-pl")
    monkeypatch.setenv("SNIPER_PROXY_URL", "http://ignored@example:1")
    proxy, proxy_auth = "geo.iproyal.com:12321", "LOGIN:HASLO_country-pl"
    official = {"http": f"http://{proxy_auth}@{proxy}", "https": f"http://{proxy_auth}@{proxy}"}

    assert build_proxy_url() == official["https"]
    assert requests_proxies() == official
    session = requests.Session()
    session.proxies.update(requests_proxies())
    assert session.proxies == official
    assert ScoutConfig().proxy_url == official["https"]

    # requests odczytuje login/hasło dokładnie takie, jak w .env
    from requests.utils import get_auth_from_url
    assert get_auth_from_url(session.proxies["https"]) == ("LOGIN", "HASLO_country-pl")

    # Znaki specjalne w haśle są bezpiecznie kodowane, a requests je odkodowuje.
    monkeypatch.setenv("SNIPER_PROXY_AUTH", "LOGIN:p@ss:word")
    assert get_auth_from_url(build_proxy_url()) == ("LOGIN", "p@ss:word")

    # Fallback na gotowy URL
    monkeypatch.delenv("SNIPER_PROXY_HOST")
    assert build_proxy_url() == "http://ignored@example:1"


def test_require_proxy_blocks_direct_traffic(monkeypatch):
    """Bez proxy w .env Zwiadowca nie może wyjść bezpośrednio."""
    import pytest
    from sniper.config import ProxyNotConfigured, requests_proxies

    for name in ("SNIPER_PROXY_HOST", "SNIPER_PROXY_AUTH", "SNIPER_PROXY_URL", "SNIPER_REQUIRE_PROXY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ProxyNotConfigured):
        requests_proxies()
    monkeypatch.setenv("SNIPER_REQUIRE_PROXY", "false")
    assert requests_proxies() == {}


def test_catalog_request_identical_to_session_management(monkeypatch):
    """Zapytanie do katalogu = 1:1 jak w session_management.py (parametry, kolejność, nagłówki)."""
    import pytest

    sm = pytest.importorskip("session_management")
    from sniper.config import BROWSER_USER_AGENT, CATALOG_HEADERS, get_catalog_params, make_main_loop_referer
    from sniper.scout import catalog_params

    for kwargs in (
        dict(category="karty_pamieci", page=1, order="newest_first"),
        dict(category="elektronika", page=3, order="relevance", search_text="ssd", price_from="10"),
    ):
        ours, theirs = get_catalog_params(**kwargs), sm.get_catalog_params(**kwargs)
        assert list(ours.items()) == list(theirs.items())
    from sniper.config import CATALOG_URL
    assert CATALOG_URL == sm.CATALOG_URL == "https://api.vinted.pl/svc-catalogue/items"
    assert make_main_loop_referer(1) == sm.make_main_loop_referer(1)
    assert make_main_loop_referer(2) == sm.make_main_loop_referer(2)

    # Zwiadowca woła to tak samo jak main_vinted.run_scraper_cycle
    cfg = ScoutConfig(category="karty_pamieci", search_text="", price_from="")
    legacy = sm.get_catalog_params(category="karty_pamieci", page=1, order="newest_first")
    legacy["per_page"] = cfg.per_page    # jedyna celowa różnica: mniejsza strona = mniej transferu
    assert list(catalog_params(cfg).items()) == list(legacy.items())
    # Kategoria spoza słownika (np. laptopy 3580) idzie wprost jako attribute_ids[catalog]
    assert get_catalog_params(category="3580")["attribute_ids[catalog]"] == "3580"

    # Nagłówki = make_boot_session() (bez dynamicznych tokenów i ciastek z dysku)
    monkeypatch.setattr(sm, "load_vinted_data_from_file", lambda: ({}, {}))
    boot = sm.make_boot_session()
    assert {k.lower(): v for k, v in CATALOG_HEADERS.items()} == {
        k.lower(): v for k, v in boot.headers.items()
        if k.lower() not in ("accept-encoding", "connection")  # domyślne nagłówki requests
    }

    # Request httpx z tymi parametrami ma ten sam query string co requests
    import requests
    params = sm.get_catalog_params(category="karty_pamieci")
    ours_url = httpx.Request("GET", sm.CATALOG_URL, params=params).url
    theirs_url = requests.Request("GET", sm.CATALOG_URL, params=params).prepare().url
    assert str(ours_url) == theirs_url

    # Playwright przedstawia się tak samo jak httpx (cf_clearance/datadome są wiązane z UA)
    assert BROWSER_USER_AGENT == CATALOG_HEADERS["user-agent"]


# Działające zapytanie z przeglądarki (cURL od użytkownika, 2026-10-02) - bez ciastek i tokenów.
CURL_URL = ("https://api.vinted.pl/svc-catalogue/items?page=2&per_page=96&search_text=&price_from=2000"
            "&currency=PLN&order=newest_first&attribute_ids%5Bcatalog%5D=3580&attribute_ids%5Bbrand%5D="
            "&attribute_ids%5Bbrand_collection%5D=&attribute_ids%5Bstatus%5D=")
CURL_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "pl,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
    "locale": "pl-PL",
    "origin": "https://www.vinted.pl",
    "platform": "web",
    "priority": "u=1, i",
    "referer": "https://www.vinted.pl/",
    "sec-ch-ua": '"Chromium";v="154", "Microsoft Edge";v="154", "Not A(Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
    "x-next-app": "marketplace-web",
}


def test_catalog_request_matches_browser_curl():
    """URL i nagłówki zapytania do katalogu = znak w znak jak w działającym cURL z przeglądarki."""
    import requests
    from sniper.config import CATALOG_HEADERS, CATALOG_ONLY_HEADERS, CATALOG_URL, BASE_HEADERS, get_catalog_params

    params = get_catalog_params(category="3580", page=2, order="newest_first", price_from="2000")
    assert str(httpx.Request("GET", CATALOG_URL, params=params).url) == CURL_URL
    assert requests.Request("GET", CATALOG_URL, params=params).prepare().url == CURL_URL
    assert CATALOG_HEADERS == CURL_HEADERS

    # Zwiadowca: nagłówki klienta + dodatki dla katalogu + Referer = komplet z cURL
    sent = {**BASE_HEADERS, **CATALOG_ONLY_HEADERS, "referer": "https://www.vinted.pl/"}
    assert sent == CURL_HEADERS


def test_scout_sends_curl_headers_to_catalog(monkeypatch):
    """Zwiadowca wysyła do api.vinted.pl dokładnie nagłówki z cURL, a do www.vinted.pl/api/v2 - same-origin."""
    seen = {}

    def handler(request):
        seen[request.url.host] = dict(request.headers)
        if request.url.host == "api.vinted.pl":
            return httpx.Response(200, json={"items": [{"id": 1, "user": {}}]})
        return httpx.Response(200, json={})

    async def scenario():
        session = VintedSession()
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=session.client.headers)
        scout = Scout(ScoutConfig(category="3580", search_text="", price_from=""), session, EmailNotifier(SmtpConfig()))
        await scout.poll_catalog()
        await session.get_json("https://www.vinted.pl/api/v2/items/1/shipping_details", referer="https://www.vinted.pl/items/1")
        await session.close()

    asyncio.run(scenario())
    api = seen["api.vinted.pl"]
    for name, value in CURL_HEADERS.items():
        assert api.get(name) == value, name
    www = seen["www.vinted.pl"]
    assert "origin" not in www and www["sec-fetch-site"] == "same-origin"



def test_empty_price_from_is_not_sent():
    """Puste price_from= daje 400 INVALID_REQUEST (sprawdzone na żywym API) - nie może trafić do URL."""
    from sniper.config import CATALOG_URL, get_catalog_params

    url = str(httpx.Request("GET", CATALOG_URL, params=get_catalog_params(category="3580")).url)
    assert "price_from" not in url
    assert url == ("https://api.vinted.pl/svc-catalogue/items?page=1&per_page=96&search_text=&currency=PLN"
                   "&order=newest_first&attribute_ids%5Bcatalog%5D=3580&attribute_ids%5Bbrand%5D="
                   "&attribute_ids%5Bbrand_collection%5D=&attribute_ids%5Bstatus%5D=")
    assert get_catalog_params(category="3580", price_from="2000")["price_from"] == "2000"


def test_price_to_and_catalog_from_env(monkeypatch):
    from sniper.config import CATALOG_URL, get_catalog_params

    url = str(httpx.Request("GET", CATALOG_URL, params=get_catalog_params(
        category="3580", price_from="100", price_to="3000")).url)
    assert "&price_from=100&price_to=3000&currency=PLN&" in url
    assert "price_to" not in str(httpx.Request("GET", CATALOG_URL, params=get_catalog_params(category="3580")).url)

    monkeypatch.setenv("SNIPER_CATALOG", "2994")
    monkeypatch.setenv("SNIPER_PRICE_FROM", "150")
    monkeypatch.setenv("SNIPER_PRICE_TO", "900")
    import importlib
    import sniper.config as config
    importlib.reload(config)
    try:
        cfg = config.ScoutConfig()
        assert (cfg.category, cfg.price_from, cfg.price_to) == ("2994", "150", "900")
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_scout_uses_per_page_from_config():
    seen = {}

    def handler(request):
        seen["per_page"] = request.url.params.get("per_page")
        return httpx.Response(200, json={"items": [{"id": 1, "user": {}}]})

    async def scenario():
        session = VintedSession()
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=session.client.headers)
        scout = Scout(ScoutConfig(category="3580", per_page=20, dedup_size=10), session, EmailNotifier(SmtpConfig()))
        await scout.poll_catalog()
        await session.close()
        return scout

    scout = asyncio.run(scenario())
    assert seen["per_page"] == "20"
    assert scout.seen.maxlen == 100      # 5 x 20, minimum 100


def test_refresh_retries_hung_and_reset_attempts(monkeypatch):
    """Wiszące wejście przeglądarki i zerwane połączenie -> kolejne próby (nowe IP), bez zawieszenia."""
    import sniper.session as sess

    attempts = {"n": 0}

    async def flaky_tokens(proxy_url=None, wait_ms=0, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            await asyncio.sleep(10)                      # strona wisi -> limit czasu
        if attempts["n"] == 2:
            raise ConnectionResetError(10054, "Istniejące połączenie zostało gwałtownie zamknięte")
        return ([{"name": "anon_id", "value": "abc", "domain": ".vinted.pl", "path": "/"}],
                {"x-csrf-token": "tok", "x-anon-id": "abc"})

    monkeypatch.setattr(sess, "fetch_fresh_tokens", flaky_tokens)

    async def scenario():
        session = sess.VintedSession(refresh_attempts=3, refresh_retry_delay=0, refresh_timeout=0.3)
        await session.refresh()
        token = session.client.headers.get("x-csrf-token")
        await session.close()
        return token

    assert asyncio.run(scenario()) == "tok"
    assert attempts["n"] == 3


def test_refresh_gives_up_after_attempts(monkeypatch):
    import pytest
    import sniper.session as sess

    async def always_fails(proxy_url=None, wait_ms=0, **kw):
        raise ConnectionResetError(10054, "reset")

    monkeypatch.setattr(sess, "fetch_fresh_tokens", always_fails)

    async def scenario():
        session = sess.VintedSession(refresh_attempts=3, refresh_retry_delay=0, refresh_timeout=0.3)
        try:
            await session.refresh()
        finally:
            await session.close()

    with pytest.raises(sess.SessionExpired, match="po 3 próbach"):
        asyncio.run(scenario())


def test_connection_reset_is_not_logged_as_error(caplog):
    import logging
    from sniper.__main__ import _quiet_connection_resets

    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.DEBUG):
            _quiet_connection_resets(loop, {"message": "Exception in callback", "exception": ConnectionResetError(10054)})
            _quiet_connection_resets(loop, {"message": "inny błąd", "exception": ValueError("x")})
    finally:
        loop.close()
    assert any(r.levelname == "DEBUG" and "zerwała" in r.getMessage() for r in caplog.records)
    assert any(r.levelname == "ERROR" and "inny błąd" in r.getMessage() for r in caplog.records)


def test_refresh_settings_from_env(monkeypatch):
    monkeypatch.setenv("SNIPER_REFRESH_ATTEMPTS", "10")
    monkeypatch.setenv("SNIPER_REFRESH_RETRY_DELAY", "2")
    monkeypatch.setenv("SNIPER_REFRESH_TIMEOUT", "120")
    monkeypatch.setenv("SNIPER_REFRESH_BACKOFF", "60")
    import importlib
    import sniper.config as config
    importlib.reload(config)
    try:
        cfg = config.ScoutConfig()
        assert (cfg.refresh_attempts, cfg.refresh_retry_delay, cfg.refresh_timeout, cfg.refresh_backoff) == (10, 2.0, 120.0, 60.0)
    finally:
        monkeypatch.undo()
        importlib.reload(config)
    assert config.ScoutConfig().refresh_attempts == 6


def test_traffic_meter_counts_compressed_body_and_cookie_headers():
    import gzip
    from sniper.traffic import TrafficMeter

    payload = json.dumps({"items": [{"id": i, "title": "Laptop " * 20} for i in range(20)]}).encode()
    packed = gzip.compress(payload)

    def handler(request):
        return httpx.Response(200, stream=httpx.ByteStream(packed),      # jak z sieci: strumień spakowany
                              headers={"content-encoding": "gzip", "content-type": "application/json"})

    async def scenario():
        session = VintedSession()
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=session.client.headers)
        session.client.cookies.set("datadome", "x" * 3000, domain=".vinted.pl")
        await session.get_json("https://api.vinted.pl/svc-catalogue/items", params={"per_page": 20})
        await session.get_json("https://www.vinted.pl/api/v2/items/1/shipping_details")
        await session.close()
        return session.traffic

    meter = asyncio.run(scenario())
    cat = meter.total["catalog"]
    assert cat["requests"] == 1
    assert len(packed) <= cat["received"] < len(packed) + 1000      # ciało spakowane + nagłówki, nie 400 KB JSON
    assert cat["sent"] > 3000                                       # ciastka w nagłówku żądania
    assert meter.total["details"]["requests"] == 1
    text, row = meter.window_report()
    assert "catalog" in text and row["catalog_requests"] == 1 and row["total_bytes"] > 0
    assert meter.window_report()[1]["window_bytes"] == 0           # okno wyzerowane


def test_relay_counts_browser_bytes():
    from sniper.proxy_relay import ProxyRelay

    counted = {"sent": 0, "received": 0}

    def on_bytes(sent, received):
        counted["sent"] += sent
        counted["received"] += received

    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n" + b"y" * 5000)
        await writer.drain()
        await reader.read(100)
        writer.close()

    async def scenario():
        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with ProxyRelay(f"http://U:P@127.0.0.1:{port}", on_bytes=on_bytes) as relay:
            host, rport = relay.server.rsplit("/", 1)[1].split(":")
            reader, writer = await asyncio.open_connection(host, int(rport))
            writer.write(b"CONNECT www.vinted.pl:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            await reader.readexactly(len(b"HTTP/1.1 200 Connection established\r\n\r\n") + 5000)
            writer.write(b"z" * 100)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
        server.close()

    asyncio.run(scenario())
    assert counted["received"] >= 5000
    assert counted["sent"] >= 100 + len(b"CONNECT www.vinted.pl:443 HTTP/1.1\r\n\r\n")


def test_relay_detects_407_from_proxy():
    """IPRoyal 407 (złe hasło / brak transferu) -> relay.auth_rejected, odpowiedź przekazana przeglądarce."""
    from sniper.proxy_relay import ProxyRelay

    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def scenario():
        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with ProxyRelay(f"http://U:ZLE@127.0.0.1:{port}") as relay:
            host, rport = relay.server.rsplit("/", 1)[1].split(":")
            reader, writer = await asyncio.open_connection(host, int(rport))
            writer.write(b"CONNECT www.vinted.pl:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            reply = await reader.readuntil(b"\r\n\r\n")
            writer.close()
            rejected = relay.auth_rejected
        server.close()
        return reply, rejected

    reply, rejected = asyncio.run(scenario())
    assert reply.startswith(b"HTTP/1.1 407") and rejected


def test_session_state_roundtrip(tmp_path):
    import sniper.session as sess

    path = tmp_path / "session.json"
    s1 = sess.VintedSession(state_file=path)
    s1._save_state([{"name": "datadome", "value": "abc", "domain": ".vinted.pl", "path": "/"}],
                   {"x-csrf-token": "tok", "x-anon-id": "anon"})

    async def scenario(max_age):
        s2 = sess.VintedSession(state_file=path)
        ok = s2.load_state(max_age)
        result = (ok, s2.client.headers.get("x-csrf-token"), s2.client.cookies.get("datadome", domain=".vinted.pl"))
        await s2.close()
        return result

    assert asyncio.run(scenario(3600)) == (True, "tok", "abc")
    assert asyncio.run(scenario(0))[0] is False            # 0 = zawsze nowa sesja
    data = json.loads(path.read_text())
    data["saved_at"] -= 7200
    path.write_text(json.dumps(data))
    assert asyncio.run(scenario(3600))[0] is False         # za stara
    asyncio.run(s1.close())


def test_light_browser_block_rules():
    from types import SimpleNamespace
    from sniper.session import _should_block

    req = lambda url, kind="script": SimpleNamespace(url=url, resource_type=kind)  # noqa: E731
    assert _should_block(req("https://images1.vinted.net/t/x.webp", "image"))
    assert _should_block(req("https://www.vinted.pl/font.woff2", "font"))
    assert _should_block(req("https://www.googletagmanager.com/gtm.js"))
    assert not _should_block(req("https://www.vinted.pl/catalog", "document"))
    assert not _should_block(req("https://www.vinted.pl/_next/static/app.js"))
    assert not _should_block(req("https://api.vinted.pl/svc-catalogue/items", "fetch"))
    assert not _should_block(req("https://js.datadome.co/tags.js"))
