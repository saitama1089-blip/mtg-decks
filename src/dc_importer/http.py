"""Bounded HTTP requests with one host-wide spacing budget and robots checks."""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

USER_AGENT = "mtg-decks/0.2 (+https://github.com/saitama1089-blip/mtg-decks)"


class BudgetReached(RuntimeError):
    pass


class SourceUnavailable(RuntimeError):
    pass


class SourceNotFound(ValueError):
    pass


@dataclass
class Page:
    text: str
    url: str


class _Redirects(HTTPRedirectHandler):
    def __init__(self, owner):
        self.owner = owner

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.owner.gate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PoliteHTTP:
    def __init__(self, state: dict, max_requests: int = 150, max_seconds: int = 900):
        self.state = state
        self.state.setdefault("last_request", {})
        self.state.setdefault("cooldown", {})
        self.deadline = time.monotonic() + max_seconds
        self.max_requests = max_requests
        self.requests = 0
        self.by_host: dict[str, int] = {}
        self.robots = None
        self.mtg_interval = 5.0
        self.opener = build_opener(_Redirects(self))

    def check_budget(self):
        if self.requests >= self.max_requests or time.monotonic() >= self.deadline:
            raise BudgetReached("Request or runtime budget reached; remaining work is saved")

    def gate(self, url: str):
        self.check_budget()
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise SourceUnavailable("Unexpected source URL")
        host = parsed.hostname
        if host in {"mtgtop8.com", "www.mtgtop8.com"}:
            host = "mtgtop8.com"
            if parsed.path not in {"/robots.txt", "/format", "/format.php", "/event", "/event.php"}:
                raise SourceUnavailable("Unexpected MTGTop8 route")
            if self.robots and not self.robots.can_fetch(USER_AGENT, url):
                raise SourceUnavailable("MTGTop8 robots directives prohibit this URL")
            interval = self.mtg_interval
        elif host == "api.scryfall.com" and parsed.path == "/cards/collection":
            interval = 0.5
        else:
            raise SourceUnavailable("Unexpected upstream host or route")
        if self.state["cooldown"].get(host, 0) > time.time():
            raise SourceUnavailable(f"{host} is cooling down after an upstream rate limit")
        delay = self.state["last_request"].get(host, 0) + interval - time.time()
        if delay > 0:
            if time.monotonic() + delay >= self.deadline:
                raise BudgetReached("Runtime budget reached before the next permitted request")
            time.sleep(delay)
        self.check_budget()
        self.state["last_request"][host] = time.time()
        self.requests += 1
        self.by_host[host] = self.by_host.get(host, 0) + 1

    def request(self, url: str, payload: dict | None = None) -> tuple[bytes, str, str]:
        for attempt in range(2):
            self.gate(url)
            request = Request(
                url,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/html;q=0.9,text/plain;q=0.8",
                    **({"Content-Type": "application/json"} if payload is not None else {}),
                },
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    data = response.read(10_000_001)
                    if len(data) > 10_000_000:
                        raise SourceUnavailable("Upstream response exceeded the size limit")
                    fallback = "iso-8859-1" if "mtgtop8.com" in url else "utf-8"
                    return data, response.headers.get_content_charset() or fallback, response.url
            except HTTPError as exc:
                host = "mtgtop8.com" if "mtgtop8.com" in url else "api.scryfall.com"
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After", "60")
                    try:
                        seconds = float(retry)
                    except ValueError:
                        try:
                            seconds = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError):
                            seconds = 60
                    self.state["cooldown"][host] = time.time() + max(60, seconds)
                    raise SourceUnavailable(f"{host} returned 429; cooldown saved") from None
                if exc.code in {401, 403}:
                    raise SourceUnavailable(f"{host} blocked access with HTTP {exc.code}") from None
                if exc.code in {404, 410}:
                    raise SourceNotFound(f"Source returned HTTP {exc.code}") from None
                if exc.code >= 500 and attempt == 0:
                    time.sleep(2)
                    continue
                raise SourceUnavailable(f"{host} returned HTTP {exc.code}") from None
            except (URLError, TimeoutError):
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise SourceUnavailable("Upstream connection failed after one retry") from None
        raise SourceUnavailable("Request did not complete")

    def load_robots(self):
        try:
            body, charset, _ = self.request("https://mtgtop8.com/robots.txt")
        except SourceNotFound:
            return  # No published directives; retain the five-second courtesy minimum.
        parser = RobotFileParser()
        parser.parse(body.decode(charset).splitlines())
        self.robots = parser
        self.mtg_interval = max(5, parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0)

    def page(self, url: str) -> Page:
        body, charset, final_url = self.request(url)
        try:
            text = body.decode(charset)
        except (UnicodeError, LookupError) as exc:
            raise SourceUnavailable("Source text could not be decoded") from exc
        if any(marker in text.lower() for marker in ("verify you are human", "cf-chl-")):
            raise SourceUnavailable("Source returned an access challenge")
        return Page(text, final_url)

    def collection(self, names: list[str]) -> dict:
        body, charset, _ = self.request(
            "https://api.scryfall.com/cards/collection",
            {
                "identifiers": [{"name": name} for name in names],
            },
        )
        return json.loads(body.decode(charset))


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
