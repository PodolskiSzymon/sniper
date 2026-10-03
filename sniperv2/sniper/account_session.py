"""Osobny program: utrzymuje sesję TWOJEGO konta Vinted zalogowaną 24/7 (krok do auto-zakupu).

Działa z domowego IP, NIGDY przez proxy IPRoyal - sesja konta i ciastka cf_clearance/datadome są związane
z Twoim IP i przeglądarką. To osobny proces niż Zwiadowca (ten skanuje przez proxy).

Jak to działa:
  1. Wczytuje ciastka z sniper/logs/my_headers.txt (cURL skopiowany z DevTools, jak w `python -m sniper.account`)
     do TRWAŁEGO profilu Chromium (logs/account_profile) - po pierwszym razie profil pamięta sesję sam.
  2. Trzyma otwartą przeglądarkę i co kilkanaście minut wchodzi na stronę. Własny JavaScript Vinted odświeża
     wtedy access_token (żyje ~1 h) refresh-tokenem (żyje ~7 dni) - dzięki temu sesja nie wygasa.
  3. Co pętlę sprawdza przez /api/v2/banners, czy wciąż jesteś zalogowany (czyta nazwę konta).
  4. `open_item(url)` otwiera ogłoszenie na Twoim zalogowanym koncie - fundament pod (przyszły) auto-zakup.
     NA RAZIE NIC NIE KUPUJE.

Uruchomienie:
    SNIPER_ACCOUNT_ENABLED=true  (w sniper/.env)
    python -m sniper.account_session

Gdy po ~7 dniach refresh_token wygaśnie: zaloguj się w przeglądarce, skopiuj świeży cURL do my_headers.txt
i uruchom ponownie.
"""
import asyncio
import json
import logging
from pathlib import Path

from .account import DEFAULT_HEADERS_FILE, detect_banners, read_headers
from .config import AccountConfig, ScoutConfig

log = logging.getLogger("sniper.account")

HOME_URL = "https://www.vinted.pl/"
# W przeglądarce robimy fetch względny - leci jako same-origin z ciastkami konta (tak jak robi to strona).
BANNERS_PATH = "/api/v2/banners"
COOKIE_DOMAIN = ".vinted.pl"


def cookie_header_to_playwright(cookie_header, domain=COOKIE_DOMAIN):
    """'a=1; b=2' -> [{'name','value','domain','path'}] dla context.add_cookies()."""
    cookies = []
    for part in (cookie_header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return cookies


def load_account_cookies(headers_file):
    """Ciastka konta z pliku my_headers.txt w formacie Playwrighta. Rzuca, gdy brak pliku / ciastek."""
    pasted = read_headers(headers_file)                 # {'cookie': ..., 'user-agent': ..., ...}
    cookies = cookie_header_to_playwright(pasted.get("cookie", ""))
    if not cookies:
        raise ValueError(f"Brak ciastek w {headers_file} - skopiuj zapytanie jako cURL (bash) z F12.")
    return cookies, pasted.get("user-agent")


# JS wykonywany w kontekście strony: pobiera /api/v2/banners i zwraca {status, body}.
_BANNERS_FETCH = """
async (path) => {
  try {
    const r = await fetch(path, {headers: {accept: 'application/json'}, credentials: 'include'});
    return {status: r.status, body: await r.text()};
  } catch (e) { return {status: 0, body: String(e)}; }
}
"""


class VintedAccount:
    """Trwała sesja przeglądarki zalogowanej na Twoje konto (bez proxy)."""

    def __init__(self, cfg: AccountConfig, log_dir):
        self.cfg = cfg
        log_dir = Path(log_dir)
        self.headers_file = Path(cfg.headers_file) if cfg.headers_file else log_dir / DEFAULT_HEADERS_FILE
        self.profile_dir = Path(cfg.profile_dir) if cfg.profile_dir else log_dir / "account_profile"
        self._pw = None
        self.context = None
        self.page = None
        self.username = None

    def clear_profile_locks(self):
        """Usuwa pliki-blokady Chromium z profilu (zostają po niedokończonym zamknięciu).

        Bez tego nowe uruchomienie widzi blokadę, oddaje sterowanie 'istniejącej sesji' i pada
        (TargetClosedError / 'Otwieram w istniejącej sesji przeglądarki'). Kasujemy tylko blokady,
        NIE ciastka - sesja logowania zostaje.
        """
        removed = []
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            lock = self.profile_dir / name
            try:
                if lock.is_symlink() or lock.exists():
                    lock.unlink()
                    removed.append(name)
            except OSError:
                pass
        if removed:
            log.info("[KONTO] Usunąłem blokady profilu: %s", ", ".join(removed))

    async def start(self):
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.clear_profile_locks()
        self._pw = await async_playwright().start()
        # Trwały profil => sesja przeżywa restart programu. BEZ proxy (proxy=None) - domowe IP.
        launch_kwargs = dict(headless=self.cfg.headless, proxy=None, viewport=self.cfg.viewport_size)
        if self.cfg.chrome_path:
            launch_kwargs["executable_path"] = self.cfg.chrome_path
        try:
            self.context = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **launch_kwargs)
        except Exception as exc:
            raise RuntimeError(
                "Nie udało się otworzyć przeglądarki na profilu konta. Najczęściej profil jest JUŻ UŻYWANY "
                "przez inne okno/proces. Zamknij wszystkie okna tej przeglądarki i procesy 'python -m sniper...' "
                "(w Menedżerze zadań), potem spróbuj ponownie. Jeśli nie pomoże: 'python -m sniper.account_session "
                f"--reset' (wyczyści profil - trzeba będzie wkleić świeży cURL). Szczegół: {exc}"
            ) from exc
        self.context.set_default_navigation_timeout(self.cfg.nav_timeout * 1000)
        _, user_agent = await self._seed_cookies()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        vp = self.cfg.viewport_size
        log.info("[KONTO] Przeglądarka uruchomiona (profil: %s, headless=%s, okno %dx%d px, bez proxy).",
                 self.profile_dir, self.cfg.headless, vp["width"], vp["height"])
        await self.refresh_and_check()
        return self

    async def _seed_cookies(self):
        """Wstrzykuje ciastka z my_headers.txt, jeśli plik istnieje (przy pierwszym logowaniu / po wygaśnięciu)."""
        if not self.headers_file.exists():
            log.info("[KONTO] Brak %s - polegam na zapisanym profilu przeglądarki.", self.headers_file)
            return [], None
        cookies, user_agent = load_account_cookies(self.headers_file)
        await self.context.add_cookies(cookies)
        log.info("[KONTO] Wczytałem %d ciastek z %s.", len(cookies), self.headers_file.name)
        return cookies, user_agent

    async def refresh_and_check(self):
        """Wchodzi na stronę (JS Vinted odświeża token) i sprawdza, czy jesteś zalogowany. Zwraca bool."""
        await self.page.goto(HOME_URL, wait_until="domcontentloaded")
        if await self._stuck_on_session_refresh():
            log.warning("[KONTO] Pętla 'session-refresh' - sesja w profilu jest nieważna. "
                        "Napraw: zatrzymaj program, wklej ŚWIEŻY cURL do %s i uruchom z --reset "
                        "(czyści stary profil). Patrz README.", self.headers_file.name)
            self.username = None
            return False
        result = await self.page.evaluate(_BANNERS_FETCH, BANNERS_PATH)
        status, body = result.get("status"), result.get("body") or ""
        if status == 401:
            log.warning("[KONTO] 401 - sesja wygasła. Zaloguj się w przeglądarce i wklej świeży cURL do %s.",
                        self.headers_file.name)
            self.username = None
            return False
        _, name = detect_banners(body)
        if name:
            self.username = name
            log.info("[KONTO] Zalogowany jako: %s", name)
            return True
        # Brak banera polecającego nie przesądza o wylogowaniu - sprawdzamy, czy strona nie jest anonimowa.
        logged = status == 200 and '"code":0' in body
        log.info("[KONTO] Sesja %s (banner bez nazwy, status %s).",
                 "aktywna" if logged else "niepewna", status)
        return logged

    @staticmethod
    def is_session_refresh(url):
        """True, gdy URL to strona odświeżania sesji Vinted (pętla = nieważna sesja)."""
        return "session-refresh" in (url or "")

    async def _stuck_on_session_refresh(self):
        """Wykrywa zapętlenie na /session-refresh: czeka chwilę, sprawdza, czy strona z niej wyszła."""
        if not self.is_session_refresh(self.page.url):
            return False
        try:
            # Daj stronie czas na dokończenie odświeżenia; jeśli to pętla, dalej będzie session-refresh.
            await self.page.wait_for_url(lambda u: not self.is_session_refresh(u), timeout=15000)
            return False
        except Exception:
            return self.is_session_refresh(self.page.url)

    async def open_item(self, url):
        """Otwiera ogłoszenie na zalogowanym koncie. NA RAZIE tylko nawigacja - nic nie kupuje."""
        log.info("[KONTO] Otwieram ofertę (bez zakupu): %s", url)
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._dismiss_consent()
        return self.page.url

    async def _dismiss_consent(self):
        """Zamyka baner zgody na ciastka (OneTrust/Didomi), jeśli jest - inaczej przeszkadza w kliknięciu."""
        for selector in ("#onetrust-accept-btn-handler", "#didomi-notice-agree-button",
                          'button:has-text("Akceptuj")', 'button:has-text("Zgadzam")'):
            try:
                button = self.page.locator(selector)
                if await button.count() and await button.first.is_visible():
                    await button.first.click(timeout=3000)
                    log.info("[KONTO] Zamknąłem baner ciastek (%s).", selector)
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                pass

    # ---- interfejs dla sniper.buyer.attempt_purchase (open / buy_now_and_get_checkout / focus) ----
    # Bot NIGDY nie płaci: dochodzi do ekranu płatności i woła Ciebie. Klik 'Zapłać' + captchę robisz Ty.
    async def open(self, url):
        return await self.open_item(url)

    async def focus(self):
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

    @staticmethod
    def _is_checkout_url(url):
        return "/api/v2/purchases/" in (url or "") and "/checkout" in (url or "")

    async def buy_now_and_get_checkout(self):
        """Klika 'Kup teraz', czeka na stronę płatności i zwraca JSON z /api/v2/purchases/{id}/checkout.

        Dużo logów na każdym kroku - gdyby coś nie zadziałało, log mówi gdzie. NIE klika 'Zapłać'.
        """
        import time as _t
        captured = []

        def on_response(response):
            if self._is_checkout_url(response.url):
                captured.append(response)

        # Nasłuch na CAŁYM kontekście - łapie odpowiedź także, gdy checkout otworzy się w nowej karcie.
        self.context.on("response", on_response)
        pages_before = set(self.context.pages)

        def reacted():
            return bool(captured) or "/checkout" in (self.page.url or "") \
                or any(p not in pages_before for p in self.context.pages)

        try:
            # Poczekaj, aż strona się ustabilizuje - React musi podpiąć obsługę 'Kup teraz' (hydracja SPA).
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Klik bywa "ślepy", gdy React jeszcze się ładuje - ponawiamy, aż przycisk naprawdę zareaguje.
            for attempt in range(1, 4):
                found = await self._click_buy_now()
                log.info("[KONTO] Klik 'Kup teraz' (próba %d, dopasowań: %d) - czekam na reakcję...", attempt, found)
                for _ in range(16):                 # ~8 s na reakcję po kliknięciu
                    if reacted():
                        break
                    await asyncio.sleep(0.5)
                if reacted():
                    break
                log.warning("[KONTO] Klik bez reakcji (strona mogła się jeszcze ładować) - ponawiam.")
                await asyncio.sleep(1.5)

            new_pages = [p for p in self.context.pages if p not in pages_before]
            target = new_pages[0] if new_pages else self.page
            if new_pages:
                log.info("[KONTO] Zakup otworzył się w NOWEJ karcie: %s", target.url)
                self.page = target

            try:
                await target.wait_for_url(lambda u: "/checkout" in (u or ""), timeout=self.cfg.nav_timeout * 1000)
                log.info("[KONTO] Jestem na ekranie płatności: %s", target.url)
            except Exception:
                await self._dump_failure(target)

            deadline = _t.monotonic() + 20
            while not captured and _t.monotonic() < deadline:
                await asyncio.sleep(0.5)
            if not captured:
                raise RuntimeError(f"nie złapałem odpowiedzi /checkout (URL strony: {target.url})")

            response = captured[-1]
            log.info("[KONTO] Mam dane checkout: HTTP %s", response.status)
            return await response.json()
        finally:
            self.context.remove_listener("response", on_response)

    async def _dump_failure(self, page):
        """Diagnostyka, gdy 'Kup teraz' nie przeszło do checkoutu: zrzut ekranu + treść modalu/toastu + liczba kart."""
        note = await self._page_notice()
        shot = Path(self.profile_dir).parent / "buy_debug.png"
        try:
            await page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None
        try:
            modal = await page.eval_on_selector_all(
                '[role="dialog"], [class*="odal"], [class*="rawer"], [class*="heet"]',
                "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)")
        except Exception:
            modal = []
        log.warning("[KONTO] 'Kup teraz' nie przeszło do checkoutu. URL: %s | kart otwartych: %d%s%s%s",
                    page.url, len(self.context.pages),
                    f" | toast: „{note}”" if note else "",
                    f" | modal: „{modal[0][:200]}”" if modal else "",
                    f" | zrzut ekranu: {shot}" if shot else "")

    # Selektory 'Kup teraz' w kolejności pewności (potwierdzone przez użytkownika: data-testid + klasy).
    BUY_NOW_SELECTORS = (
        ('[data-testid="item-buy-button"]', "css"),
        (".details-list--actions button.web_ui__Button__primary", "css"),
        ("kup teraz", "role"),
    )

    async def _page_notice(self):
        """Tekst widocznego powiadomienia/toastu Vinted (np. 'Nie możesz kupić własnego przedmiotu'). '' gdy brak."""
        try:
            notices = await self.page.eval_on_selector_all(
                '[role="alert"], [class*="otification"], [class*="oast"]',
                "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)")
            return " | ".join(dict.fromkeys(notices))[:300]
        except Exception:
            return ""

    async def _click_buy_now(self):
        """Klik 'Kup teraz'. Próbuje kolejnych selektorów; zwraca liczbę dopasowań dla logu."""
        import re as _re
        for selector, kind in self.BUY_NOW_SELECTORS:
            button = (self.page.get_by_role("button", name=_re.compile(selector, _re.I))
                      if kind == "role" else self.page.locator(selector))
            found = await button.count()
            if found:
                log.info("[KONTO] Przycisk 'Kup teraz' znaleziony selektorem: %s", selector)
                await button.first.click(timeout=self.cfg.nav_timeout * 1000)
                return found
        raise RuntimeError("nie znalazłem przycisku 'Kup teraz' na stronie oferty")

    async def run_forever(self):
        """Pętla podtrzymująca sesję: co keepalive_min minut wchodzi na stronę i sprawdza zalogowanie."""
        interval = max(self.cfg.keepalive_min, 1.0) * 60
        log.info("[KONTO] Podtrzymuję sesję co %.0f min. Ctrl+C kończy.", self.cfg.keepalive_min)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh_and_check()
            except Exception:
                log.exception("[KONTO] Błąd podczas podtrzymania sesji - próbuję dalej.")

    def reset_profile(self):
        """Usuwa trwały profil przeglądarki - czyści stare/martwe ciastka (po ponownym logowaniu w Edge)."""
        import shutil
        if self.profile_dir.exists():
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            log.info("[KONTO] Wyczyściłem profil %s - startuję od zera z ciastek z %s.",
                     self.profile_dir, self.headers_file.name)

    async def close(self):
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()


async def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Utrzymuje sesję konta Vinted 24/7 (bez proxy).")
    parser.add_argument("--reset", action="store_true",
                        help="wyczyść profil przeglądarki przed startem (napraw pętlę session-refresh)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = ScoutConfig()
    if not cfg.account.enabled:
        print("Sesja konta wyłączona. Ustaw SNIPER_ACCOUNT_ENABLED=true w sniper/.env, potem:")
        print("  python -m sniper.account_session")
        return 1
    account = VintedAccount(cfg.account, cfg.log_dir)
    if args.reset:
        account.reset_profile()
    try:
        await account.start()
        if not account.username:
            log.warning("[KONTO] Nie potwierdziłem nazwy konta - sprawdź my_headers.txt (świeży cURL).")
        await account.run_forever()
    except KeyboardInterrupt:
        log.info("[KONTO] Zatrzymano ręcznie.")
    finally:
        await account.close()
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
