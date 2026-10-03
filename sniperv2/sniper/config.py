"""Konfiguracja Zwiadowcy - wszystko z zmiennych środowiskowych (lub pliku sniper/.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv jest opcjonalny
    pass


def _env(name, default=""):
    return os.getenv(name, default).strip()


def _env_int(name, default):
    value = _env(name)
    return int(value) if value else default


def _env_float(name, default):
    value = _env(name)
    return float(value) if value else default


def _env_bool(name, default):
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "tak", "on")


def build_proxy_url():
    """Buduje URL proxy według oficjalnego wzorca IPRoyal: http://{proxy_auth}@{proxy}.

    Priorytet:
      1. SNIPER_PROXY_HOST (np. geo.iproyal.com:12321) + SNIPER_PROXY_AUTH (LOGIN:HASLO_country-pl)
      2. SNIPER_PROXY_URL  (gotowy http://LOGIN:HASLO_country-pl@geo.iproyal.com:12321)
    Zwraca "" gdy proxy nie jest skonfigurowane.
    """
    proxy = _env("SNIPER_PROXY_HOST")
    proxy_auth = _env("SNIPER_PROXY_AUTH")
    if proxy:
        proxy = proxy.split("://", 1)[-1]          # tolerujemy wpisanie z http://
        if not proxy_auth:
            return f"http://{proxy}"
        login, _, password = proxy_auth.partition(":")
        # quote() zmienia tylko znaki specjalne (@ : / itd.) - dla zwykłych loginów/haseł
        # wynik jest identyczny z f'http://{proxy_auth}@{proxy}'.
        return f"http://{quote(login, safe='')}:{quote(password, safe='')}@{proxy}"
    return _env("SNIPER_PROXY_URL")


class ProxyNotConfigured(RuntimeError):
    """Brak proxy w .env, a SNIPER_REQUIRE_PROXY=true (domyślnie) - nie wolno wyjść bezpośrednio."""


def require_proxy_url():
    """URL proxy albo wyjątek. Gwarantuje, że żaden ruch nie wyjdzie z pominięciem IPRoyal.

    Ustaw SNIPER_REQUIRE_PROXY=false tylko świadomie (np. testy lokalne) - wtedy brak proxy = ruch bezpośredni.
    """
    proxy_url = build_proxy_url()
    if not proxy_url and _env_bool("SNIPER_REQUIRE_PROXY", True):
        raise ProxyNotConfigured(
            "Brak proxy: ustaw SNIPER_PROXY_HOST + SNIPER_PROXY_AUTH (lub SNIPER_PROXY_URL) w sniper/.env"
        )
    return proxy_url


def requests_proxies(proxy_url=None):
    """Słownik proxies dla requests: {'http': ..., 'https': ...} (używa go sniper.diagnose)."""
    proxy_url = require_proxy_url() if proxy_url is None else proxy_url
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


BASE_URL = "https://www.vinted.pl"
# Nowy endpoint katalogu (Vinted przeniósł listę z www.vinted.pl/api/v2/catalog/items)
CATALOG_URL = "https://api.vinted.pl/svc-catalogue/items"
SIDEBAR_URL = BASE_URL + "/api/v2/items/{item_id}/details/sidebar"
SHIPPING_URL = BASE_URL + "/api/v2/items/{item_id}/shipping_details"

# ---------------------------------------------------------------------------
# 1:1 z działającego projektu (session_management.py / cookies_management.py).
# NIE zmieniać parametr po parametrze - to jest sprawdzony zestaw.
# ---------------------------------------------------------------------------

# UA identyczny z działającym zapytaniem (cURL z przeglądarki) - cf_clearance/datadome są wiązane z UA,
# więc Playwright (który zdobywa te ciastka) i httpx muszą się przedstawiać tak samo.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)

# Nagłówki 1:1 z działającego zapytania do api.vinted.pl/svc-catalogue/items (cURL z przeglądarki),
# bez ciastek i tokenów (x-csrf-token / x-anon-id dochodzą z Playwrighta).
CATALOG_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'pl,en;q=0.9,en-GB;q=0.8,en-US;q=0.7',
    'locale': 'pl-PL',
    'origin': 'https://www.vinted.pl',
    'platform': 'web',
    'priority': 'u=1, i',
    'referer': 'https://www.vinted.pl/',
    'sec-ch-ua': '"Chromium";v="154", "Microsoft Edge";v="154", "Not A(Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-site',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0',
    'x-next-app': 'marketplace-web',
}

# Zapytania do www.vinted.pl/api/v2/... (sidebar, shipping_details) to dla przeglądarki ta sama domena:
# bez 'origin', z 'sec-fetch-site: same-origin'.
BASE_HEADERS = {k: v for k, v in CATALOG_HEADERS.items() if k != 'origin'}
BASE_HEADERS['sec-fetch-site'] = 'same-origin'
CATALOG_ONLY_HEADERS = {'origin': CATALOG_HEADERS['origin'], 'sec-fetch-site': CATALOG_HEADERS['sec-fetch-site']}

# session_management.py -> categories
categories = {
    'karty_pamieci': 3063,
    'elektronika': 2994,
}
CATEGORIES = categories


def get_catalog_params(category, order='newest_first', page=1, search_text='', brand_ids='', brand_collection_ids='', status_ids='', price_from='', price_to='', per_page=96):
    """Kopia session_management.get_catalog_params - parametry 1:1 z działającego zapytania (cURL z przeglądarki).

    Jedyna różnica: kategoria spoza słownika (np. "3580") jest używana wprost jako attribute_ids[catalog],
    zamiast rzucać KeyError.
    """
    catalog_ids = categories.get(category, category)
    params = {
        'page': page,
        'per_page': per_page,
        'search_text': search_text,
        'price_from': price_from,
        'price_to': price_to,
        'currency': 'PLN',
        'order': order,
        'attribute_ids[catalog]': catalog_ids,
        'attribute_ids[brand]': brand_ids,
        'attribute_ids[brand_collection]': brand_collection_ids,
        'attribute_ids[status]': status_ids,
    }
    # Vinted odrzuca puste price_from= (400 INVALID_REQUEST) - ceny wysyłamy tylko z wartością,
    # tak jak przeglądarka. Pozostałe puste pola (search_text, attribute_ids[...]) są akceptowane.
    for key in ('price_from', 'price_to'):
        if params[key] in ('', None):
            del params[key]
    return params


def make_main_loop_referer(page=1):
    """Kopia session_management.make_main_loop_referer."""
    if page == 1:
        return "https://www.vinted.pl/catalog"
    else:
        return f"https://www.vinted.pl/catalog?page={page}"


@dataclass(frozen=True)
class SmtpConfig:
    host: str = _env("SNIPER_SMTP_HOST", "smtp.poczta.onet.pl")
    port: int = _env_int("SNIPER_SMTP_PORT", 465)
    username: str = _env("SNIPER_SMTP_USER")          # np. twoj_login@onet.pl
    password: str = _env("SNIPER_SMTP_PASSWORD")      # <-- TUTAJ hasło do Onetu (przez .env!)
    sender: str = _env("SNIPER_EMAIL_FROM") or _env("SNIPER_SMTP_USER")
    recipient: str = _env("SNIPER_EMAIL_TO") or _env("SNIPER_SMTP_USER")
    timeout: float = _env_float("SNIPER_SMTP_TIMEOUT", 20.0)

    @property
    def enabled(self):
        return bool(self.username and self.password and self.recipient)


def _env_list(name, default=""):
    """Lista z przecinkami: 'RTX, 4060 ,' -> ['rtx', '4060'] (małe litery, bez pustych)."""
    return tuple(part.strip().lower() for part in _env(name, default).split(",") if part.strip())


DEFAULT_AI_KEYWORDS = ("rtx,3050,3060,3070,3080,4050,4060,4070,4080,4090,"
                       "5050,5060,5070,5080,5090")


def _ai_provider():
    """SNIPER_AI_PROVIDER albo zgadnięty z tego, który klucz jest w .env (domyślnie gemini)."""
    provider = _env("SNIPER_AI_PROVIDER").lower()
    if provider:
        return provider
    if _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"):
        return "gemini"
    if _env("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "gemini"


def _ai_api_key(provider):
    specific = (_env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")) if provider == "gemini" else _env("ANTHROPIC_API_KEY")
    return _env("SNIPER_AI_API_KEY") or specific


AI_DEFAULT_MODELS = {"gemini": "gemini-3.8-flash", "anthropic": "claude-opus-5-5"}
_AI_PROVIDER = _ai_provider()


@dataclass(frozen=True)
class AiConfig:
    """Ocena ofert przez model AI (sniper/evaluator.py). Wywołania idą bezpośrednio, NIE przez proxy IPRoyal."""
    enabled: bool = _env_bool("SNIPER_AI_ENABLED", True)
    # gemini (Google AI Studio) albo anthropic (Claude).
    provider: str = _AI_PROVIDER
    # Klucz: SNIPER_AI_API_KEY albo standardowe GEMINI_API_KEY / GOOGLE_API_KEY / ANTHROPIC_API_KEY.
    api_key: str = _ai_api_key(_AI_PROVIDER)
    model: str = _env("SNIPER_AI_MODEL") or AI_DEFAULT_MODELS.get(_AI_PROVIDER, "")
    # Głębokość myślenia: low / medium / high (Gemini: thinking_level, Claude: effort). Puste = domyślna modelu.
    effort: str = _env("SNIPER_AI_EFFORT", "medium").lower()
    # Tylko Claude: zapasowy model po stronie API, gdy główny odmówi odpowiedzi (fallbacks: "default").
    fallback: bool = _env_bool("SNIPER_AI_FALLBACK", True)
    max_tokens: int = _env_int("SNIPER_AI_MAX_TOKENS", 8000)
    max_photos: int = _env_int("SNIPER_AI_MAX_PHOTOS", 6)
    # Tylko Gemini: url = Google pobiera zdjęcia sam; download = pobieramy je bezpośrednio (domowe IP, bez proxy)
    # i wysyłamy w zapytaniu.
    photos: str = _env("SNIPER_AI_PHOTOS", "url").lower()
    guidelines_file: str = _env("SNIPER_AI_GUIDELINES") or str(Path(__file__).with_name("guidelines.md"))

    # Mail tylko gdy ocena >= min_score; notify_all=true -> mail o każdej ofercie (z oceną albo bez).
    min_score: float = _env_float("SNIPER_AI_MIN_SCORE", 7.0)
    notify_all: bool = _env_bool("SNIPER_AI_NOTIFY_ALL", False)

    # Asynchronicznie: limit równoległych wywołań, limit czasu jednej próby, liczba ponowień.
    max_concurrent: int = _env_int("SNIPER_AI_MAX_CONCURRENT", 3)
    timeout: float = _env_float("SNIPER_AI_TIMEOUT", 120.0)
    retries: int = _env_int("SNIPER_AI_RETRIES", 2)
    retry_delay: float = _env_float("SNIPER_AI_RETRY_DELAY", 5.0)

    # Tani filtr przed AI (zero kosztów): cena łączna i słowa kluczowe w tytule/opisie.
    price_min: float | None = _env_float("SNIPER_AI_PRICE_MIN", None)
    price_max: float | None = _env_float("SNIPER_AI_PRICE_MAX", None)
    keywords: tuple = _env_list("SNIPER_AI_KEYWORDS", DEFAULT_AI_KEYWORDS)
    exclude_keywords: tuple = _env_list("SNIPER_AI_EXCLUDE_KEYWORDS")
    # Gdzie szukać słów kluczowych: "title" albo "title+description".
    keywords_in: str = _env("SNIPER_AI_KEYWORDS_IN", "title+description").lower()

    # Cena modelu w USD za 1M tokenów (wejście / wyjście) - tylko do szacunku kosztu w heartbeacie.
    # Puste = z tabeli MODEL_PRICES poniżej.
    price_in: float | None = _env_float("SNIPER_AI_PRICE_IN", None)
    price_out: float | None = _env_float("SNIPER_AI_PRICE_OUT", None)

    @property
    def active(self):
        return self.enabled and bool(self.api_key)


# USD za 1M tokenów (wejście, wyjście). Gemini 3.x Flash: cena promocyjna do 31.12.2026,
# od 2027 r. 1,50 / 7,50 - wtedy ustaw SNIPER_AI_PRICE_IN / SNIPER_AI_PRICE_OUT. Claude: cennik 2026-09.
MODEL_PRICES = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


@dataclass(frozen=True)
class AccountConfig:
    """Sesja TWOJEGO konta Vinted (auto-zakup) - osobny program sniper.account_session.

    Idzie z domowego IP, NIGDY przez proxy IPRoyal (sesja konta i ciastka anty-botowe są związane z Twoim IP).
    Logowanie: ciastka z pliku (SNIPER_ACCOUNT_HEADERS_FILE) wczytane do trwałego profilu Chromium;
    stronę odświeża jej własny JS, więc token podtrzymuje się sam (refresh_token żyje ~7 dni).
    """
    enabled: bool = _env_bool("SNIPER_ACCOUNT_ENABLED", False)
    headers_file: str = _env("SNIPER_ACCOUNT_HEADERS_FILE")   # domyślnie <log_dir>/my_headers.txt (ustalane niżej)
    profile_dir: str = _env("SNIPER_ACCOUNT_PROFILE_DIR")     # domyślnie <log_dir>/account_profile
    # Widoczne okno przeglądarki: przy pierwszym logowaniu / captchy wygodniej je widzieć (false).
    headless: bool = _env_bool("SNIPER_ACCOUNT_HEADLESS", False)
    # Co ile minut wejść na stronę, żeby podtrzymać sesję (JS Vinted odświeża wtedy token dostępu ~1 h).
    keepalive_min: float = _env_float("SNIPER_ACCOUNT_KEEPALIVE_MIN", 20.0)
    # Limit czasu jednej nawigacji (s).
    nav_timeout: float = _env_float("SNIPER_ACCOUNT_NAV_TIMEOUT", 45.0)
    # Ścieżka do chrome.exe - tylko gdy Playwright nie znajduje przeglądarki sam (puste = automatycznie).
    chrome_path: str = _env("SNIPER_ACCOUNT_CHROME_PATH")
    # Rozmiar okna strony (viewport) w px: "szerokość x wysokość". Okno systemowe jest nieco większe (pasek przeglądarki).
    viewport: str = _env("SNIPER_ACCOUNT_VIEWPORT", "1280x900")

    @property
    def viewport_size(self):
        try:
            w, h = self.viewport.lower().split("x", 1)
            return {"width": int(w), "height": int(h)}
        except (ValueError, AttributeError):
            return {"width": 1280, "height": 900}


@dataclass(frozen=True)
class BuyerConfig:
    """Auto-zakup okazji z konta (sniper.buyer). Z domowego IP przez sesję account_session (bez proxy).

    Bot NIGDY nie płaci: dochodzi do ekranu płatności, sprawdza limity i woła Ciebie - klik 'Zapłać'
    oraz captchę (suwak) robisz Ty. Tu są tylko twarde limity, które muszą przejść, zanim checkout powstanie.
    """
    enabled: bool = _env_bool("SNIPER_BUY_ENABLED", False)
    # Twarde limity bezpieczeństwa:
    max_total_pln: float = _env_float("SNIPER_BUY_MAX_TOTAL", 2500.0)   # maksymalna suma do zapłaty (z wysyłką i opłatą)
    max_per_day: int = _env_int("SNIPER_BUY_MAX_PER_DAY", 2)            # ile zakupów na dobę
    pl_only: bool = _env_bool("SNIPER_BUY_PL_ONLY", True)               # tylko sprzedawca z Polski
    min_score: float = _env_float("SNIPER_BUY_MIN_SCORE", 8.0)          # minimalna ocena AI (zwykle > próg maila)


@dataclass(frozen=True)
class ScoutConfig:
    # Zbudowane z SNIPER_PROXY_HOST + SNIPER_PROXY_AUTH (albo SNIPER_PROXY_URL) - patrz build_proxy_url().
    proxy_url: str = field(default_factory=build_proxy_url)

    # Kategoria: numer catalog_id z Vinted (np. 3580 = laptopy) albo nazwa z CATEGORIES.
    # SNIPER_CATALOG ma pierwszeństwo, SNIPER_CATEGORY zostaje dla zgodności.
    category: str = _env("SNIPER_CATALOG") or _env("SNIPER_CATEGORY", "karty_pamieci")
    search_text: str = _env("SNIPER_SEARCH_TEXT")
    price_from: str = _env("SNIPER_PRICE_FROM", "100")   # minimalna cena w PLN (puste = bez filtra)
    price_to: str = _env("SNIPER_PRICE_TO")               # maksymalna cena w PLN (puste = bez filtra)
    # Ofert na jeden skan. svc-catalogue respektuje per_page (test check_per_page.py, 2026-10-02):
    # 96 ofert = ~39 KB transferu na skan, 20 ofert = ~9 KB. 20 to zapas ~1 h przy nowej ofercie co ~3 min.
    per_page: int = _env_int("SNIPER_PER_PAGE", 20)
    # Pamięć ID - musi być kilka razy większa niż strona katalogu; za mała jest podnoszona do 5 x per_page (min. 100).
    dedup_size: int = _env_int("SNIPER_DEDUP_SIZE", 100)

    # Odstęp między STARTAMI kolejnych skanów katalogu: 15 s = 4 skany na minutę (oszczędza transfer proxy)
    poll_interval: float = _env_float("SNIPER_POLL_INTERVAL", 15.0)
    poll_jitter: float = _env_float("SNIPER_POLL_JITTER", 1.0)       # losowe odchylenie +/- jitter sekund
    heartbeat_interval: float = _env_float("SNIPER_HEARTBEAT", 60.0)  # co ile sekund log "żyję" (0 = wyłączony)
    max_concurrent_details: int = _env_int("SNIPER_MAX_CONCURRENT_DETAILS", 5)
    request_timeout: float = _env_float("SNIPER_REQUEST_TIMEOUT", 10.0)
    # Pierwszy skan tylko "zapamiętuje" obecne oferty, bez alertów (żeby nie zalać skrzynki).
    skip_initial_batch: bool = _env_bool("SNIPER_SKIP_INITIAL_BATCH", True)

    browser_wait_ms: int = _env_int("SNIPER_BROWSER_WAIT_MS", 15000)
    # Odświeżanie sesji przez Playwright (przy rotacyjnym proxy każda próba = nowe IP):
    refresh_attempts: int = _env_int("SNIPER_REFRESH_ATTEMPTS", 6)          # prób w jednej serii
    refresh_retry_delay: float = _env_float("SNIPER_REFRESH_RETRY_DELAY", 5.0)  # s między próbami
    refresh_timeout: float = _env_float("SNIPER_REFRESH_TIMEOUT", 90.0)    # s limitu na jedną próbę
    refresh_backoff: float = _env_float("SNIPER_REFRESH_BACKOFF", 30.0)    # s przerwy po nieudanej serii
    # Lekka przeglądarka: bez obrazków/wideo/fontów i skryptów reklamowych (wizyta ~9 MB -> ułamek tego).
    browser_light: bool = _env_bool("SNIPER_BROWSER_LIGHT", True)
    # Sesja (ciastka + tokeny) zapisywana na dysk i używana po restarcie, jeśli młodsza niż tyle minut
    # (0 = zawsze nowa sesja przeglądarką). Gdy wygaśnie, 401/403 i tak wywoła odświeżenie.
    session_max_age_min: float = _env_float("SNIPER_SESSION_MAX_AGE", 360.0)
    # Folder na logi: sniper.log (rotacja co północ, 30 dni) + offers.jsonl (złapane oferty)
    log_dir: str = _env("SNIPER_LOG_DIR") or str(Path(__file__).with_name("logs"))

    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    buyer: BuyerConfig = field(default_factory=BuyerConfig)

    @property
    def catalog_id(self):
        return CATEGORIES.get(self.category, self.category)
