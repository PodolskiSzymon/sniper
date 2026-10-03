"""Lokalny przekaźnik proxy dla Playwrighta.

Chromium nie potrafi wysłać loginu/hasła do proxy przy tunelowaniu HTTPS
(błąd net::ERR_PROXY_AUTH_UNSUPPORTED). Dlatego przeglądarka łączy się z
lokalnym proxy bez hasła na 127.0.0.1, a ten przekaźnik dokleja nagłówek
Proxy-Authorization i przekazuje ruch do właściwego proxy (np. IPRoyal).
"""
import asyncio
import base64
import logging
from urllib.parse import unquote, urlsplit

log = logging.getLogger("sniper.proxy_relay")

_HEADER_LIMIT = 64 * 1024

PROXY_AUTH_HELP = (
    "IPRoyal odrzucił dane logowania (407 Proxy Authentication Required). Sprawdź: "
    "1) saldo transferu / ważność planu w panelu IPRoyal, "
    "2) SNIPER_PROXY_AUTH (LOGIN:HASLO_country-pl) w sniper/.env - literówka albo nowe hasło, "
    "3) ograniczenia dostępu (whitelist IP) w ustawieniach IPRoyal."
)


class ProxyAuthRejected(Exception):
    """Proxy odpowiedziało 407 - złe dane logowania albo konto bez transferu."""


async def _pipe(reader, writer, count=None):
    try:
        while data := await reader.read(65536):
            if count:
                count(len(data))
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class ProxyRelay:
    """Użycie: `async with ProxyRelay(url) as relay: ... relay.server ...`"""

    def __init__(self, upstream_url, on_bytes=None):
        """on_bytes(sent, received) - opcjonalny licznik bajtów przechodzących przez przekaźnik."""
        self._on_bytes = on_bytes
        parts = urlsplit(upstream_url)
        self._host = parts.hostname
        self._port = parts.port or 80
        credentials = f"{unquote(parts.username or '')}:{unquote(parts.password or '')}"
        self._auth = b"Proxy-Authorization: Basic " + base64.b64encode(credentials.encode()) + b"\r\n"
        self._server = None
        self._connections = set()
        self.auth_rejected = False     # True, jeśli upstream odpowiedział 407

    @property
    def server(self):
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        await self._server.wait_closed()

    def _count_sent(self, n):
        if self._on_bytes:
            self._on_bytes(n, 0)

    def _count_received(self, n):
        if self._on_bytes:
            self._on_bytes(0, n)

    async def _handle(self, client_reader, client_writer):
        task = asyncio.current_task()
        self._connections.add(task)
        upstream_writer = None
        try:
            head = await client_reader.readuntil(b"\r\n\r\n")
            if len(head) > _HEADER_LIMIT:
                return
            # Usuwamy ewentualny Proxy-Authorization od przeglądarki i wstawiamy własny.
            lines = head.split(b"\r\n")
            kept = [l for l in lines[1:] if l and not l.lower().startswith(b"proxy-authorization:")]
            new_head = lines[0] + b"\r\n" + b"".join(l + b"\r\n" for l in kept) + self._auth + b"\r\n"

            upstream_reader, upstream_writer = await asyncio.open_connection(self._host, self._port)
            self._count_sent(len(new_head))
            upstream_writer.write(new_head)
            await upstream_writer.drain()

            # Odpowiedź proxy czytamy sami, żeby rozpoznać 407 (Chromium pokazuje wtedy tylko
            # mylące net::ERR_PROXY_AUTH_UNSUPPORTED), a potem przekazujemy ją przeglądarce bez zmian.
            reply = await upstream_reader.readuntil(b"\r\n\r\n")
            status = reply.split(b" ", 2)[1:2]
            if status == [b"407"] and not self.auth_rejected:
                self.auth_rejected = True
                log.error("[RELAY] %s", PROXY_AUTH_HELP)
            self._count_received(len(reply))
            client_writer.write(reply)
            await client_writer.drain()

            await asyncio.gather(
                _pipe(client_reader, upstream_writer, self._count_sent),
                _pipe(upstream_reader, client_writer, self._count_received),
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError) as exc:
            log.debug("[RELAY] Połączenie przerwane: %r", exc)
        finally:
            for w in (client_writer, upstream_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass
            self._connections.discard(task)

