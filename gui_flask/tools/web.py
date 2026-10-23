"""Web fetch tool — retrieve URL content with basic HTML-to-text extraction."""
from __future__ import annotations
import html
import ipaddress
import re
import socket
from urllib import error, parse, request

from . import register_tool

_MAX_CONTENT_LENGTH = 100_000  # 100 KB max
_SEARCH_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _html_to_text(raw: str) -> str:
    """Minimal HTML→text: strip tags, decode entities, collapse whitespace."""
    # Remove script/style blocks
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    # Strip tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _is_blocked_hostname(hostname: str) -> bool:
    """Block loopback, private, link-local, and metadata addresses.

    Prefix checks such as ``startswith("172.")`` also blocked public
    addresses in 172.0.0.0/8 and missed 127.0.0.2, decimal loopback,
    and 169.254.169.254.
    """
    host = (hostname or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host in {"", "localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    if host.isdigit():
        try:
            return _is_blocked_ip(ipaddress.ip_address(int(host)))
        except ValueError:
            return True
    try:
        return _is_blocked_ip(ipaddress.ip_address(host))
    except ValueError:
        return False


def _reject_private_url(url: str) -> None:
    parsed = parse.urlparse(url)
    hostname = parsed.hostname or ""
    if _is_blocked_hostname(hostname):
        raise ValueError("Blocked: cannot fetch private network URLs")
    try:
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if _is_blocked_hostname(ip):
                raise ValueError(f"Blocked: '{hostname}' resolves to private IP {ip}")
    except socket.gaierror:
        pass
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")


class _GuardedRedirect(request.HTTPRedirectHandler):
    """Re-check every redirect so a public URL cannot hop to a private one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _reject_private_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _unwrap_redirect_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = parse.urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = parse.parse_qs(parsed.query).get("uddg", [])
        if uddg:
            return parse.unquote(uddg[0])
    return url


def _extract_search_results(raw: str, max_results: int) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, title_html in _SEARCH_RESULT_RE.findall(raw):
        url = _unwrap_redirect_url(html.unescape(href))
        parsed = parse.urlparse(url)
        hostname = parsed.hostname or ""
        if _is_blocked_hostname(hostname):
            continue
        title = _html_to_text(title_html)
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append((title, url))
        if len(results) >= max(1, max_results):
            break
    return results


@register_tool(
    name="fetch_url",
    description="Fetch a URL and return its text content. HTML is converted to plain text. Max 100KB.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_length": {"type": "integer", "description": "Max chars to return (default 100000)"},
        },
        "required": ["url"],
    },
    server_side=True,
)
def fetch_url(url: str, max_length: int = _MAX_CONTENT_LENGTH) -> str:
    _reject_private_url(url)
    opener = request.build_opener(_GuardedRedirect)
    req = request.Request(url, headers={
        "User-Agent": "MYKRO-Agent/1.0 (local tool)",
    })
    try:
        with opener.open(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_length).decode("utf-8", errors="replace")
    except error.HTTPError as e:
        return f"HTTP {e.code}: {e.reason}"
    except error.URLError as e:
        return f"URL error: {e.reason}"

    if "html" in content_type.lower():
        return _html_to_text(raw)
    return raw


@register_tool(
    name="search_web",
    description="Search the public web for up-to-date references and return a small list of results with titles and URLs.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Maximum number of results to return (default 5)"},
        },
        "required": ["query"],
    },
    server_side=True,
)
def search_web(query: str, max_results: int = 5) -> str:
    safe_limit = max(1, min(int(max_results), 10))
    encoded_query = parse.quote_plus(query)
    url = f"https://duckduckgo.com/html/?q={encoded_query}"
    req = request.Request(url, headers={
        "User-Agent": "MYKRO-Agent/1.0 (local tool)",
    })
    try:
        with request.urlopen(req, timeout=15) as resp:
            raw = resp.read(_MAX_CONTENT_LENGTH).decode("utf-8", errors="replace")
    except error.HTTPError as e:
        return f"HTTP {e.code}: {e.reason}"
    except error.URLError as e:
        return f"URL error: {e.reason}"

    results = _extract_search_results(raw, safe_limit)
    if not results:
        return f'No search results found for "{query}".'

    lines = [f'Search results for "{query}":']
    for idx, (title, result_url) in enumerate(results, start=1):
        lines.append(f"{idx}. {title}\n   URL: {result_url}")
    return "\n".join(lines)
