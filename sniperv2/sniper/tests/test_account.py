"""Testy sprawdzania sesji konta (sniper/account.py) - bez sieci i bez prawdziwych ciastek."""
import httpx
import pytest

from sniper import account
from sniper.config import BASE_HEADERS

CURL = r'''curl 'https://api.vinted.pl/svc-catalogue/items?page=1' \
  -H 'accept: application/json' \
  -H 'accept-language: pl,en;q=0.9' \
  -b 'anon_id=abc; access_token_web=TAJNE.TOKEN.XYZ; cf_clearance=zzz' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0) Edg/154.0' \
  -H 'x-csrf-token: csrf-123' \
  -H 'x-anon-id: anon-xyz' \
  -H 'sec-fetch-site: same-site' '''

BLOCK = """\
cookie
anon_id=abc; access_token_web=TAJNE.TOKEN.XYZ; cf_clearance=zzz
user-agent
Mozilla/5.0 (Windows NT 10.0) Edg/154.0
x-csrf-token
csrf-123
accept-language: pl,en;q=0.9
"""


def test_parse_curl_extracts_cookie_and_carried_headers():
    h = account.parse_curl(CURL)
    assert "access_token_web=TAJNE.TOKEN.XYZ" in h["cookie"]
    assert h["x-csrf-token"] == "csrf-123" and h["x-anon-id"] == "anon-xyz"
    assert h["user-agent"].endswith("Edg/154.0")


def test_parse_devtools_block_two_line_format():
    h = account.parse_block(BLOCK)
    assert "access_token_web=TAJNE.TOKEN.XYZ" in h["cookie"]
    assert h["x-csrf-token"] == "csrf-123" and h["accept-language"] == "pl,en;q=0.9"


def test_read_headers_keeps_only_carried(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text(CURL, encoding="utf-8")
    h = account.read_headers(f)
    assert set(h) <= set(account.CARRY) and "sec-fetch-site" not in h   # odsiane
    assert "cookie" in h


def test_build_headers_overlays_cookie_and_ua():
    headers = account.build_headers({"cookie": "x=1", "user-agent": "UA/1", "x-csrf-token": "c"})
    assert headers["cookie"] == "x=1" and headers["user-agent"] == "UA/1"
    assert headers["x-next-app"] == BASE_HEADERS["x-next-app"]      # reszta z BASE_HEADERS


def test_build_headers_requires_cookie():
    with pytest.raises(ValueError, match="cookie"):
        account.build_headers({"user-agent": "UA/1"})


@pytest.mark.parametrize("html,expected,fragment", [
    ('window.__data={"user":{"id":123456789,"login":"szymon_k"}}', True, "szymon_k"),
    ('{"current_user_id":123456789,"username":"flipper99"}', True, "flipper99"),
    ('{"is_anon_user":true,"user":null}', False, "ANONIMOWY"),
    ('<a href="/member/123456789">Profil</a><button>Wyloguj</button>', True, "wylogowania"),
    ('<html>jakis marketing bez danych</html>', None, "sprawdź zapisany HTML"),
])
def test_detect_login(html, expected, fragment):
    logged, detail = account.detect_login(html)
    assert logged is expected and fragment in detail


def test_detect_login_prefers_named_user_over_anon():
    # gdy w HTML jest i flaga anon (inny widget) i nazwa konta - traktujemy jako zalogowany
    logged, _ = account.detect_login('{"is_anon_user":true} ... "login":"szymon_k"')
    assert logged is True


BANNERS_JSON = ('{"banners":{"promotional_banner":{"actions":{"primary":{"action":{"extra":'
                '{"invite_url":"https://www.vinted.pl/invite/szymon_k/abc123",'
                '"subject":"Join szymon_k on Vinted"}}}}}},"code":0}')


def _mock_client(monkeypatch, handler):
    real_client = httpx.Client

    def fake_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(account.httpx, "Client", fake_client)


def test_check_uses_banners_endpoint_and_reads_username(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")   # musi być zignorowane (trust_env=False)
    seen = {}

    def handler(request):
        seen.setdefault("urls", []).append(str(request.url))
        seen["cookie"] = request.headers.get("cookie")
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text=BANNERS_JSON)

    _mock_client(monkeypatch, handler)
    f = tmp_path / "my_headers.txt"
    f.write_text(CURL, encoding="utf-8")
    status, logged, detail, out_path, size = account.check(f, tmp_path)

    assert status == 200 and logged is True and "szymon_k" in detail
    assert seen["urls"] == [account.BANNERS_URL]              # strona główna niepotrzebna
    assert "access_token_web=TAJNE.TOKEN.XYZ" in seen["cookie"] and seen["ua"].endswith("Edg/154.0")
    assert out_path is None


def test_check_401_means_session_expired(tmp_path, monkeypatch):
    _mock_client(monkeypatch, lambda r: httpx.Response(401, text='{"code":100}'))
    f = tmp_path / "my_headers.txt"
    f.write_text(CURL, encoding="utf-8")
    status, logged, detail, *_ = account.check(f, tmp_path)
    assert status == 401 and logged is False and "wygasła" in detail


def test_check_falls_back_to_home_when_no_invite_banner(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path == "/api/v2/banners":
            return httpx.Response(200, text='{"banners":{},"code":0}')     # brak banera polecającego
        return httpx.Response(200, text='<a href="/member/1">x</a><button>Wyloguj</button>')

    _mock_client(monkeypatch, handler)
    f = tmp_path / "my_headers.txt"
    f.write_text(CURL, encoding="utf-8")
    status, logged, detail, out_path, size = account.check(f, tmp_path)
    assert logged is True and "wylogowania" in detail and (tmp_path / "account_check.html").exists()


def test_detect_banners_extracts_name():
    assert account.detect_banners('x "/invite/koala_test/tok" y')[1] == "koala_test"
    assert account.detect_banners('"subject":"Join flipper99 on Vinted"')[1] == "flipper99"
    assert account.detect_banners('{"banners":{},"code":0}') == (None, None)


def test_main_without_file_explains(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(account.ScoutConfig, "log_dir", str(tmp_path / "empty"))
    code = account.main([str(tmp_path / "nie_ma.txt")])
    assert code == 2 and "Brak pliku" in capsys.readouterr().out


@pytest.mark.parametrize("html,fragment", [
    (r'{\"login\":\"szymon_k\",\"id\":123456789}', "szymon_k"),         # ekranowane cudzysłowy (Next.js)
    ('{&quot;username&quot;:&quot;flipper99&quot;}', "flipper99"),       # HTML-owe &quot;
])
def test_detect_login_handles_escaped_json(html, fragment):
    logged, detail = account.detect_login(html)
    assert logged is True and fragment in detail


def test_find_in_saved_returns_snippets(tmp_path):
    (tmp_path / "account_check.html").write_text(
        'x' * 200 + 'blabla"login":"szymon_k","id":123' + 'y' * 200, encoding="utf-8")
    path, snippets = account.find_in_saved(tmp_path, "szymon_k", window=20)
    assert path is not None and len(snippets) == 1 and "szymon_k" in snippets[0]
    assert len(snippets[0]) < 80                        # krótki fragment, nie cały plik
    assert account.find_in_saved(tmp_path, "nie_ma_tego")[1] == []


def test_find_in_saved_missing_file(tmp_path):
    assert account.find_in_saved(tmp_path, "cokolwiek") == (None, [])
