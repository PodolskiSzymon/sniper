"""Uruchomienie: python -m sniper  (z katalogu głównego repozytorium)."""
import asyncio
import logging
import logging.handlers
from pathlib import Path
from urllib.parse import urlsplit

from .config import ScoutConfig, require_proxy_url
from .notifier import EmailNotifier
from .scout import Scout
from .session import VintedSession


def setup_logging(log_dir):
    """Konsola: INFO. Plik sniper/logs/sniper.log: DEBUG (m.in. pełny JSON złapanych ofert), nowy plik co północ."""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    handlers = [console]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        to_file = logging.handlers.TimedRotatingFileHandler(
            Path(log_dir) / "sniper.log", when="midnight", backupCount=30, encoding="utf-8")
        to_file.setLevel(logging.DEBUG)
        to_file.setFormatter(fmt)
        handlers.append(to_file)
    logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic", "asyncio", "aiosmtplib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _quiet_connection_resets(loop, context):
    """Zerwane połączenie od proxy (np. WinError 10054 na Windowsie) to nie błąd programu.

    Pętla Proactor na Windowsie loguje je jako ERROR z tracebackiem przy zamykaniu gniazda,
    chociaż żądanie i tak jest ponawiane. Zapisujemy je tylko w pliku logu (DEBUG).
    """
    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        logging.getLogger("sniper.net").debug("[NET] Zdalna strona zerwała połączenie: %r", exc)
        return
    loop.default_exception_handler(context)


def build_evaluator(cfg, notifier):
    """Moduł oceny AI albo None (wtedy mail o każdej ofercie, jak dotąd)."""
    log = logging.getLogger("sniper")
    if not cfg.ai.enabled:
        log.info("[AI] Ocena AI wyłączona (SNIPER_AI_ENABLED=false) - mail o każdej ofercie.")
        return None
    if not cfg.ai.api_key:
        log.warning("[AI] Brak klucza SNIPER_AI_API_KEY (GEMINI_API_KEY / ANTHROPIC_API_KEY) - ocena AI wyłączona, "
                    "mail o każdej ofercie.")
        return None
    try:
        from .evaluator import OfferEvaluator
    except ImportError as exc:
        log.error("[AI] Brak biblioteki AI (%s) - uruchom: pip install -r sniper/requirements.txt. "
                  "Ocena AI wyłączona, mail o każdej ofercie.", exc)
        return None
    try:
        evaluator = OfferEvaluator(cfg.ai, notifier, log_dir=cfg.log_dir)
    except (ImportError, ValueError) as exc:
        log.error("[AI] Nie mogę uruchomić oceny AI: %s - pip install -r sniper/requirements.txt / sprawdź "
                  "SNIPER_AI_PROVIDER. Mail o każdej ofercie.", exc)
        return None
    evaluator.check_guidelines()
    log.info("[AI] Ocena AI włączona: %s", evaluator.describe())
    return evaluator


async def main():
    asyncio.get_running_loop().set_exception_handler(_quiet_connection_resets)
    cfg = ScoutConfig()
    setup_logging(cfg.log_dir)
    logging.getLogger("sniper").info("Logi zapisuję do: %s", Path(cfg.log_dir).resolve())
    require_proxy_url()  # bez proxy Zwiadowca w ogóle nie startuje (SNIPER_REQUIRE_PROXY)
    proxy = urlsplit(cfg.proxy_url)
    logging.getLogger("sniper").info(
        "Proxy: %s:%s | login: %s | hasło: %s", proxy.hostname, proxy.port,
        "TAK" if proxy.username else "BRAK", "TAK" if proxy.password else "BRAK")

    session = VintedSession(
        proxy_url=cfg.proxy_url,
        timeout=cfg.request_timeout,
        browser_wait_ms=cfg.browser_wait_ms,
        refresh_attempts=cfg.refresh_attempts,
        refresh_retry_delay=cfg.refresh_retry_delay,
        refresh_timeout=cfg.refresh_timeout,
        browser_light=cfg.browser_light,
        state_file=Path(cfg.log_dir) / "session.json" if cfg.log_dir else None,
    )
    notifier = EmailNotifier(cfg.smtp)
    evaluator = build_evaluator(cfg, notifier)
    scout = Scout(cfg, session, notifier, evaluator)
    try:
        await scout.run()
    finally:
        await scout.shutdown()
        if evaluator:
            await evaluator.drain()      # oceny w locie -> ich maile trafiają do notifier
            await evaluator.close()
        await notifier.drain()
        await session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("sniper").info("=== ZWIADOWCA ZATRZYMANY RĘCZNIE ===")
    except Exception:
        # Pełny traceback trafia też do pliku logu, nie tylko na konsolę.
        logging.getLogger("sniper").exception("=== ZWIADOWCA PRZERWANY BŁĘDEM ===")
        raise
