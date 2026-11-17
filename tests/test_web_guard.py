"""Network rules for the docs reader: URL policy and the guarded fetcher (mock transport, no network)."""

from __future__ import annotations

import httpx2
import pytest

from app.mcp.web_guard import PAGE_TYPES, BudgetExhausted, FetchError, GuardedFetcher, URLPolicy, URLRejected, check_public_host, parse_url
from tests.docs_site import FakeDocsWeb, fake_sources

POLICY = URLPolicy(
    ["https://docs.python.org/3/", "https://www.rfc-editor.org/rfc/", "https://example.org/llms.txt"],
    exact=["https://docs.python.org/3/searchindex.js"],
)


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.python.org/3/",
        "https://docs.python.org/3/library/secrets.html",
        "https://docs.python.org/3/library/secrets.html#secrets.compare_digest",
        "https://DOCS.python.org/3/library/",
        "https://docs.python.org:443/3/library/",
        "https://docs.python.org/3",
        "https://www.rfc-editor.org/rfc/rfc9700.html",
        "https://example.org/llms.txt",
    ],
)
def test_allowed_urls(url: str) -> None:
    assert POLICY.allows(url)


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://docs.python.org/3/", "only https"),
        ("ftp://docs.python.org/3/", "only https"),
        ("file:///etc/passwd", "only https"),
        ("javascript:alert(1)", "only https"),
        ("https://docs.python.org:8443/3/", "default https port"),
        ("https://user:pw@docs.python.org/3/", "user name or password"),
        ("https://docs.python.org@evil.com/3/", "user name or password"),
        ("https://127.0.0.1/3/", "IP-address hosts"),
        ("https://[::1]/3/", "IP-address hosts"),
        ("https://169.254.169.254/latest/meta-data/", "IP-address hosts"),
        ("https://docs.python.org.evil.com/3/", "not inside an allowed"),
        ("https://evil-docs.python.org/3/", "not inside an allowed"),
        ("https://python.org/3/", "not inside an allowed"),
        ("https://docs.python.org/2/library/", "not inside an allowed"),
        ("https://docs.python.org/3evil/", "not inside an allowed"),
        ("https://docs.python.org/3/../2/", "'..' path segments"),
        ("https://docs.python.org/3/%2e%2e/2/", "'..' path segments"),
        ("https://docs.python.org/3/..%2f2/", "encoded slashes"),
        ("https://docs.python.org/3/a%5cb", "encoded slashes"),
        ("https://docs.python.org/3\\..\\2/", "backslashes"),
        ("https://docs.python.org/3/search.html?q=x", "query strings"),
        ("https://docs.python.org/3/login", "login or account"),
        ("https://docs.python.org/3/accounts/settings", "login or account"),
        ("https://docs.python.org/3/oauth/authorize", "login or account"),
        ("https://docs.python.org/3/sign-in.html", "login or account"),
        ("https://docs.python.org/3/sk-ant-api03-" + "a" * 40 + ".html", "contain a secret"),
        ("https://docs.python.org/3/ghp_" + "b" * 36, "contain a secret"),
        ("https://docs.python.org/3/x y", "whitespace"),
        ("https://docs.python.org/3/" + "a" * 3000, "longer than"),
        ("", "empty"),
    ],
)
def test_rejected_urls_say_why(url: str, reason: str) -> None:
    with pytest.raises(URLRejected, match=reason):
        POLICY.check(url)


def test_exact_index_url_does_not_open_its_directory() -> None:
    policy = URLPolicy(["https://docs.authkit.test/en/"], exact=["https://index.authkit.test/search/index.json"])
    assert policy.allows("https://index.authkit.test/search/index.json")
    assert not policy.allows("https://index.authkit.test/search/other.json")


def test_fragment_is_kept_but_not_sent() -> None:
    parsed = parse_url("https://docs.python.org/3/library/secrets.html#secrets.compare_digest")
    assert parsed.url == "https://docs.python.org/3/library/secrets.html" and parsed.fragment == "secrets.compare_digest"


@pytest.mark.parametrize("addresses", [["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], ["192.168.1.1"], ["::1"], ["fd00::1"], ["::ffff:10.0.0.1"], ["100.64.0.1"], ["0.0.0.0"], ["93.184.215.14", "127.0.0.1"]])
def test_private_addresses_are_blocked(addresses: list[str]) -> None:
    with pytest.raises(URLRejected, match="non-public address"):
        check_public_host("docs.example.org", lambda host: addresses)


def test_public_address_passes_and_resolution_failure_is_reported() -> None:
    check_public_host("docs.example.org", lambda host: ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"])

    def fail(host: str) -> list[str]:
        raise OSError("nodename nor servname provided")

    with pytest.raises(FetchError, match="could not resolve"):
        check_public_host("docs.example.org", fail)


# ---------------------------------------------------------------------- fetcher
def make_fetcher(web: FakeDocsWeb, **kwargs) -> GuardedFetcher:
    sources = fake_sources()
    policy = URLPolicy([s.base_url for s in sources], [s.index_url for s in sources if s.index_url])
    return GuardedFetcher(policy, resolver=web.resolver, transport=web.transport(), **kwargs)


def fetch(fetcher: GuardedFetcher, url: str, max_bytes: int = 4000):
    return fetcher.fetch(url, max_bytes=max_bytes, accept_types=PAGE_TYPES)


def test_fetch_page_with_get_only_and_no_credentials() -> None:
    web = FakeDocsWeb()
    response = fetch(make_fetcher(web), "https://docs.authkit.test/en/tokens.html")
    assert response.status == 200 and response.content_type == "text/html" and "Refresh token rotation" in response.text
    assert [str(r.url) for r in web.requests] == ["https://docs.authkit.test/robots.txt", "https://docs.authkit.test/en/tokens.html"]
    for request in web.requests:
        assert request.method == "GET"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.headers["user-agent"].startswith("mcp-coding-agent-docs/")


def test_cookies_are_never_stored_or_sent() -> None:
    web = FakeDocsWeb()
    fetcher = make_fetcher(web)
    response = fetch(fetcher, "https://docs.authkit.test/en/cookie.html")  # Set-Cookie + redirect
    assert response.url == "https://docs.authkit.test/en/tokens.html"
    fetch(fetcher, "https://docs.authkit.test/en/api.html")
    assert all("cookie" not in r.headers for r in web.requests)
    assert len(fetcher._client.cookies) == 0


def test_credentials_in_environment_are_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine docs.authkit.test login alice password hunter2\n")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    web = FakeDocsWeb()
    fetch(make_fetcher(web), "https://docs.authkit.test/en/tokens.html")
    assert all("authorization" not in r.headers and "proxy-authorization" not in r.headers for r in web.requests)


def test_redirects_are_followed_and_rechecked() -> None:
    web = FakeDocsWeb()
    fetcher = make_fetcher(web)
    response = fetch(fetcher, "https://docs.authkit.test/en/old.html")
    assert response.url == "https://docs.authkit.test/en/tokens.html" and response.redirects == ["https://docs.authkit.test/en/old.html"]
    with pytest.raises(URLRejected, match="not inside an allowed documentation source"):
        fetch(fetcher, "https://docs.authkit.test/en/escape.html")
    with pytest.raises(URLRejected, match="non-public address"):
        fetch(fetcher, "https://docs.authkit.test/en/to-intranet.html")
    with pytest.raises(FetchError, match="too many redirects"):
        fetch(fetcher, "https://docs.authkit.test/en/loop.html")
    assert not any(r.url.host in ("evil.example.net", "intranet.authkit.test") for r in web.requests)


@pytest.mark.parametrize("url", ["https://intranet.authkit.test/notes", "https://metadata.authkit.test/latest/meta-data/", "https://mixed.authkit.test/x"])
def test_allowlisted_host_on_private_network_is_never_contacted(url: str) -> None:
    web = FakeDocsWeb()
    with pytest.raises(URLRejected, match="non-public address"):
        fetch(make_fetcher(web), url)
    assert web.requests == []


def test_content_type_size_and_status_limits() -> None:
    web = FakeDocsWeb()
    fetcher = make_fetcher(web)
    with pytest.raises(FetchError, match="unsupported content type 'application/pdf'"):
        fetch(fetcher, "https://docs.authkit.test/en/manual.pdf")
    with pytest.raises(FetchError, match="more than the 4000-byte limit"):  # declared Content-Length
        fetch(fetcher, "https://docs.authkit.test/en/huge.html")
    with pytest.raises(FetchError, match="HTTP 500"):
        fetch(fetcher, "https://docs.authkit.test/en/error.html")
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch(fetcher, "https://docs.authkit.test/en/missing.html")


def test_streamed_oversize_is_cut_off() -> None:
    web = FakeDocsWeb()
    produced: list[int] = []

    def chunks():
        for i in range(1000):  # 1 MB if read to the end; no Content-Length header
            produced.append(i)
            yield b"x" * 1000

    web.overrides["https://docs.authkit.test/en/stream.html"] = lambda request: httpx2.Response(200, headers={"content-type": "text/html"}, content=chunks())
    with pytest.raises(FetchError, match="larger than the 4000-byte limit; reading stopped"):
        fetch(make_fetcher(web), "https://docs.authkit.test/en/stream.html")
    assert len(produced) < 10


def test_timeouts_and_network_errors_are_clear() -> None:
    web = FakeDocsWeb()

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    web.overrides["https://docs.authkit.test/en/slow.html"] = slow
    web.overrides["https://docs.authkit.test/en/broken.html"] = broken
    fetcher = make_fetcher(web, timeout_seconds=3)
    with pytest.raises(FetchError, match="timed out after 3s"):
        fetch(fetcher, "https://docs.authkit.test/en/slow.html")
    with pytest.raises(FetchError, match="network error .* ConnectError"):
        fetch(fetcher, "https://docs.authkit.test/en/broken.html")


def test_robots_txt_is_honored() -> None:
    web = FakeDocsWeb()
    fetcher = make_fetcher(web)
    with pytest.raises(URLRejected, match="robots.txt"):
        fetch(fetcher, "https://docs.authkit.test/en/private/notes.html")
    assert not any(r.url.path == "/en/private/notes.html" for r in web.requests)
    fetch(fetcher, "https://docs.authkit.test/en/tokens.html")
    assert sum(r.url.path == "/robots.txt" for r in web.requests) == 1  # cached per host
    # a missing robots.txt (even an HTML error page) means no rules
    fetch(fetcher, "https://mk.authkit.test/timeouts/")
    # and robots can be switched off for hosts the operator controls
    plain = make_fetcher(FakeDocsWeb(), respect_robots=False)
    assert fetch(plain, "https://docs.authkit.test/en/private/notes.html").status == 200


def test_request_budget() -> None:
    web = FakeDocsWeb()
    fetcher = make_fetcher(web, max_fetches=3)
    fetch(fetcher, "https://docs.authkit.test/en/tokens.html")  # robots.txt + page
    fetch(fetcher, "https://docs.authkit.test/en/api.html")
    with pytest.raises(BudgetExhausted, match="budget \\(3 requests\\) is used up"):
        fetch(fetcher, "https://docs.authkit.test/en/index.html")
    assert len(web.requests) == 3
