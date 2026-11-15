"""URL policy and guarded HTTP fetcher for the documentation server.

The documentation server may only read pages under configured URL prefixes.
Everything here exists so that a model (or text injected into a page or an
issue) cannot turn "read the docs" into general web access:

- https only, default port, no user:password@, no IP-address hosts, no query
  strings, no secrets in the URL, no login or account paths;
- every host must resolve to public addresses only (no localhost, private
  ranges or cloud metadata endpoints);
- redirects are followed by hand, and every hop is checked again;
- GET only, no cookies stored or sent, no Authorization header, and no
  credentials or proxies taken from the environment (``trust_env=False``);
- response size, content type, timeout and number of requests are capped;
- robots.txt is honored.
"""

from __future__ import annotations

import http.cookiejar
import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx2

from .security import redact

USER_AGENT = "mcp-coding-agent-docs/0.3 (read-only documentation reader)"
MAX_URL_LENGTH = 2048
REDIRECT_CODES = {301, 302, 303, 307, 308}

PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/x-markdown", "text/x-rst")
INDEX_TYPES = PAGE_TYPES + ("application/json", "application/javascript", "text/javascript", "application/x-javascript")

# Path segments that belong to login or account flows. Reading them never
# helps with documentation, so they are refused even under an allowed prefix.
BLOCKED_PATH_SEGMENTS = frozenset(
    {
        "login", "log-in", "logout", "log-out", "signin", "sign-in", "signout", "sign-out",
        "signup", "sign-up", "register", "oauth", "oauth2", "authorize", "sso",
        "account", "accounts", "checkout", "cart", "billing", "password",
    }
)
_PAGE_EXTENSION = re.compile(r"\.(html?|php|aspx?|jsp|md)$")

Resolver = Callable[[str], list[str]]


class URLRejected(Exception):
    """The URL is outside the documentation policy. The message says why."""


class FetchError(Exception):
    """The request was allowed but did not produce a usable response.

    ``temporary`` marks failures that may go away by themselves (timeouts,
    network errors, 429, 5xx); ``retry_after`` is the server's hint, if any.
    """

    def __init__(self, message: str, *, temporary: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.temporary = temporary
        self.retry_after = retry_after


class BudgetExhausted(FetchError):
    """The per-session request budget is used up."""


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


@dataclass(frozen=True)
class ParsedURL:
    url: str  # normalized, without fragment
    host: str
    path: str  # percent-decoded path, used for matching
    fragment: str


def parse_url(url: str) -> ParsedURL:
    """Apply the rules that do not depend on configuration."""
    if not isinstance(url, str) or not url.strip():
        raise URLRejected("the URL is empty")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise URLRejected(f"the URL is longer than {MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
        raise URLRejected("the URL contains whitespace, control characters or backslashes")
    if redact(url) != url:
        raise URLRejected("the URL appears to contain a secret (API key or token), so it will not be sent anywhere")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise URLRejected(f"only https URLs are allowed (got {parts.scheme or 'no scheme'!r})")
    if "@" in parts.netloc:
        raise URLRejected("URLs with a user name or password are not allowed")
    try:
        port = parts.port
    except ValueError:
        raise URLRejected("the URL has an invalid port") from None
    if port not in (None, 443):
        raise URLRejected("only the default https port (443) is allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise URLRejected("the URL has no host")
    if _is_ip_literal(host):
        raise URLRejected("IP-address hosts are not allowed; use the documentation site's host name")
    if parts.query:
        raise URLRejected("query strings are not allowed; documentation pages are addressed by path")
    raw_path = parts.path or "/"
    if re.search(r"%(2f|5c|00)", raw_path, re.IGNORECASE):
        raise URLRejected("encoded slashes, backslashes or NUL bytes are not allowed in the path")
    path = unquote(raw_path)
    segments = path.split("/")
    if any(seg in (".", "..") for seg in segments):
        raise URLRejected("'.' and '..' path segments are not allowed")
    for seg in segments:
        if _PAGE_EXTENSION.sub("", seg.lower()) in BLOCKED_PATH_SEGMENTS:
            raise URLRejected(f"'{seg}' looks like a login or account page; the documentation reader does not visit those")
    return ParsedURL(urlunsplit(("https", host, raw_path, "", "")), host, path, parts.fragment)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class AllowedPrefix:
    host: str
    path: str

    @classmethod
    def parse(cls, url: str) -> AllowedPrefix:
        parsed = parse_url(url)
        return cls(parsed.host, parsed.path)

    def matches(self, host: str, path: str) -> bool:
        if host != self.host:
            return False
        if self.path.endswith("/"):
            return path.startswith(self.path) or path + "/" == self.path
        return path == self.path or path.startswith(self.path + "/")

    def __str__(self) -> str:
        return f"https://{self.host}{self.path}"


class URLPolicy:
    """Which URLs may be read: anything under ``prefixes``, plus ``exact`` URLs (search indexes)."""

    def __init__(self, prefixes: list[str], exact: list[str] = ()) -> None:
        self.prefixes = [AllowedPrefix.parse(p) for p in prefixes]
        self.exact = {parse_url(u).url for u in exact}
        self.hosts = {p.host for p in self.prefixes} | {urlsplit(u).hostname for u in self.exact}

    def check(self, url: str) -> ParsedURL:
        parsed = parse_url(url)
        if parsed.url in self.exact or any(p.matches(parsed.host, parsed.path) for p in self.prefixes):
            return parsed
        allowed = ", ".join(str(p) for p in self.prefixes)
        raise URLRejected(f"{parsed.url} is not inside an allowed documentation source. Allowed prefixes: {allowed}")

    def allows(self, url: str) -> bool:
        try:
            self.check(url)
            return True
        except URLRejected:
            return False


def check_public_host(host: str, resolver: Resolver) -> None:
    try:
        addresses = resolver(host)
    except OSError as exc:
        raise FetchError(f"could not resolve {host}: {exc}", temporary=True) from exc
    if not addresses:
        raise FetchError(f"could not resolve {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise URLRejected(f"{host} resolves to a non-public address ({ip}); private and local networks are not reachable")


@dataclass
class FetchedResponse:
    url: str  # final URL after redirects
    status: int
    content_type: str
    text: str
    size: int
    redirects: list[str]


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False


class GuardedFetcher:
    def __init__(
        self,
        policy: URLPolicy,
        *,
        timeout_seconds: float = 15.0,
        max_redirects: int = 3,
        max_fetches: int = 40,
        respect_robots: bool = True,
        resolver: Resolver | None = None,
        transport: Any = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.policy = policy
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self.max_fetches = max_fetches
        self.respect_robots = respect_robots
        self.resolver = resolver or system_resolver
        self.user_agent = user_agent
        self.fetches = 0
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = httpx2.Client(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,  # no .netrc credentials, no proxy variables
            cookies=http.cookiejar.CookieJar(policy=_NoCookies()),
            headers={"User-Agent": user_agent, "Accept": "text/html, text/markdown;q=0.9, text/plain;q=0.8, */*;q=0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str, *, max_bytes: int, accept_types: tuple[str, ...] = PAGE_TYPES) -> FetchedResponse:
        current = url
        redirects: list[str] = []
        for _ in range(self.max_redirects + 1):
            parsed = self.policy.check(current)
            check_public_host(parsed.host, self.resolver)
            if self.respect_robots and not self._robots_allow(parsed):
                raise URLRejected(f"robots.txt on {parsed.host} does not allow automated reading of {parsed.path}")
            outcome = self._get(parsed.url, max_bytes=max_bytes, accept_types=accept_types)
            if isinstance(outcome, str):  # redirect target
                redirects.append(parsed.url)
                current = outcome
                continue
            outcome.redirects = redirects
            return outcome
        raise FetchError(f"too many redirects (more than {self.max_redirects}) starting at {url}")

    # ------------------------------------------------------------------ internals
    def _spend(self) -> None:
        if self.fetches >= self.max_fetches:
            raise BudgetExhausted(f"the documentation request budget ({self.max_fetches} requests) is used up; work with what you have already read")
        self.fetches += 1

    def _get(self, url: str, *, max_bytes: int, accept_types: tuple[str, ...]) -> FetchedResponse | str:
        self._spend()
        self._client.cookies.clear()
        host = urlsplit(url).hostname
        try:
            with self._client.stream("GET", url) as response:
                status = response.status_code
                if status in REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"HTTP {status} redirect from {host} without a Location header")
                    return urljoin(url, location)
                if status == 404:
                    raise FetchError(f"HTTP 404: {url} was not found")
                if status == 429:
                    retry_after = response.headers.get("retry-after", "")
                    raise FetchError(
                        f"HTTP 429: {host} is rate limiting requests; try again later",
                        temporary=True,
                        retry_after=float(retry_after) if retry_after.isdigit() else None,
                    )
                if status >= 400:
                    raise FetchError(f"HTTP {status} from {host} for {url}", temporary=status >= 500)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in accept_types:
                    raise FetchError(f"unsupported content type {content_type or 'unknown'!r} at {url}; only text documentation is read")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"{url} is {int(declared)} bytes, more than the {max_bytes}-byte limit")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(f"{url} is larger than the {max_bytes}-byte limit; reading stopped")
                    chunks.append(chunk)
                encoding = response.charset_encoding or "utf-8"
        except httpx2.TimeoutException as exc:
            raise FetchError(f"timed out after {self.timeout_seconds:g}s fetching {url}", temporary=True) from exc
        except httpx2.HTTPError as exc:
            raise FetchError(f"network error fetching {url}: {type(exc).__name__}", temporary=True) from exc
        body = b"".join(chunks)
        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return FetchedResponse(url, status, content_type, text, total, [])

    def _robots_allow(self, parsed: ParsedURL) -> bool:
        if parsed.host not in self._robots:
            self._robots[parsed.host] = self._load_robots(parsed.host)
        robots = self._robots[parsed.host]
        return robots is None or robots.can_fetch(self.user_agent, parsed.url)

    def _load_robots(self, host: str) -> RobotFileParser | None:
        """robots.txt for ``host``; None means no usable file (everything allowed)."""
        try:
            outcome = self._get(f"https://{host}/robots.txt", max_bytes=500_000, accept_types=("text/plain",))
        except BudgetExhausted:
            raise
        except FetchError:
            return None  # missing, HTML error page, unreachable: no rules to apply
        if isinstance(outcome, str):
            return None
        parser = RobotFileParser()
        parser.parse(outcome.text.splitlines())
        return parser
