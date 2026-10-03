"""Licznik transferu przez proxy - żeby świadomie dobrać per_page i częstotliwość skanów.

Kategorie:
  * catalog  - zapytania do api.vinted.pl/svc-catalogue (co skan),
  * details  - sidebar + shipping_details złapanych ofert,
  * browser  - odświeżanie sesji przez Playwright (liczone na przekaźniku: dokładne bajty, z TLS).

Dla httpx liczymy: wysłane = linia żądania + nagłówki (z ciastkami!), odebrane = linia statusu +
nagłówki + ciało tak, jak przeszło łączem (skompresowane). Narzut TLS dla httpx nie jest widoczny
z Pythona - przy połączeniach keep-alive to zwykle kilka %.
"""
import time

CATEGORIES = ("catalog", "details", "browser")


def _headers_size(headers):
    return sum(len(k) + len(v) + 4 for k, v in headers.raw)   # "k: v\r\n"


def request_bytes(request):
    line = len(request.method) + len(request.url.raw_path) + 12   # "GET <ścieżka> HTTP/1.1\r\n"
    return line + _headers_size(request.headers) + 2 + len(request.content or b"")


def response_bytes(response):
    # num_bytes_downloaded = bajty ciała tak, jak przyszły łączem (przed rozpakowaniem gzip).
    body = response.num_bytes_downloaded
    if not body:
        length = response.headers.get("content-length")
        body = int(length) if length and length.isdigit() else len(response.content or b"")
    return 17 + len(response.reason_phrase or "") + _headers_size(response.headers) + 2 + body


def fmt_bytes(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MB"
    return f"{n / 1024:.1f} KB"


class TrafficMeter:
    def __init__(self):
        self.started = time.monotonic()
        self.total = {c: {"sent": 0, "received": 0, "requests": 0} for c in CATEGORIES}
        self._window = {c: {"sent": 0, "received": 0, "requests": 0} for c in CATEGORIES}
        self._window_started = self.started

    def add(self, category, sent=0, received=0, requests=1):
        for bucket in (self.total[category], self._window[category]):
            bucket["sent"] += sent
            bucket["received"] += received
            bucket["requests"] += requests

    def add_http(self, category, response):
        self.add(category, request_bytes(response.request), response_bytes(response))

    @staticmethod
    def _sum(buckets):
        return sum(b["sent"] + b["received"] for b in buckets.values())

    def window_report(self):
        """Podsumowanie od ostatniego wywołania + prognoza na godzinę/dobę z całego czasu działania. Zeruje okno."""
        now = time.monotonic()
        window_s = max(now - self._window_started, 1e-9)
        total_s = max(now - self.started, 1e-9)
        parts = []
        for c in CATEGORIES:
            b = self._window[c]
            size = b["sent"] + b["received"]
            if b["requests"]:
                parts.append(f"{c} {fmt_bytes(size)} ({b['requests']} zap., śr. {fmt_bytes(size / b['requests'])}"
                             f", wysł. {fmt_bytes(b['sent'])})")
        window_total = self._sum(self._window)
        all_total = self._sum(self.total)
        browser_total = self.total["browser"]["sent"] + self.total["browser"]["received"]
        # Tempo skanowania bez jednorazowych wizyt przeglądarki - inaczej start zawyża prognozę wielokrotnie.
        per_hour = (all_total - browser_total) / total_s * 3600
        row = {
            "window_s": round(window_s, 1),
            **{f"{c}_bytes": self._window[c]["sent"] + self._window[c]["received"] for c in CATEGORIES},
            **{f"{c}_requests": self._window[c]["requests"] for c in CATEGORIES},
            "window_bytes": window_total,
            "total_bytes": all_total,
            "scan_mb_per_hour": round(per_hour / 1024 / 1024, 3),
            "browser_visits_total": self.total["browser"]["requests"],
            "browser_bytes_total": browser_total,
        }
        visits = self.total["browser"]["requests"]
        text = (f"Transfer ({window_s:.0f}s): {fmt_bytes(window_total)} = " + ("; ".join(parts) or "brak ruchu")
                + f" | od startu {fmt_bytes(all_total)} | skanowanie ~{fmt_bytes(per_hour)}/h, ~{fmt_bytes(per_hour * 24)}/dobę"
                + f" | przeglądarka: {visits} wizyt, {fmt_bytes(browser_total)}"
                + (f" (śr. {fmt_bytes(browser_total / visits)}/wizytę)" if visits else ""))
        self._window = {c: {"sent": 0, "received": 0, "requests": 0} for c in CATEGORIES}
        self._window_started = now
        return text, row
