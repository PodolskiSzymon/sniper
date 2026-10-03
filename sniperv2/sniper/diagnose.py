"""Diagnostyka zapytania do katalogu: python -m sniper.diagnose [--headed]

Wysyła TO SAMO zapytanie (get_catalog_params) trzema drogami i wypisuje status + początek odpowiedzi:
  A) z wnętrza przeglądarki (fetch w kontekście strony Vinted),
  B) httpx - dokładnie jak Zwiadowca,
  C) requests - jak make_boot_session() ze starego projektu.
Dodatkowo pokazuje, jakie endpointy /api/ woła sama strona Vinted podczas ładowania katalogu.
Wartości ciastek, tokenów i hasło proxy NIE są wypisywane - tylko nazwy.
"""
import asyncio
import json
import sys

import httpx
import requests

from .config import (
    BROWSER_USER_AGENT, CATALOG_HEADERS, CATALOG_URL, ScoutConfig,
    get_catalog_params, require_proxy_url, requests_proxies,
)
from .proxy_relay import ProxyRelay


def _short(text, limit=300):
    return (text or "").replace("\n", " ")[:limit]


def _line(label, status, body):
    print(f"  {label:<10} status={status}  body: {_short(body)}")


async def browser_phase(proxy_url, params, headed):
    from playwright.async_api import async_playwright

    api_calls = []
    tokens = {}

    def on_response(response):
        if "/api/" in response.url:
            api_calls.append((response.request.method, response.status, response.url.split("?")[0]))

    def on_request(request):
        if "/api/v2/" in request.url or "api.vinted.pl" in request.url:
            for name in ("x-csrf-token", "x-anon-id"):
                if request.headers.get(name):
                    tokens[name] = request.headers[name]

    async with ProxyRelay(proxy_url) as relay, async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed, proxy={"server": relay.server})
        try:
            context = await browser.new_context(user_agent=BROWSER_USER_AGENT)
            page = await context.new_page()
            page.on("request", on_request)
            page.on("response", on_response)

            catalog_page = f"https://www.vinted.pl/catalog?catalog[]={params['attribute_ids[catalog]']}&order=newest_first"
            print(f"\n[1] Przeglądarka otwiera: {catalog_page}")
            try:
                await page.goto(catalog_page, timeout=60000)
                await page.wait_for_timeout(6000)
            except Exception as exc:
                print(f"  BŁĄD wejścia na stronę: {str(exc).splitlines()[0]}")

            url = str(httpx.Request("GET", CATALOG_URL, params=params).url)
            try:
                fetched = await page.evaluate(
                    """async ([url, headers]) => {
                        const r = await fetch(url, {headers, credentials: 'include'});
                        return [r.status, await r.text()];
                    }""",
                    [url, dict(tokens)],
                )
            except Exception as exc:
                fetched = ("ERR", str(exc).splitlines()[0])
            cookies = await context.cookies()
        finally:
            await browser.close()
    return api_calls, tokens, cookies, fetched


def httpx_phase(proxy_url, cookies, tokens, params):
    client = httpx.Client(proxy=proxy_url, headers=CATALOG_HEADERS, timeout=20, follow_redirects=True)
    for c in cookies:
        client.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    client.headers.update(tokens)
    try:
        r = client.get(CATALOG_URL, params=params)
        return r.status_code, r.text
    except Exception as exc:
        return "ERR", repr(exc)
    finally:
        client.close()


def requests_phase(cookies, tokens, params):
    session = requests.Session()
    session.headers.update(CATALOG_HEADERS)
    session.proxies.update(requests_proxies())
    session.headers.update(tokens)
    session.cookies.update({c["name"]: c["value"] for c in cookies})
    try:
        r = session.get(CATALOG_URL, params=params, timeout=20)
        return r.status_code, r.text
    except Exception as exc:
        return "ERR", repr(exc)


def main():
    headed = "--headed" in sys.argv
    cfg = ScoutConfig()
    proxy_url = require_proxy_url()
    params = get_catalog_params(category=cfg.category, page=1, order="newest_first",
                                search_text=cfg.search_text, price_from=cfg.price_from)
    print("=== DIAGNOSTYKA ZWIADOWCY ===")
    print(f"Kategoria: {cfg.category} | proxy: TAK | tryb przeglądarki: {'headed' if headed else 'headless'}")
    print("Parametry:", json.dumps({k: v for k, v in params.items()}, ensure_ascii=False))

    api_calls, tokens, cookies, fetched = asyncio.run(browser_phase(proxy_url, params, headed))

    print(f"\n[2] Endpointy /api/ wołane przez stronę Vinted ({len(api_calls)}):")
    for method, status, url in api_calls[:40]:
        print(f"  {method:<5} {status}  {url}")
    print(f"\n[3] Ciastka ({len(cookies)}): {', '.join(sorted(c['name'] for c in cookies))}")
    print(f"    Tokeny: {', '.join(sorted(tokens)) or 'brak'}")

    print("\n[4] To samo zapytanie do katalogu trzema drogami:")
    _line("browser", *fetched)
    _line("httpx", *httpx_phase(proxy_url, cookies, tokens, params))
    _line("requests", *requests_phase(cookies, tokens, params))
    print("\nWklej cały ten wynik do rozmowy z Claude.")


if __name__ == "__main__":
    main()
