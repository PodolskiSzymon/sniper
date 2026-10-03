"""Asynchroniczne alerty e-mail przez SMTP Onetu (aiosmtplib, SSL na porcie 465).

Mail testowy (sprawdzenie konfiguracji z sniper/.env bez czekania na ogłoszenie):
    python -m sniper.notifier
"""
import asyncio
import logging
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape

import aiosmtplib

log = logging.getLogger("sniper.notifier")

PROFILE_URL = "https://www.vinted.pl/member/{seller_id}"


def _fmt_money(amount, currency):
    return f"{amount:.2f} {currency}" if amount is not None else "brak danych"


def shipping_text(offer):
    ship = offer.shipping
    if ship is None:
        return "brak danych"
    if ship.free_shipping:
        return "darmowa"
    if ship.pickup_only:
        return "tylko odbiór osobisty"
    return _fmt_money(ship.price, ship.currency or offer.currency)


def _seller_lines(offer):
    seller = offer.seller
    stars = f"{seller.stars:.1f}/5" if seller.stars is not None else "brak"
    feedback = seller.feedback_count if seller.feedback_count is not None else "?"
    profile = PROFILE_URL.format(seller_id=seller.id) if seller.id else None
    kind = "firma" if seller.business else "osoba prywatna" if seller.business is not None else "?"
    return {
        "Nazwa": seller.name or "?",
        "Kraj": seller.country or seller.country_code or "nieznany",
        "Ocena": f"{stars} ({feedback} opinii)",
        "Typ konta": kind,
        "Profil": profile or "brak",
    }


def _pln(value):
    return f"{value:.0f} zł" if isinstance(value, (int, float)) else "?"


def ai_headline(ai):
    """Krótki werdykt AI do tematu maila (None = moduł AI wyłączony)."""
    if not ai:
        return None
    if ai["status"] == "oceniona":
        ev = ai["evaluation"]
        return f"{ev['score']:g}/10 {'OKAZJA' if ev['is_deal'] else 'nie kupować'}"
    if ai["status"] == "odfiltrowana":
        return "pominięta przez filtr"
    return "NIEOCENIONA"


def ai_lines(ai):
    """Szczegóły oceny AI jako lista (etykieta, wartość)."""
    if not ai:
        return []
    if ai["status"] == "odfiltrowana":
        return [("Filtr przed AI", ai.get("prefilter_reason") or "?")]
    if ai["status"] != "oceniona":
        return [("Błąd AI", f"{ai.get('error') or '?'} - oceń ofertę samodzielnie")]
    ev = ai["evaluation"]
    return [
        ("Karta", ev.get("gpu_model") or "nie rozpoznano"),
        ("Laptop", ev.get("laptop_model") or "?"),
        ("Wartość rynkowa", _pln(ev.get("market_value_pln"))),
        ("Maks. cena zakupu", _pln(ev.get("max_buy_price_pln"))),
        ("Potencjalny zysk", _pln(ev.get("potential_profit_pln"))),
        ("Uzasadnienie", ev.get("reasoning") or "-"),
        ("Zdjęcia (AI)", f"widzi {ev.get('photos_seen', '?')} z {ai.get('photos_sent', '?')}"
                         + (f": {ev['photo_notes']}" if ev.get("photo_notes") else "")),
        ("Czerwone flagi", "; ".join(ev.get("red_flags") or []) or "brak"),
    ]


def _ai_text(ai):
    if not ai:
        return ""
    lines = "\n".join(f"  {k + ':':<19}{v}" for k, v in ai_lines(ai))
    return f"=== OCENA AI: {ai_headline(ai)} ===\n{lines}\n\n"


def _ai_html(ai, e):
    if not ai:
        return ""
    color = {"oceniona": "#0a7d32" if (ai.get("evaluation") or {}).get("is_deal") else "#555",
             "odfiltrowana": "#888"}.get(ai["status"], "#c0392b")
    rows = "".join(f"<tr><td style='color:#666;padding:2px 12px 2px 0;vertical-align:top'>{e(k)}</td>"
                   f"<td>{e(v)}</td></tr>" for k, v in ai_lines(ai))
    return (f"<div style='border:2px solid {color};border-radius:8px;padding:10px 14px;margin:0 0 14px'>"
            f"<div style='font-size:18px;font-weight:bold;color:{color};margin-bottom:6px'>"
            f"Ocena AI: {e(ai_headline(ai))}</div><table>{rows}</table></div>")


def _text_body(offer, price, shipping, total, ai=None):
    seller = "\n".join(f"  {k + ':':<11}{v}" for k, v in _seller_lines(offer).items())
    photos = "\n".join(f"  {i}. {u}" for i, u in enumerate(offer.photo_urls, 1)) or "  (brak)"
    return (
        f"Nowa oferta na Vinted!\n\n"
        f"{_ai_text(ai)}"
        f"Tytuł:     {offer.title}\n"
        f"Cena:      {price}\n"
        f"Wysyłka:   {shipping}\n"
        f"Razem:     {total}\n"
        f"Stan:      {offer.condition or '?'}\n"
        f"Marka:     {offer.brand or '?'}\n"
        f"Link:      {offer.url}\n\n"
        f"Sprzedawca:\n{seller}\n\n"
        f"Opis:\n{offer.description or '(brak)'}\n\n"
        f"Zdjęcia ({len(offer.photo_urls)}):\n{photos}\n"
    )


def _html_body(offer, price, shipping, total, ai=None):
    e = lambda v: escape(str(v))  # noqa: E731
    seller_rows = "".join(
        f"<tr><td style='color:#666;padding:2px 12px 2px 0'>{e(k)}</td><td>"
        + (f"<a href='{e(v)}'>{e(v)}</a>" if k == "Profil" and v != "brak" else e(v))
        + "</td></tr>"
        for k, v in _seller_lines(offer).items()
    )
    photos = "".join(
        f"<a href='{e(u)}'><img src='{e(u)}' alt='zdjęcie {i}' "
        f"style='height:180px;margin:0 6px 6px 0;border-radius:6px;border:1px solid #ddd'></a>"
        for i, u in enumerate(offer.photo_urls, 1)
    ) or "(brak zdjęć)"
    photo_links = "".join(f"<li><a href='{e(u)}'>{e(u)}</a></li>" for u in offer.photo_urls)
    description = e(offer.description or "(brak)").replace("\n", "<br>")
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
{_ai_html(ai, e)}
<h2 style="margin:0 0 8px">{e(offer.title)}</h2>
<p style="font-size:16px;margin:0 0 12px">
  <b>{e(price)}</b> + wysyłka <b>{e(shipping)}</b> = <b>{e(total)}</b><br>
  <span style="color:#666">Stan: {e(offer.condition or '?')} · Marka: {e(offer.brand or '?')}</span>
</p>
<p><a href="{e(offer.url)}" style="background:#09b1ba;color:#fff;padding:10px 16px;border-radius:6px;
   text-decoration:none;display:inline-block">Otwórz ogłoszenie na Vinted</a></p>
<h3 style="margin:16px 0 6px">Zdjęcia ({len(offer.photo_urls)})</h3>
<div>{photos}</div>
<ol style="font-size:12px;color:#666">{photo_links}</ol>
<h3 style="margin:16px 0 6px">Opis</h3>
<p>{description}</p>
<h3 style="margin:16px 0 6px">Sprzedawca</h3>
<table>{seller_rows}</table>
<p style="color:#999;font-size:11px;margin-top:20px">ID oferty: {e(offer.id)} · wykryto: {e(offer.detected_at)}</p>
</body></html>"""


def build_message(offer, sender, recipient, ai=None):
    price = _fmt_money(offer.price, offer.currency)
    shipping = shipping_text(offer)
    total = _fmt_money(offer.total_price, offer.currency)

    msg = EmailMessage()
    verdict = ai_headline(ai)
    msg["Subject"] = (f"[Sniper]{f' {verdict} |' if verdict else ''} {offer.title} | "
                      f"{price} + wysyłka {shipping}")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="sniper.local")
    msg.set_content(_text_body(offer, price, shipping, total, ai))
    msg.add_alternative(_html_body(offer, price, shipping, total, ai), subtype="html")
    return msg


class EmailNotifier:
    """Wysyła maile w tle - główna pętla tylko tworzy zadanie i leci dalej."""

    def __init__(self, smtp_config):
        self.cfg = smtp_config
        self._tasks = set()
        self.sent = 0
        self.failed = 0
        if not self.cfg.enabled:
            log.warning("[MAIL] Brak SNIPER_SMTP_USER/SNIPER_SMTP_PASSWORD - alerty e-mail wyłączone.")
        else:
            log.info("[MAIL] Alerty e-mail włączone: %s -> %s (%s:%s)",
                     self.cfg.sender, self.cfg.recipient, self.cfg.host, self.cfg.port)

    def notify(self, offer, ai=None):
        """Nieblokujące: planuje wysyłkę i natychmiast wraca. ai = rekord oceny z evaluator.py (opcjonalnie)."""
        if not self.cfg.enabled:
            return
        task = asyncio.create_task(self._send(offer, ai), name=f"mail-{offer.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send_now(self, offer, ai=None):
        """Wysyła od razu i rzuca wyjątek przy błędzie (dla maila testowego)."""
        message = build_message(offer, self.cfg.sender, self.cfg.recipient, ai)
        await aiosmtplib.send(
            message,
            hostname=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.username,
            password=self.cfg.password,
            use_tls=True,          # SSL od początku połączenia (port 465)
            timeout=self.cfg.timeout,
        )

    async def _send(self, offer, ai=None):
        try:
            await self.send_now(offer, ai)
            self.sent += 1
            log.info("[MAIL] Wysłano alert dla %s -> %s", offer.id, self.cfg.recipient)
        except Exception as exc:
            self.failed += 1
            log.error("[MAIL] Nie udało się wysłać alertu dla %s: %r", offer.id, exc)

    async def drain(self, timeout=15.0):
        """Przy zamykaniu programu - daje szansę dokończyć wysyłkę maili w locie."""
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)


def sample_offer():
    """Przykładowa oferta (dane z api.docx) do maila testowego."""
    from .extractor import Offer, Seller, Shipping

    return Offer(
        id=9238023547,
        url="https://www.vinted.pl/items/9238023547",
        title="[TEST] Samsung pro ultimate 512GB",
        price=261.08,
        currency="PLN",
        description="To jest mail testowy Zwiadowcy.\nPrawdziwe alerty będą wyglądać tak samo.",
        photo_urls=[
            "https://images1.vinted.net/tc/01_00aa0_x12a89U2qGnnRiBPaUktzcfF/1782210267.webp?s=7ab89321622f25c5150bff67caaca3891b0aad16",
            "https://images1.vinted.net/tc/03_01b11_91gVnbc6hZxWMu4MGkLzNTRJ/1782210267.webp?s=cb5f40dd249c62c96183c8f5a9ed083d91c54f7f",
        ],
        seller=Seller(id=148344250, name="skestenyte.ska", country="Litwa", country_code="LT",
                      feedback_count=7, feedback_reputation=1.0, stars=5.0, business=False),
        shipping=Shipping(price=13.27, currency="PLN", free_shipping=False, pickup_only=False,
                          multiple_options=True),
        total_price=274.35,
        brand="Samsung",
        condition="Nowy z metką",
    )


async def _send_test():
    from .config import SmtpConfig

    cfg = SmtpConfig()
    if not cfg.enabled:
        print("Brak SNIPER_SMTP_USER / SNIPER_SMTP_PASSWORD w sniper/.env - nie mam jak wysłać.")
        return 1
    print(f"Wysyłam mail testowy: {cfg.sender} -> {cfg.recipient} przez {cfg.host}:{cfg.port} ...")
    try:
        await EmailNotifier(cfg).send_now(sample_offer())
    except aiosmtplib.SMTPAuthenticationError as exc:
        print(f"BŁĄD LOGOWANIA: {exc}\n -> sprawdź login/hasło i czy w ustawieniach Onetu jest włączony "
              f"dostęp przez programy pocztowe (SMTP).")
        return 1
    except Exception as exc:
        print(f"BŁĄD: {exc!r}")
        return 1
    print("Wysłano. Sprawdź skrzynkę (także folder SPAM).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_send_test()))
