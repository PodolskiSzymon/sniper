"""Testy modułu oceny AI - bez prawdziwych wywołań API (atrapa klienta / MockTransport)."""
import asyncio
import csv
import json
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx
import httpx2

from sniper.config import AiConfig, ScoutConfig, SmtpConfig
from sniper.evaluator import (EVALUATION_SCHEMA, FALLBACK_BETA, STATUS_EVALUATED, STATUS_FAILED, STATUS_FILTERED,
                              OfferEvaluator, build_request, prefilter)
from sniper.extractor import Offer, Seller, Shipping
from sniper.notifier import EmailNotifier, build_message
from sniper.scout import Scout
from sniper.session import VintedSession

PHOTOS = [f"https://images1.vinted.net/tc/{i}/f800.webp?s=abc{i}" for i in range(1, 10)]


def laptop(**kw):
    data = dict(
        id=7001, url="https://www.vinted.pl/items/7001-legion", title="Lenovo Legion 5 RTX 4060 16GB",
        price=1700.0, currency="PLN", description="Sprawny, bateria 90%, ładowarka w zestawie.",
        photo_urls=PHOTOS[:3],
        seller=Seller(id=1, name="jan", country="Polska", country_code="PL", feedback_count=12,
                      feedback_reputation=1.0, stars=5.0, business=False),
        shipping=Shipping(price=15.0, currency="PLN", free_shipping=False, pickup_only=False, multiple_options=True),
        total_price=1715.0, brand="Lenovo", condition="Bardzo dobry",
    )
    data.update(kw)
    return Offer(**data)


def ai_cfg(tmp_path, **kw):
    guidelines = tmp_path / "guidelines.md"
    if not guidelines.exists():
        guidelines.write_text("<!-- komentarz -->\nRTX 4060: maksymalna cena zakupu poniżej 1950 zł\n", encoding="utf-8")
    base = AiConfig(enabled=True, provider="anthropic", api_key="test", model="claude-opus-5-5", effort="medium",
                    fallback=True, photos="url",
                    max_tokens=8000, max_photos=6, guidelines_file=str(guidelines), min_score=7.0,
                    notify_all=False, max_concurrent=2, timeout=5.0, retries=2, retry_delay=0.0,
                    price_min=None, price_max=9000.0, keywords=("rtx", "4060"), exclude_keywords=(),
                    keywords_in="title+description")
    return replace(base, **kw)


EVAL_DEAL = {"is_deal": True, "score": 8, "gpu_model": "RTX 4060", "laptop_model": "Legion 5",
             "market_value_pln": 3100, "max_buy_price_pln": 1950, "potential_profit_pln": 1235,
             "reasoning": "RTX 4060 za 1715 zł łącznie, poniżej progu 1950 zł.", "red_flags": []}


def response(payload=EVAL_DEAL, stop_reason="end_turn", text=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=text if text is not None else json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_creation_input_tokens=0,
                              cache_read_input_tokens=2500),
    )


class FakeClient:
    """Atrapa AsyncAnthropic: client.beta.messages.create(**kw) zwraca kolejne odpowiedzi / rzuca wyjątki."""

    def __init__(self, *results, delay=0.0):
        self.results = list(results)
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self.active -= 1

    async def close(self):
        pass


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, offer, ai=None):
        self.sent.append((offer, ai))


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(f"blad {status}", response=httpx2.Response(status, request=request), body=None)


def run(coro):
    return asyncio.run(coro)


async def submit_and_wait(evaluator, *offers):
    for offer in offers:
        evaluator.submit(offer)
    await asyncio.gather(*evaluator._tasks)


# ----------------------------------------------------------------------------- filtr wstępny
def test_prefilter_price_and_keywords(tmp_path):
    cfg = ai_cfg(tmp_path, price_min=500.0, price_max=3000.0, exclude_keywords=("na części",))
    assert prefilter(laptop(), cfg) is None
    assert "> 3000" in prefilter(laptop(total_price=3500.0), cfg)
    assert "< 500" in prefilter(laptop(price=300.0, total_price=315.0), cfg)
    assert prefilter(laptop(title="Dell Latitude i5", description="biurowy"), cfg) == "brak słów kluczowych"
    # słowo kluczowe tylko w opisie: przechodzi przy title+description, odpada przy title
    in_desc = laptop(title="Laptop gamingowy MSI", description="Karta RTX 4060, 16 GB RAM")
    assert prefilter(in_desc, cfg) is None
    assert prefilter(in_desc, replace(cfg, keywords_in="title")) == "brak słów kluczowych"
    assert "na części" in prefilter(laptop(description="Sprzedam na części"), cfg)
    assert prefilter(laptop(title="Dell"), replace(cfg, keywords=())) is None   # puste = bez filtra słów


# ----------------------------------------------------------------------------- zapytanie
def test_build_request_photos_as_urls_and_structured_output(tmp_path):
    cfg = ai_cfg(tmp_path, max_photos=4)
    offer = laptop(photo_urls=PHOTOS)
    request, photos = build_request(offer, cfg, "MOJE WYTYCZNE")
    assert photos == 4
    content = request["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert [b["source"] for b in images] == [{"type": "url", "url": u} for u in PHOTOS[:4]]
    text = content[-1]["text"]
    for fragment in ("Lenovo Legion 5 RTX 4060", "1700.00 PLN", "15.00 PLN", "1715.00 PLN", "Bardzo dobry",
                     "Lenovo", "jan", "Polska", "5.0/5 (12 opinii)", "ładowarka w zestawie"):
        assert fragment in text
    system = request["system"][0]
    assert "<wytyczne>\nMOJE WYTYCZNE\n</wytyczne>" in system["text"]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": EVALUATION_SCHEMA},
                                        "effort": "medium"}
    assert request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
    assert "thinking" not in request and "temperature" not in request

    request, photos = build_request(offer, replace(cfg, fallback=False, effort=""), "W", with_photos=False)
    assert photos == 0 and not [b for b in request["messages"][0]["content"] if b["type"] == "image"]
    assert "fallbacks" not in request and "betas" not in request and "effort" not in request["output_config"]


def test_schema_objects_are_closed():
    assert EVALUATION_SCHEMA["additionalProperties"] is False
    assert set(EVALUATION_SCHEMA["required"]) == set(EVALUATION_SCHEMA["properties"])


def test_real_sdk_request_goes_direct_without_proxy(tmp_path, monkeypatch):
    """Prawdziwy AsyncAnthropic na MockTransport: SDK akceptuje nasze argumenty, nagłówek beta, brak proxy."""
    monkeypatch.setenv("SNIPER_PROXY_HOST", "geo.iproyal.com:12321")
    monkeypatch.setenv("SNIPER_PROXY_AUTH", "LOGIN:HASLO")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(EVAL_DEAL)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 200, "cache_creation_input_tokens": 2500,
                      "cache_read_input_tokens": 0},
        })

    async def scenario():
        client = anthropic.AsyncAnthropic(
            api_key="sk-test", max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
        evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), log_dir=tmp_path, client=client)
        record = await evaluator.evaluate(laptop())
        await evaluator.close()
        return record, evaluator

    record, evaluator = run(scenario())
    assert record["status"] == STATUS_EVALUATED and record["evaluation"]["score"] == 8
    assert seen["url"].startswith("https://api.anthropic.com/v1/messages")
    assert FALLBACK_BETA in seen["headers"]["anthropic-beta"]
    assert seen["headers"]["x-api-key"] == "sk-test" and "proxy-authorization" not in seen["headers"]
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][1] == {"type": "image", "source": {"type": "url", "url": PHOTOS[0]}}
    assert record["usage"]["cache_creation_input_tokens"] == 2500
    assert evaluator.total["calls"] == 1


def test_default_client_has_no_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_PROXY_HOST", "geo.iproyal.com:12321")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), log_dir=tmp_path)
    mounts = getattr(evaluator.client._client, "_mounts", {})
    assert not any(t is not None and "iproyal" in repr(getattr(t, "_pool", t)) for t in mounts.values())
    assert evaluator.client.max_retries == 0
    run(evaluator.close())


# ----------------------------------------------------------------------------- przepływ
def test_deal_is_mailed_and_saved(tmp_path):
    notifier = FakeNotifier()
    evaluator = OfferEvaluator(ai_cfg(tmp_path), notifier, log_dir=tmp_path, client=FakeClient(response()))
    run(submit_and_wait(evaluator, laptop()))

    assert len(notifier.sent) == 1
    offer, ai = notifier.sent[0]
    assert offer.id == 7001 and ai["status"] == STATUS_EVALUATED and ai["evaluation"]["score"] == 8

    lines = (tmp_path / "evaluations.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["offer"]["title"] == "Lenovo Legion 5 RTX 4060 16GB"
    assert record["evaluation"]["potential_profit_pln"] == 1235 and record["notified"] is True
    assert record["photos_sent"] == 3 and record["usage"]["cache_read_input_tokens"] == 2500

    raw = (tmp_path / "evaluations.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                     # BOM dla Excela
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
    assert rows[0][:4] == ["czas", "status", "ocena", "okazja"]
    row = dict(zip(rows[0], rows[1]))
    assert (row["ocena"], row["okazja"], row["mail"], row["karta"], row["zysk"]) == ("8", "TAK", "TAK", "RTX 4060", "1235")


def test_low_score_not_mailed_unless_notify_all(tmp_path):
    low = response({**EVAL_DEAL, "is_deal": False, "score": 3, "red_flags": ["porysowana matryca"]})
    notifier = FakeNotifier()
    evaluator = OfferEvaluator(ai_cfg(tmp_path), notifier, log_dir=tmp_path, client=FakeClient(low))
    run(submit_and_wait(evaluator, laptop()))
    assert notifier.sent == []
    assert json.loads((tmp_path / "evaluations.jsonl").read_text(encoding="utf-8"))["notified"] is False

    evaluator = OfferEvaluator(ai_cfg(tmp_path, notify_all=True), notifier, log_dir=tmp_path, client=FakeClient(low))
    run(submit_and_wait(evaluator, laptop()))
    assert len(notifier.sent) == 1 and notifier.sent[0][1]["evaluation"]["red_flags"] == ["porysowana matryca"]


def test_filtered_offer_costs_nothing_but_is_logged(tmp_path):
    client = FakeClient(response())
    notifier = FakeNotifier()
    evaluator = OfferEvaluator(ai_cfg(tmp_path), notifier, log_dir=tmp_path, client=client)
    evaluator.submit(laptop(title="Dell Latitude i5", description="biurowy"))
    assert client.calls == [] and notifier.sent == [] and not evaluator._tasks
    record = json.loads((tmp_path / "evaluations.jsonl").read_text(encoding="utf-8"))
    assert record["status"] == STATUS_FILTERED and record["prefilter_reason"] == "brak słów kluczowych"


def test_retry_then_success(tmp_path):
    client = FakeClient(anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
                        _status_error(anthropic.InternalServerError, 529), response())
    notifier = FakeNotifier()
    evaluator = OfferEvaluator(ai_cfg(tmp_path), notifier, log_dir=tmp_path, client=client)
    run(submit_and_wait(evaluator, laptop()))
    assert len(client.calls) == 3
    assert notifier.sent[0][1]["status"] == STATUS_EVALUATED and notifier.sent[0][1]["attempts"] == 3


def test_ai_failure_still_mailed_as_unevaluated(tmp_path):
    client = FakeClient(_status_error(anthropic.RateLimitError, 429))
    notifier = FakeNotifier()
    evaluator = OfferEvaluator(ai_cfg(tmp_path, retries=1), notifier, log_dir=tmp_path, client=client)
    run(submit_and_wait(evaluator, laptop()))
    assert len(client.calls) == 2                      # 1 próba + 1 ponowienie
    offer, ai = notifier.sent[0]
    assert ai["status"] == STATUS_FAILED and "429" in ai["error"]
    assert evaluator.total["failed"] == 1


def test_timeout_and_invalid_json_are_retried(tmp_path):
    async def scenario():
        client = FakeClient(response(text="to nie json"), response(), delay=0.0)
        evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), log_dir=tmp_path, client=client)
        first = await evaluator.evaluate(laptop())
        slow = FakeClient(response(), delay=1.0)
        evaluator2 = OfferEvaluator(ai_cfg(tmp_path, timeout=0.05, retries=1), FakeNotifier(), client=slow)
        second = await evaluator2.evaluate(laptop())
        return first, second, len(slow.calls)

    first, second, slow_calls = run(scenario())
    assert first["status"] == STATUS_EVALUATED and first["attempts"] == 2
    assert second["status"] == STATUS_FAILED and second["error"] == "przekroczony limit czasu" and slow_calls == 2


def test_auth_error_not_retried(tmp_path):
    client = FakeClient(_status_error(anthropic.AuthenticationError, 401))
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), client=client)
    record = run(evaluator.evaluate(laptop()))
    assert len(client.calls) == 1 and "klucz" in record["error"]


def test_bad_photo_url_retried_without_photos(tmp_path):
    client = FakeClient(_status_error(anthropic.BadRequestError, 400), response())
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), client=client)
    record = run(evaluator.evaluate(laptop()))
    assert record["status"] == STATUS_EVALUATED and record["photos_sent"] == 0
    assert any(b["type"] == "image" for b in client.calls[0]["messages"][0]["content"])
    assert not any(b["type"] == "image" for b in client.calls[1]["messages"][0]["content"])


def test_refusal_is_unevaluated(tmp_path):
    client = FakeClient(response(stop_reason="refusal", text=""))
    record = run(OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), client=client).evaluate(laptop()))
    assert record["status"] == STATUS_FAILED and "odmówił" in record["error"] and len(client.calls) == 1


def test_concurrency_limit(tmp_path):
    client = FakeClient(response(), delay=0.05)
    evaluator = OfferEvaluator(ai_cfg(tmp_path, max_concurrent=2), FakeNotifier(), client=client)
    run(submit_and_wait(evaluator, *[laptop(id=i) for i in range(6)]))
    assert len(client.calls) == 6 and client.max_active == 2


def test_guidelines_reloaded_after_edit(tmp_path):
    client = FakeClient(response())
    cfg = ai_cfg(tmp_path)
    evaluator = OfferEvaluator(cfg, FakeNotifier(), client=client)
    run(evaluator.evaluate(laptop()))
    path = tmp_path / "guidelines.md"
    path.write_text("NOWE WYTYCZNE: RTX 4060 do 1800 zł", encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))
    run(evaluator.evaluate(laptop()))
    first, second = (call["system"][0]["text"] for call in client.calls)
    assert "1950" in first and "komentarz" not in first
    assert "NOWE WYTYCZNE" in second


def test_missing_guidelines_offer_not_lost(tmp_path):
    notifier = FakeNotifier()
    cfg = ai_cfg(tmp_path, guidelines_file=str(tmp_path / "brak.md"))
    evaluator = OfferEvaluator(cfg, notifier, client=FakeClient(response()))
    assert evaluator.check_guidelines() is False
    run(submit_and_wait(evaluator, laptop()))
    assert notifier.sent[0][1]["status"] == STATUS_FAILED


def test_heartbeat_report_counts_tokens(tmp_path):
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), client=FakeClient(response()))
    run(submit_and_wait(evaluator, laptop(), laptop(id=2), laptop(id=3, title="Dell", description="")))
    text = evaluator.window_report()
    assert "ocenione 2" in text and "odfiltrowane 1" in text and "maile 2" in text and "wywołania 2" in text
    assert "tokeny we 7400 (cache 5000) / wy 600" in text and "$0.024" in text
    assert "ocenione 0" in evaluator.window_report()    # okno wyzerowane, suma od startu zostaje
    assert evaluator.total["calls"] == 2


# ----------------------------------------------------------------------------- mail i Zwiadowca
def test_mail_contains_ai_verdict(tmp_path):
    record = {"status": STATUS_EVALUATED, "evaluation": {**EVAL_DEAL, "red_flags": ["brak ładowarki"]}}
    msg = build_message(laptop(), "a@onet.pl", "b@onet.pl", ai=record)
    assert msg["Subject"].startswith("[Sniper] 8/10 OKAZJA | Lenovo Legion 5")
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    for body in (text, html):
        assert "RTX 4060" in body and "1235 zł" in body and "brak ładowarki" in body and "poniżej progu" in body

    failed = build_message(laptop(), "a", "b", ai={"status": STATUS_FAILED, "error": "HTTP 529"})
    assert "NIEOCENIONA" in failed["Subject"] and "HTTP 529" in failed.get_body(("plain",)).get_content()
    plain = build_message(laptop(), "a", "b")                   # bez AI - jak dotąd
    assert plain["Subject"].startswith("[Sniper] Lenovo")


def test_scout_emit_does_not_wait_for_ai(tmp_path):
    """Zwiadowca oddaje ofertę do oceny i od razu wraca; maila o każdej ofercie już nie wysyła sam."""
    async def scenario():
        cfg = ScoutConfig(category="3580", log_dir=str(tmp_path), smtp=SmtpConfig(username="", password=""))
        session = VintedSession()
        session.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        notifier = EmailNotifier(cfg.smtp)
        mails = []
        notifier.notify = lambda offer, ai=None: mails.append((offer.id, ai and ai["status"]))
        client = FakeClient(response(), delay=0.2)
        evaluator = OfferEvaluator(ai_cfg(tmp_path), notifier, log_dir=tmp_path, client=client)
        scout = Scout(cfg, session, notifier, evaluator)

        started = time.monotonic()
        scout.emit(laptop())
        elapsed = time.monotonic() - started
        assert mails == [] and len(evaluator._tasks) == 1
        await evaluator.drain()
        await session.close()
        return elapsed, mails, scout

    elapsed, mails, scout = run(scenario())
    assert elapsed < 0.1
    assert mails == [(7001, STATUS_EVALUATED)]
    assert scout.offers.qsize() == 1 and (tmp_path / "offers.jsonl").exists()


def test_offer_roundtrip_from_jsonl():
    offer = laptop()
    again = Offer.from_dict(json.loads(json.dumps(offer.to_dict())))
    assert again == offer
    no_ship = Offer.from_dict({**offer.to_dict(), "shipping": None, "seller": {}})
    assert no_ship.shipping is None and no_ship.seller.name is None


# ----------------------------------------------------------------------------- Gemini
from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types as genai_types  # noqa: E402

from sniper.evaluator import build_gemini_request, gemini_usage, image_mime  # noqa: E402


def gemini_cfg(tmp_path, **kw):
    return ai_cfg(tmp_path, **{"provider": "gemini", "model": "gemini-3.8-flash", **kw})


def gemini_response(payload=EVAL_DEAL, finish="STOP", text=None, block=None):
    return SimpleNamespace(
        text=text if text is not None else json.dumps(payload),
        candidates=[SimpleNamespace(finish_reason=genai_types.FinishReason(finish))],
        prompt_feedback=SimpleNamespace(block_reason=block) if block else None,
        usage_metadata=SimpleNamespace(prompt_token_count=4000, cached_content_token_count=1000,
                                       candidates_token_count=250, thoughts_token_count=750),
    )


class FakeGemini:
    """Atrapa genai.Client: client.aio.models.generate_content(**kw)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self.generate_content))

    async def generate_content(self, **kw):
        self.calls.append(kw)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def genai_error(code, message="blad", status="INVALID_ARGUMENT"):
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


def test_gemini_request_shape(tmp_path):
    cfg = gemini_cfg(tmp_path, max_photos=2)
    request = build_gemini_request(laptop(photo_urls=PHOTOS), cfg, "MOJE WYTYCZNE",
                                   [(PHOTOS[0], None), (PHOTOS[1], b"\x89PNG")])
    assert request["model"] == "gemini-3.8-flash"
    parts = request["contents"][0].parts
    assert parts[0].text.startswith("Zdjęcia z ogłoszenia (2 z 9)")
    assert parts[1].file_data.file_uri == PHOTOS[0] and parts[1].file_data.mime_type == "image/webp"
    assert parts[2].inline_data.data == b"\x89PNG"
    assert "1715.00 PLN" in parts[-1].text
    config = request["config"]
    assert "<wytyczne>\nMOJE WYTYCZNE\n</wytyczne>" in config.system_instruction
    assert config.response_mime_type == "application/json" and config.response_json_schema == EVALUATION_SCHEMA
    assert config.thinking_config.thinking_level.name == "MEDIUM" and config.max_output_tokens == 8000
    assert image_mime("https://x/a.JPG?s=1") == "image/jpeg" and image_mime("https://x/a") == "image/jpeg"


def test_gemini_usage_counts_cache_and_thoughts():
    assert gemini_usage(gemini_response()) == {"input_tokens": 3000, "output_tokens": 1000,
                                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000}


def test_gemini_real_sdk_request(tmp_path, monkeypatch):
    """Prawdziwy genai.Client na MockTransport: URL, klucz w nagłówku, zdjęcia jako fileData, JSON schema."""
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(EVAL_DEAL)}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5200, "candidatesTokenCount": 180, "thoughtsTokenCount": 620,
                              "cachedContentTokenCount": 0, "totalTokenCount": 6000},
        })

    async def scenario():
        from google import genai
        client = genai.Client(api_key="AQ.test-key", http_options=genai_types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
        evaluator = OfferEvaluator(gemini_cfg(tmp_path), FakeNotifier(), log_dir=tmp_path, client=client)
        record = await evaluator.evaluate(laptop())
        await evaluator.close()
        return record

    record = run(scenario())
    assert record["status"] == STATUS_EVALUATED and record["evaluation"]["gpu_model"] == "RTX 4060"
    assert seen["url"].startswith("https://generativelanguage.googleapis.com/")
    assert "models/gemini-3.8-flash:generateContent" in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "AQ.test-key" and "proxy-authorization" not in seen["headers"]
    body = seen["body"]
    parts = body["contents"][0]["parts"]
    file_data = parts[1]["fileData"]              # SDK wysyła pola w snake_case lub camelCase - API przyjmuje oba
    assert file_data.get("fileUri", file_data.get("file_uri")) == PHOTOS[0]
    assert file_data.get("mimeType", file_data.get("mime_type")) == "image/webp"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"]["required"] == EVALUATION_SCHEMA["required"]
    thinking = body["generationConfig"]["thinkingConfig"]
    assert thinking.get("thinkingLevel", thinking.get("thinking_level")) == "MEDIUM"
    assert "<wytyczne>" in body["systemInstruction"]["parts"][0]["text"]
    assert record["usage"] == {"input_tokens": 5200, "output_tokens": 800, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}


def test_gemini_deal_mailed(tmp_path):
    notifier = FakeNotifier()
    client = FakeGemini(gemini_response())
    evaluator = OfferEvaluator(gemini_cfg(tmp_path), notifier, log_dir=tmp_path, client=client)
    run(submit_and_wait(evaluator, laptop()))
    assert notifier.sent[0][1]["evaluation"]["score"] == 8 and notifier.sent[0][1]["model"] == "gemini-3.8-flash"
    assert "gemini" in evaluator.describe()


def test_gemini_retries_and_errors(tmp_path):
    # 503 -> ponowienie -> sukces
    client = FakeGemini(genai_error(503, status="UNAVAILABLE"), gemini_response())
    record = run(OfferEvaluator(gemini_cfg(tmp_path), FakeNotifier(), client=client).evaluate(laptop()))
    assert record["status"] == STATUS_EVALUATED and record["attempts"] == 2

    # zły klucz: Google zwraca 400 API_KEY_INVALID - bez ponawiania i bez "ponów bez zdjęć"
    bad_key = FakeGemini(genai_error(400, "API key not valid. Please pass a valid API key."))
    record = run(OfferEvaluator(gemini_cfg(tmp_path), FakeNotifier(), client=bad_key).evaluate(laptop()))
    assert len(bad_key.calls) == 1 and "klucz" in record["error"] and record["status"] == STATUS_FAILED

    # 400 przez zdjęcie -> ponowienie bez zdjęć
    photo = FakeGemini(genai_error(400, "Cannot fetch content from the provided URL."), gemini_response())
    record = run(OfferEvaluator(gemini_cfg(tmp_path), FakeNotifier(), client=photo).evaluate(laptop()))
    assert record["status"] == STATUS_EVALUATED and record["photos_sent"] == 0
    assert len(photo.calls[1]["contents"][0].parts) == 2      # opis braku zdjęć + dane oferty

    # odmowa / ucięcie
    for response, fragment in ((gemini_response(finish="SAFETY", text=""), "odmówił"),
                               (gemini_response(text="", block="PROHIBITED_CONTENT"), "odmówił"),
                               (gemini_response(finish="MAX_TOKENS", text='{"is_d'), "ucięta")):
        record = run(OfferEvaluator(gemini_cfg(tmp_path), FakeNotifier(),
                                    client=FakeGemini(response)).evaluate(laptop()))
        assert record["status"] == STATUS_FAILED and fragment in record["error"]


def test_gemini_download_mode_fetches_photos_directly(tmp_path):
    fetched = []

    def cdn(request):
        fetched.append(str(request.url))
        if "/3/" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"IMG" + str(request.url).encode()[-5:], headers={"content-type": "image/webp"})

    async def scenario():
        client = FakeGemini(gemini_response())
        evaluator = OfferEvaluator(gemini_cfg(tmp_path, photos="download"), FakeNotifier(), client=client)
        evaluator.backend._http = httpx.AsyncClient(transport=httpx.MockTransport(cdn))
        record = await evaluator.evaluate(laptop())
        await evaluator.close()
        return record, client.calls[0]["contents"][0].parts

    record, parts = run(scenario())
    assert len(fetched) == 3 and record["photos_sent"] == 2         # 404 pominięte, oferta oceniona
    inline = [p for p in parts if p.inline_data is not None]
    assert len(inline) == 2 and not [p for p in parts if p.file_data is not None]


def test_provider_and_key_from_env(monkeypatch):
    import importlib
    import sniper.config as config
    for name in ("SNIPER_AI_PROVIDER", "SNIPER_AI_API_KEY", "SNIPER_AI_MODEL", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.z-env")
    try:
        importlib.reload(config)
        cfg = config.AiConfig()
        assert (cfg.provider, cfg.api_key, cfg.model) == ("gemini", "AQ.z-env", "gemini-3.8-flash")
        monkeypatch.setenv("GEMINI_API_KEY", "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        importlib.reload(config)
        cfg = config.AiConfig()
        assert (cfg.provider, cfg.api_key, cfg.model) == ("anthropic", "sk-ant", "claude-opus-5-5")
        monkeypatch.setenv("SNIPER_AI_PROVIDER", "gemini")
        monkeypatch.setenv("SNIPER_AI_API_KEY", "AQ.ogolny")
        importlib.reload(config)
        cfg = config.AiConfig()
        assert (cfg.provider, cfg.api_key) == ("gemini", "AQ.ogolny")
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_cli_sample_offer_passes_prefilter(tmp_path):
    from sniper.evaluator import sample_laptop_offer
    offer = sample_laptop_offer()
    assert prefilter(offer, gemini_cfg(tmp_path, keywords=AiConfig().keywords)) is None
    assert offer.photo_urls == [] and "vinted.pl/items" not in offer.url


def test_seller_country_and_poland_rule_reach_model(tmp_path):
    from sniper.evaluator import offer_text, system_text
    from pathlib import Path
    assert "kraj: Polska (PL)" in offer_text(laptop())
    foreign = laptop(seller=Seller(id=2, name="x", country="Litwa", country_code="LT", feedback_count=1,
                                   feedback_reputation=1.0, stars=5.0, business=False))
    assert "kraj: Litwa (LT)" in offer_text(foreign)
    unknown = laptop(seller=Seller(*[None] * 8))
    assert "kraj: nieznany" in offer_text(unknown)
    guidelines = Path("sniper/guidelines.md").read_text(encoding="utf-8")
    assert "PREMIA ZA POLSKĘ" in guidelines and "+150 zł" in guidelines
    assert "kraj sprzedawcy" in system_text(guidelines)


def test_photo_check_fields(tmp_path, caplog):
    import logging
    seen = {**EVAL_DEAL, "photos_seen": 3, "photo_notes": "Laptop otwarty, naklejka RTX 4060, ekran bez rys."}
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), log_dir=tmp_path, client=FakeClient(response(seen)))
    with caplog.at_level(logging.INFO, logger="sniper.ai"):
        record = run(evaluator.evaluate(laptop()))
    assert record["evaluation"]["photos_seen"] == 3 and record["photos_sent"] == 3
    assert "wysłane 3, AI widzi 3" in caplog.text and "naklejka RTX 4060" in caplog.text
    assert "photos_seen" in EVALUATION_SCHEMA["required"]

    blind = {**EVAL_DEAL, "photos_seen": 0, "photo_notes": ""}
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), client=FakeClient(response(blind)))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sniper.ai"):
        run(evaluator.evaluate(laptop()))
    assert "model ich nie widzi" in caplog.text and "SNIPER_AI_PHOTOS=download" in caplog.text

    msg = build_message(laptop(), "a", "b", ai={"status": STATUS_EVALUATED, "photos_sent": 3, "evaluation": seen})
    assert "widzi 3 z 3: Laptop otwarty" in msg.get_body(("plain",)).get_content()


def test_csv_with_old_header_is_rotated(tmp_path):
    old = tmp_path / "evaluations.csv"
    old.write_text("﻿czas;status;ocena\n2026-10-03;oceniona;1\n", encoding="utf-8")
    evaluator = OfferEvaluator(ai_cfg(tmp_path), FakeNotifier(), log_dir=tmp_path, client=FakeClient(response()))
    run(submit_and_wait(evaluator, laptop()))
    rows = list(csv.reader(old.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
    assert rows[0][-3:] == ["zdjecia_wyslane", "zdjecia_widziane", "co_na_zdjeciach"] and len(rows) == 2
    archived = [p for p in tmp_path.glob("evaluations.*.csv")]
    assert len(archived) == 1 and "2026-10-03;oceniona;1" in archived[0].read_text(encoding="utf-8-sig")
    run(submit_and_wait(evaluator, laptop(id=2)))      # ten sam nagłówek - dopisuje, bez rotacji
    assert len(list(tmp_path.glob("evaluations.*.csv"))) == 1
