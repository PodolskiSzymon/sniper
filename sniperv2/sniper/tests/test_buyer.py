"""Testy rdzenia auto-zakupu (sniper/buyer.py) na PRAWDZIWEJ odpowiedzi checkout (cURL użytkownika)."""
import json
from datetime import date, datetime, timedelta, timezone

from sniper.buyer import PurchaseLedger, decide_purchase, parse_checkout, summarize
from sniper.config import BuyerConfig

# Fragment realnej odpowiedzi /api/v2/purchases/{id}/checkout (taśma klejąca, 20 zł + 3,90 opłata, suma 23,90).
CHECKOUT = {
    "checkout": {
        "id": "ajg2iUlYVsDYaWtSDQO1n",
        "checksum": "7b18313dd287da51bf58848c7d9087d5|10c3ba547e60bad51ff2fb73830d51f7",
        "components": {
            "order_summary_v2": {
                "order_items": [{
                    "id": "10225109576", "title": "6 sztuk szeroka taśma klejąca pakowa",
                    "price": {"amount": "20.0", "currency_code": "PLN"},
                }],
            },
            "payment_method": {
                "selected_payment_method": {"credit_card": {"last4": "1111", "brand": "MasterCard"}},
            },
            "pay_button_v2": {
                "payments_available": True, "button_title": "Zapłać",
                "total": {"price": {"amount": "23.9", "currency_code": "PLN"}},
            },
            "shipping_address": {"address": {"country_code": "PL", "city": "Miasto"}},
        },
    },
    "code": 0,
}


def cfg(**kw):
    base = dict(enabled=True, max_total_pln=2500.0, max_per_day=2, pl_only=True, min_score=8.0)
    base.update(kw)
    return BuyerConfig(**base)


def offer(score=9, country_code="PL"):
    return {"evaluation": {"score": score}, "offer": {"seller": {"country_code": country_code}}}


# ----------------------------------------------------------------------------- parser
def test_parse_checkout_real_payload():
    p = parse_checkout(CHECKOUT)
    assert p["purchase_id"] == "ajg2iUlYVsDYaWtSDQO1n" and p["checksum"].startswith("7b18313")
    assert p["item_id"] == "10225109576" and p["item_title"].startswith("6 sztuk")
    assert p["item_count"] == 1 and p["item_price"] == 20.0
    assert p["total"] == 23.9 and p["currency"] == "PLN"
    assert p["payments_available"] is True and p["pay_button_title"] == "Zapłać"
    assert p["card_last4"] == "1111" and p["buyer_country"] == "PL"
    assert "taśma" in summarize(p) and "...1111" in summarize(p)


def test_parse_checkout_handles_empty():
    p = parse_checkout({})
    assert p["item_id"] is None and p["total"] is None and p["payments_available"] is False


# ----------------------------------------------------------------------------- decyzja
def test_decide_buys_when_all_ok(tmp_path):
    ok, reason = decide_purchase(parse_checkout(CHECKOUT), cfg(), PurchaseLedger(tmp_path), offer())
    assert ok is True and "taśma" in reason


def test_decide_blocks_over_total(tmp_path):
    ok, reason = decide_purchase(parse_checkout(CHECKOUT), cfg(max_total_pln=10.0), PurchaseLedger(tmp_path), offer())
    assert ok is False and "limit" in reason and "10" in reason


def test_decide_blocks_low_score(tmp_path):
    ok, reason = decide_purchase(parse_checkout(CHECKOUT), cfg(min_score=8.0), PurchaseLedger(tmp_path), offer(score=6))
    assert ok is False and "ocena AI 6" in reason


def test_decide_blocks_foreign_seller(tmp_path):
    ok, reason = decide_purchase(parse_checkout(CHECKOUT), cfg(pl_only=True), PurchaseLedger(tmp_path),
                                 offer(country_code="LT"))
    assert ok is False and "spoza PL" in reason
    ok2, _ = decide_purchase(parse_checkout(CHECKOUT), cfg(pl_only=False), PurchaseLedger(tmp_path),
                             offer(country_code="LT"))
    assert ok2 is True            # wyłączony pl_only przepuszcza


def test_decide_blocks_when_payments_unavailable(tmp_path):
    payload = json.loads(json.dumps(CHECKOUT))
    payload["checkout"]["components"]["pay_button_v2"]["payments_available"] = False
    ok, reason = decide_purchase(parse_checkout(payload), cfg(), PurchaseLedger(tmp_path), offer())
    assert ok is False and "niedostępna" in reason


def test_decide_blocks_multiple_items(tmp_path):
    payload = json.loads(json.dumps(CHECKOUT))
    items = payload["checkout"]["components"]["order_summary_v2"]["order_items"]
    items.append(dict(items[0]))
    ok, reason = decide_purchase(parse_checkout(payload), cfg(), PurchaseLedger(tmp_path), offer())
    assert ok is False and "2 przedmiot" in reason


def test_decide_without_offer_skips_score_and_country(tmp_path):
    ok, _ = decide_purchase(parse_checkout(CHECKOUT), cfg(), PurchaseLedger(tmp_path), offer=None)
    assert ok is True            # bez danych oferty sprawdzamy tylko limity ceny/ilości


# ----------------------------------------------------------------------------- rejestr
def test_ledger_blocks_duplicate_and_counts_daily(tmp_path):
    ledger = PurchaseLedger(tmp_path)
    parsed = parse_checkout(CHECKOUT)
    assert ledger.already_bought(parsed["item_id"]) is False
    ledger.record(parsed, "bought")
    assert ledger.already_bought(parsed["item_id"]) is True
    assert ledger.count_today() == 1

    ok, reason = decide_purchase(parsed, cfg(), ledger, offer())
    assert ok is False and "już kupiona" in reason


def test_ledger_daily_limit_blocks(tmp_path):
    ledger = PurchaseLedger(tmp_path)
    for i in range(2):
        ledger.record({"item_id": f"x{i}", "item_title": "t", "total": 10}, "bought")
    ok, reason = decide_purchase(parse_checkout(CHECKOUT), cfg(max_per_day=2), ledger, offer())
    assert ok is False and "na dobę" in reason


def test_ledger_persists_to_disk_and_reloads(tmp_path):
    PurchaseLedger(tmp_path).record(parse_checkout(CHECKOUT), "bought")
    again = PurchaseLedger(tmp_path)
    assert again.already_bought("10225109576") and again.count_today() == 1
    row = json.loads((tmp_path / "bought.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["item_id"] == "10225109576" and row["status"] == "bought" and row["total"] == 23.9


def test_ledger_old_purchases_dont_count_today(tmp_path):
    ledger = PurchaseLedger(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=3)).astimezone().date()
    ledger._rows.append({"status": "bought", "local_date": old.isoformat(), "item_id": "z"})
    assert ledger.count_today() == 0 and ledger.count_on(old) == 1


def test_dry_run_record_does_not_count_as_bought(tmp_path):
    ledger = PurchaseLedger(tmp_path)
    ledger.record(parse_checkout(CHECKOUT), "dry_run", "tryb próbny")
    assert ledger.count_today() == 0                      # tryb próbny nie zajmuje limitu dobowego
    assert ledger.already_bought("10225109576") is False  # ani nie blokuje przyszłego realnego zakupu


# ----------------------------------------------------------------------------- orkiestracja (atrapa przeglądarki)
import asyncio  # noqa: E402

from sniper.buyer import attempt_purchase  # noqa: E402


class FakeNav:
    """Atrapa przeglądarki - NIE ma metody pay(): bot nigdy nie płaci."""

    def __init__(self, payload=CHECKOUT, fail_checkout=False):
        self.payload = payload
        self.fail_checkout = fail_checkout
        self.calls = []

    async def open(self, url):
        self.calls.append(("open", url))

    async def buy_now_and_get_checkout(self):
        self.calls.append(("buy_now",))
        if self.fail_checkout:
            raise RuntimeError("brak przycisku Kup teraz")
        return self.payload

    async def focus(self):
        self.calls.append(("focus",))


def test_attempt_ready_prepares_checkout_but_never_pays(tmp_path):
    nav, ledger = FakeNav(), PurchaseLedger(tmp_path)
    result = asyncio.run(attempt_purchase(nav, "url", offer(), cfg(), ledger))
    assert result["status"] == "ready"
    assert [c[0] for c in nav.calls] == ["open", "buy_now", "focus"]
    assert not hasattr(nav, "pay")                              # w atrapie nie ma płacenia
    assert ledger.already_bought("10225109576") is True        # 'ready' blokuje ponowne przygotowanie
    assert ledger.count_today() == 1


def test_attempt_skips_over_limit_and_does_not_prepare(tmp_path):
    nav, ledger = FakeNav(), PurchaseLedger(tmp_path)
    result = asyncio.run(attempt_purchase(nav, "url", offer(), cfg(max_total_pln=5.0), ledger))
    assert result["status"] == "skipped" and "limit" in result["reason"]
    assert ("focus",) not in nav.calls and ledger.already_bought("10225109576") is False


def test_attempt_skips_foreign_seller(tmp_path):
    nav, ledger = FakeNav(), PurchaseLedger(tmp_path)
    result = asyncio.run(attempt_purchase(nav, "url", offer(country_code="LT"), cfg(pl_only=True), ledger))
    assert result["status"] == "skipped" and "spoza PL" in result["reason"]


def test_attempt_handles_checkout_error(tmp_path):
    nav, ledger = FakeNav(fail_checkout=True), PurchaseLedger(tmp_path)
    result = asyncio.run(attempt_purchase(nav, "url", offer(), cfg(), ledger))
    assert result["status"] == "error" and ("focus",) not in nav.calls


def test_cli_requires_url():
    import pytest as _pytest
    with _pytest.raises(SystemExit):           # brak URL => argparse kończy z błędem
        asyncio.run(attempt_cli_noargs())


async def attempt_cli_noargs():
    from sniper.buyer import _cli
    return await _cli([])
