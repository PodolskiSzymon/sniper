"""Testy sesji konta (sniper/account_session.py) - części bez przeglądarki (konwersja ciastek, konfiguracja)."""
import pytest

from sniper import account_session as acc
from sniper.config import AccountConfig, ScoutConfig


def test_cookie_header_to_playwright():
    cookies = acc.cookie_header_to_playwright("a=1; access_token_web=TOK.EN; b=2 ")
    assert cookies == [
        {"name": "a", "value": "1", "domain": ".vinted.pl", "path": "/"},
        {"name": "access_token_web", "value": "TOK.EN", "domain": ".vinted.pl", "path": "/"},
        {"name": "b", "value": "2", "domain": ".vinted.pl", "path": "/"},
    ]
    assert acc.cookie_header_to_playwright("") == []
    assert acc.cookie_header_to_playwright("smieci_bez_rowna") == []


def test_load_account_cookies(tmp_path):
    f = tmp_path / "my_headers.txt"
    f.write_text("curl 'https://www.vinted.pl/' -b 'anon_id=abc; access_token_web=T.O.K' "
                 "-H 'user-agent: Edg/154'", encoding="utf-8")
    cookies, ua = acc.load_account_cookies(f)
    names = {c["name"] for c in cookies}
    assert names == {"anon_id", "access_token_web"} and ua == "Edg/154"


def test_load_account_cookies_requires_cookie(tmp_path):
    f = tmp_path / "my_headers.txt"
    f.write_text("curl 'https://www.vinted.pl/' -H 'user-agent: Edg/154'", encoding="utf-8")
    with pytest.raises(ValueError, match="Brak ciastek"):
        acc.load_account_cookies(f)


def test_account_paths_default_to_log_dir(tmp_path):
    cfg = AccountConfig()
    account = acc.VintedAccount(cfg, tmp_path)
    assert account.headers_file == tmp_path / "my_headers.txt"
    assert account.profile_dir == tmp_path / "account_profile"


def test_account_paths_from_config(tmp_path):
    cfg = AccountConfig(headers_file=str(tmp_path / "h.txt"), profile_dir=str(tmp_path / "prof"))
    account = acc.VintedAccount(cfg, tmp_path / "logs")
    assert account.headers_file == tmp_path / "h.txt" and account.profile_dir == tmp_path / "prof"


def test_account_disabled_by_default():
    assert ScoutConfig().account.enabled is False


class FakePage:
    def __init__(self, banners_result):
        self._banners = banners_result
        self.goto_urls = []

    async def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        return None

    async def evaluate(self, js, arg=None):
        return self._banners

    @property
    def url(self):
        return self.goto_urls[-1] if self.goto_urls else ""


def _account_with_page(tmp_path, banners_result):
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = FakePage(banners_result)
    return account


def test_refresh_and_check_reads_username(tmp_path):
    import asyncio
    body = '{"banners":{"x":{"extra":{"invite_url":"https://www.vinted.pl/invite/koala_test/tok"}}},"code":0}'
    account = _account_with_page(tmp_path, {"status": 200, "body": body})
    assert asyncio.run(account.refresh_and_check()) is True
    assert account.username == "koala_test" and acc.HOME_URL in account.page.goto_urls


def test_refresh_and_check_detects_expired(tmp_path):
    import asyncio
    account = _account_with_page(tmp_path, {"status": 401, "body": '{"code":100}'})
    assert asyncio.run(account.refresh_and_check()) is False and account.username is None


def test_refresh_and_check_logged_in_without_banner(tmp_path):
    import asyncio
    account = _account_with_page(tmp_path, {"status": 200, "body": '{"banners":{},"code":0}'})
    assert asyncio.run(account.refresh_and_check()) is True      # code:0 = sesja aktywna, choć bez nazwy


def test_open_item_navigates_without_buying(tmp_path):
    import asyncio
    account = _account_with_page(tmp_path, {"status": 200, "body": "{}"})
    url = "https://www.vinted.pl/items/123-laptop"
    assert asyncio.run(account.open_item(url)) == url and account.page.goto_urls == [url]


def test_is_session_refresh_detects_loop():
    assert acc.VintedAccount.is_session_refresh("https://www.vinted.pl/session-refresh?ref_url=%2F") is True
    assert acc.VintedAccount.is_session_refresh("https://www.vinted.pl/") is False
    assert acc.VintedAccount.is_session_refresh("") is False


def test_reset_profile_removes_dir(tmp_path):
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.profile_dir.mkdir(parents=True)
    (account.profile_dir / "Cookies").write_text("stare", encoding="utf-8")
    account.reset_profile()
    assert not account.profile_dir.exists()


def test_stuck_on_session_refresh_when_not_refresh(tmp_path):
    import asyncio
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.page = FakePage({"status": 200, "body": "{}"})
    account.page.goto_urls.append("https://www.vinted.pl/")      # nie jest to session-refresh
    assert asyncio.run(account._stuck_on_session_refresh()) is False


def test_clear_profile_locks_removes_only_locks(tmp_path):
    account = acc.VintedAccount(AccountConfig(), tmp_path)
    account.profile_dir.mkdir(parents=True)
    for name in ("SingletonLock", "lockfile", "Cookies"):
        (account.profile_dir / name).write_text("x", encoding="utf-8")
    account.clear_profile_locks()
    assert not (account.profile_dir / "SingletonLock").exists()
    assert not (account.profile_dir / "lockfile").exists()
    assert (account.profile_dir / "Cookies").exists()      # ciastka (logowanie) zostają


def test_is_checkout_url():
    assert acc.VintedAccount._is_checkout_url("https://www.vinted.pl/api/v2/purchases/abc/checkout") is True
    assert acc.VintedAccount._is_checkout_url("https://www.vinted.pl/api/v2/purchases/abc/checkout?x=1") is True
    assert acc.VintedAccount._is_checkout_url("https://www.vinted.pl/api/v2/items/123") is False
    assert acc.VintedAccount._is_checkout_url("") is False
