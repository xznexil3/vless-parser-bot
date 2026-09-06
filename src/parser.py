"""VLESS subscription fetching, extraction, validation, and endpoint checks."""

import asyncio
import base64
import ipaddress
import json
import re
import uuid
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

import aiohttp

from config import SOURCES

# Delimiters here are not legal unescaped URI data and commonly surround links
# in HTML, JSON, Markdown, and Telegram exports.
VLESS_REGEX = re.compile(r"vless://[^\s\r\n\"'<>`\\|]+", re.IGNORECASE)
# Xray accepts the canonical 128-bit UUID representation. Public VLESS feeds
# also use reserved/future UUID version bits, so imposing RFC version 1-5 here
# would incorrectly discard otherwise parseable client IDs.
UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PERCENT_ESCAPE_REGEX = re.compile(r"%(?![0-9a-fA-F]{2})")
QUERY_KEY_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
BASE64_REGEX = re.compile(r"^[A-Za-z0-9_+/=-]+$")
DOMAIN_LABEL_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

HEADERS = {
    "User-Agent": "v2rayNG/1.9.35 (VLESS-Parser-Bot/2.0)",
    "Accept": "text/plain, application/json;q=0.9, */*;q=0.5",
}
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DECODE_DEPTH = 3
MAX_DECODED_ITEMS = 10_000

ALLOWED_SECURITY = {"none", "tls", "reality", "xtls"}
ALLOWED_TRANSPORTS = {
    "tcp",
    "raw",
    "kcp",
    "mkcp",
    "ws",
    "http",
    "h2",
    "grpc",
    "gun",
    "quic",
    "httpupgrade",
    "xhttp",
    "splithttp",
}
BOOLEAN_PARAMS = {"allowinsecure", "insecure"}


def _contains_control(value: str) -> bool:
    return any(
        ord(char) < 32
        or 127 <= ord(char) <= 159
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    )


def _canonical_host(host: str) -> str:
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        return host.rstrip(".").encode("idna").decode("ascii").lower()


def _validate_public_host(host: str, *, allow_single_label: bool = False) -> Tuple[bool, str]:
    if not host:
        return False, "пустой host"
    if host != host.strip() or _contains_control(host):
        return False, "недопустимые символы в host"
    if "%" in host:
        return False, "zone id/escape в host не поддерживается"

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[0-9.]+", host):
            return False, "невалидный IP"
        try:
            ascii_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            return False, "невалидное IDN-имя"
        if len(ascii_host) > 253 or not ascii_host:
            return False, "невалидная длина домена"
        if not allow_single_label and "." not in ascii_host:
            return False, "host должен быть публичным доменом или IP"
        if ascii_host == "localhost" or ascii_host.endswith((".localhost", ".local")):
            return False, "локальный host"
        if any(not DOMAIN_LABEL_REGEX.fullmatch(label) for label in ascii_host.split(".")):
            return False, "невалидное доменное имя"
        return True, "ok"

    # Public subscription feeds must never expose local, documentation,
    # multicast, or otherwise non-routable addresses.
    if not address.is_global:
        return False, "непубличный IP"
    return True, "ok"


def _validate_header_host(value: str) -> bool:
    """Validate an HTTP Host value, allowing an optional numeric port."""
    if not value or "/" in value or "@" in value:
        return False
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if not host or parsed.path or parsed.query or parsed.fragment:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    return _validate_public_host(host)[0]


def is_bad_host(host: str) -> bool:
    """Compatibility helper used by older callers."""
    return not _validate_public_host(host)[0]


def _parse_query(query: str) -> Tuple[Optional[List[Tuple[str, str]]], str]:
    if PERCENT_ESCAPE_REGEX.search(query):
        return None, "невалидное percent-encoding в query"
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=False,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"невалидный query: {exc}"

    seen = set()
    for key, value in pairs:
        if not QUERY_KEY_REGEX.fullmatch(key):
            return None, f"невалидное имя параметра: {key!r}"
        lowered = key.lower()
        if lowered in seen:
            return None, f"повторяющийся параметр: {key}"
        seen.add(lowered)
        if _contains_control(value):
            return None, f"управляющий символ в параметре {key}"
    return pairs, "ok"


def _get_param(params: Dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        if name.lower() in params:
            return params[name.lower()]
    return default


def _valid_reality_public_key(value: str) -> bool:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
    except (ValueError, TypeError):
        return False
    return len(decoded) == 32


def is_valid_vless(link: str) -> Tuple[bool, str]:
    """Strictly validate a VLESS share URI.

    This validates the URI, address, UUID, transport/security values, and
    Reality-specific fields. Network reachability is handled separately by
    :func:`validate_configs` in ``tcp`` mode.
    """
    if not isinstance(link, str):
        return False, "конфиг должен быть строкой"
    link = link.strip()
    if not link.lower().startswith("vless://"):
        return False, "не vless://"
    if len(link) > 16_384:
        return False, "URI слишком длинный"
    if any(char.isspace() for char in link) or _contains_control(link):
        return False, "пробел/управляющий символ в URI"
    if "\\" in link or PERCENT_ESCAPE_REGEX.search(link):
        return False, "невалидное экранирование URI"

    try:
        parsed = urlsplit(link)
    except ValueError as exc:
        return False, f"невалидный URI: {exc}"

    if parsed.scheme.lower() != "vless":
        return False, "не vless://"
    if not parsed.netloc:
        return False, "нет authority"
    if parsed.path not in ("", "/"):
        return False, "path должен передаваться query-параметром"
    if parsed.password is not None:
        return False, "пароль в authority недопустим"
    if parsed.username is None or parsed.netloc.count("@") != 1:
        return False, "требуется uuid@host:port"

    try:
        user = unquote(parsed.username, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return False, "невалидное кодирование UUID"
    if not UUID_REGEX.fullmatch(user):
        return False, "невалидный UUID"
    try:
        if str(uuid.UUID(user)).lower() != user.lower():
            return False, "UUID не в canonical-формате"
    except ValueError:
        return False, "невалидный UUID"

    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        return False, f"невалидный host/port: {exc}"
    if host is None:
        return False, "нет host"
    host_ok, host_reason = _validate_public_host(host)
    if not host_ok:
        return False, host_reason
    if port is None:
        return False, "нет порта"
    if not 1 <= port <= 65535:
        return False, "порт вне диапазона"

    pairs, reason = _parse_query(parsed.query)
    if pairs is None:
        return False, reason
    params = {key.lower(): value for key, value in pairs}

    encryption = _get_param(params, "encryption", default="none").lower()
    if encryption != "none":
        return False, "VLESS поддерживает encryption=none"

    security = _get_param(params, "security", default="none").lower() or "none"
    if security not in ALLOWED_SECURITY:
        return False, f"неподдерживаемый security={security}"

    transport = _get_param(params, "type", "network", default="tcp").lower()
    if transport not in ALLOWED_TRANSPORTS:
        return False, f"неподдерживаемый type={transport}"

    for key in BOOLEAN_PARAMS:
        if key in params and params[key].lower() not in {"0", "1", "true", "false"}:
            return False, f"невалидный boolean {key}"

    sni = _get_param(params, "sni", "servername")
    if sni:
        sni_ok, _ = _validate_public_host(sni)
        if not sni_ok:
            return False, "невалидный SNI"

    header_host = _get_param(params, "host")
    if header_host:
        for item in header_host.split(","):
            if not _validate_header_host(item.strip()):
                return False, "невалидный host query-параметр"

    if parsed.fragment:
        try:
            remark = unquote(parsed.fragment, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False, "невалидное кодирование remark"
        if _contains_control(remark):
            return False, "управляющий символ в remark"

    if security == "reality":
        public_key = _get_param(params, "pbk", "publickey")
        if not _valid_reality_public_key(public_key):
            return False, "для Reality требуется корректный pbk/publicKey"
        if not sni:
            return False, "для Reality требуется SNI"
        short_id = _get_param(params, "sid", "shortid")
        if short_id and (not re.fullmatch(r"[0-9a-fA-F]{2,16}", short_id) or len(short_id) % 2):
            return False, "невалидный Reality short id"

    return True, "ok"


def _unescape_container_text(text: str) -> str:
    # JSON and HTML wrappers seen in provider APIs and copied web pages.
    text = text.replace("\\/", "/")
    def decode_ascii_escape(match: re.Match) -> str:
        codepoint = int(match.group(1), 16)
        # Decode URI punctuation but leave non-ASCII and surrogate escapes for
        # json.loads(); manufacturing lone surrogates would make UTF-8 output
        # impossible to save safely.
        return chr(codepoint) if 0x20 <= codepoint <= 0x7E else match.group(0)

    text = re.sub(r"\\u([0-9a-fA-F]{4})", decode_ascii_escape, text)
    replacements = {
        "&amp;": "&",
        "&#38;": "&",
        "&#x26;": "&",
        "&quot;": '"',
        "&#34;": '"',
    }
    for encoded, decoded in replacements.items():
        text = text.replace(encoded, decoded)
    return text


def _trim_extracted_uri(uri: str) -> str:
    uri = uri.strip().rstrip(".,;!")
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    changed = True
    while uri and changed:
        changed = False
        for opening, closing in pairs:
            if uri.endswith(closing) and uri.count(closing) > uri.count(opening):
                uri = uri[:-1]
                changed = True
    return uri


def _try_base64_decode(value: str) -> Optional[str]:
    value = value.strip().lstrip("\ufeff").strip("\"'").rstrip(",")
    compact = "".join(value.split())
    if len(compact) < 16 or len(compact) > MAX_RESPONSE_BYTES * 2:
        return None
    if not BASE64_REGEX.fullmatch(compact):
        return None
    raw = compact.rstrip("=")
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        return decoded.decode("utf-8-sig", errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None


def _json_string_values(text: str) -> Iterable[str]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{\"":
        return []
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return []

    values = []
    stack = [payload]
    while stack and len(values) < MAX_DECODED_ITEMS:
        value = stack.pop()
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return values


def extract_configs(text: str, proto_filter: str = "vless") -> List[str]:
    """Extract VLESS links from plain, wrapped, JSON, and base64 payloads.

    ``proto_filter`` remains for API compatibility; values other than
    ``"vless"`` and ``"all"`` return an empty list because the bot is VLESS-only.
    """
    if proto_filter not in {"vless", "all"} or not isinstance(text, str) or not text:
        return []

    queue = [(text[:MAX_RESPONSE_BYTES], 0)]
    queued = {text[:MAX_RESPONSE_BYTES]}
    found: List[str] = []
    seen_links = set()
    processed = 0

    while queue and processed < MAX_DECODED_ITEMS:
        current, depth = queue.pop(0)
        processed += 1
        normalized = _unescape_container_text(current)

        for match in VLESS_REGEX.finditer(normalized):
            uri = _trim_extracted_uri(match.group(0))
            if uri and uri not in seen_links:
                seen_links.add(uri)
                found.append(uri)

        if depth >= MAX_DECODE_DEPTH:
            continue

        json_values = list(_json_string_values(current))
        for value in json_values:
            # Parsed JSON strings may contain real newlines that were escaped
            # in the outer payload; scan those values as text, not only as
            # possible base64.
            if value and value not in queued and len(queued) < MAX_DECODED_ITEMS:
                queued.add(value)
                queue.append((value, depth + 1))

        candidates = [current, *json_values]
        if "\n" in current or "\r" in current:
            candidates.extend(current.splitlines())

        for candidate in candidates:
            decoded = _try_base64_decode(candidate)
            if decoded and decoded not in queued and len(queued) < MAX_DECODED_ITEMS:
                queued.add(decoded)
                queue.append((decoded, depth + 1))

    return found


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    """Fetch one provider payload, returning an empty string on failure."""
    try:
        async with session.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                print(f"[fetch] {url} -> {response.status}")
                return ""
            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                print(f"[fetch] {url} -> response exceeds {MAX_RESPONSE_BYTES} bytes")
                return ""
            charset = response.charset or "utf-8"
            try:
                return body.decode(charset, errors="replace")
            except LookupError:
                return body.decode("utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as exc:
        print(f"[fetch error] {url}: {exc}")
        return ""


def extract_host_port(link: str) -> Optional[Tuple[str, int]]:
    ok, _ = is_valid_vless(link)
    if not ok:
        return None
    try:
        parsed = urlsplit(link.strip())
        return _canonical_host(parsed.hostname or ""), int(parsed.port)
    except (TypeError, ValueError, UnicodeError):
        return None


async def check_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    """Check whether the VLESS endpoint accepts a TCP connection."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        return True
    except (OSError, asyncio.TimeoutError, ValueError):
        return False


def is_valid_any(link: str) -> Tuple[bool, str]:
    if isinstance(link, str) and link.strip().lower().startswith("vless://"):
        return is_valid_vless(link)
    return False, "only vless allowed"


def vless_identity(link: str) -> Optional[Tuple]:
    """Return a fragment-independent canonical identity for deduplication."""
    ok, _ = is_valid_vless(link)
    if not ok:
        return None
    parsed = urlsplit(link.strip())
    pairs, _ = _parse_query(parsed.query)
    canonical_query = tuple(sorted((key.lower(), value) for key, value in (pairs or [])))
    return (
        unquote(parsed.username or "").lower(),
        _canonical_host(parsed.hostname or ""),
        parsed.port,
        canonical_query,
    )


def deduplicate_configs(links: Iterable[str]) -> List[str]:
    """Deduplicate exact and semantically equal VLESS links.

    Remarks/fragments do not affect a connection, and query ordering does not
    make a second configuration, so both are ignored by the identity key.
    """
    result = []
    seen_exact = set()
    seen_identity = set()
    for link in links:
        if link in seen_exact:
            continue
        seen_exact.add(link)
        identity = vless_identity(link)
        key = identity if identity is not None else ("invalid", link)
        if key in seen_identity:
            continue
        seen_identity.add(key)
        result.append(link)
    return result


async def _check_endpoint_statuses(
    endpoints: Iterable[Tuple[str, int]],
    *,
    concurrency: int = 100,
) -> Dict[Tuple[str, int], bool]:
    """Check a set of unique endpoints with one shared concurrency bound."""
    unique_endpoints = set(endpoints)
    semaphore = asyncio.Semaphore(concurrency)

    async def check_endpoint(endpoint: Tuple[str, int]) -> Tuple[Tuple[str, int], bool]:
        async with semaphore:
            return endpoint, await check_tcp(endpoint[0], endpoint[1], timeout=2.5)

    if not unique_endpoints:
        return {}
    return dict(
        await asyncio.gather(*(check_endpoint(endpoint) for endpoint in unique_endpoints))
    )


async def validate_configs(
    links: List[str],
    mode: str = "syntax",
    allow_generic: bool = False,
) -> List[str]:
    """Validate every config by syntax and optionally endpoint connectivity.

    Modes are ``none``, ``syntax``, and ``tcp``. In ``tcp`` mode each unique
    host/port is checked once and all configs for unreachable endpoints are
    removed. ``allow_generic`` is retained for compatibility and ignored.
    """
    del allow_generic
    mode = (mode or "syntax").strip().lower()
    if mode not in {"none", "syntax", "tcp"}:
        raise ValueError(f"unknown validation mode: {mode}")
    if mode == "none":
        return list(links)

    syntax_valid = [link for link in links if is_valid_vless(link)[0]]
    syntax_valid = deduplicate_configs(syntax_valid)
    if mode == "syntax" or not syntax_valid:
        return syntax_valid

    endpoints = {extract_host_port(link) for link in syntax_valid}
    endpoints.discard(None)
    statuses = await _check_endpoint_statuses(endpoints)
    return [link for link in syntax_valid if statuses.get(extract_host_port(link), False)]


async def fetch_category(
    session: aiohttp.ClientSession,
    category_key: str,
    mode: str = "syntax",
) -> Dict:
    cfg = SOURCES.get(category_key)
    if not cfg:
        return {
            "key": category_key,
            "name": category_key,
            "configs": [],
            "raw_text": "",
            "error": "unknown category",
            "errors": ["unknown category"],
        }

    strategy = cfg.get("url_strategy", "all")
    if strategy not in {"all", "first_available"}:
        strategy = "all"

    all_configs = []
    raw_parts = []
    errors = []
    used_urls = []

    for url in cfg.get("urls", []):
        text = await fetch_text(session, url)
        if not text:
            errors.append(f"{url} — пусто/ошибка")
            continue
        extracted = extract_configs(text, proto_filter="vless")
        if not extracted:
            errors.append(f"{url} — VLESS не найден")
            continue
        if not any(is_valid_vless(link)[0] for link in extracted):
            errors.append(f"{url} — нет валидных VLESS")
            continue
        used_urls.append(url)
        raw_parts.append(f"# Source: {url}\n{text.strip()}\n")
        all_configs.extend(extracted)
        if strategy == "first_available":
            break

    raw_total = len(all_configs)
    filtered = await validate_configs(all_configs, mode=mode)
    removed = raw_total - len(filtered)
    invalid_reasons = Counter()
    if removed:
        for link in all_configs:
            ok, reason = is_valid_vless(link)
            if not ok:
                invalid_reasons[reason] += 1
        print(
            f"[{category_key}] очистка: {raw_total} -> {len(filtered)} "
            f"(удалено {removed})"
        )

    return {
        "key": category_key,
        "name": cfg["name"],
        "description": cfg.get("description", ""),
        "configs": filtered,
        "raw_total": raw_total,
        "filtered_total": len(filtered),
        "removed": removed,
        "validation_mode": mode,
        "invalid_reasons": dict(invalid_reasons),
        "errors": errors,
        "urls": cfg.get("urls", []),
        "used_urls": used_urls,
        "raw_text": "\n".join(raw_parts)[:200_000],
    }


async def fetch_all(mode: str = "syntax", categories: Optional[List[str]] = None) -> Dict[str, Dict]:
    if categories is None:
        categories = list(SOURCES.keys())
    normalized_mode = (mode or "syntax").strip().lower()
    if normalized_mode not in {"none", "syntax", "tcp"}:
        raise ValueError(f"unknown validation mode: {mode}")

    # Fetch and syntax-check providers concurrently. TCP checks are performed
    # once afterwards so one global semaphore bounds the whole refresh and a
    # shared endpoint published by many providers is contacted only once.
    category_mode = "syntax" if normalized_mode == "tcp" else normalized_mode
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *(fetch_category(session, key, mode=category_mode) for key in categories)
        )

    if normalized_mode == "tcp":
        endpoints = {
            endpoint
            for result in results
            for link in result.get("configs", [])
            if (endpoint := extract_host_port(link)) is not None
        }
        statuses = await _check_endpoint_statuses(endpoints)
        for result in results:
            syntax_configs = result.get("configs", [])
            reachable = [
                link
                for link in syntax_configs
                if statuses.get(extract_host_port(link), False)
            ]
            result["configs"] = reachable
            result["filtered_total"] = len(reachable)
            result["removed"] = result.get("raw_total", len(syntax_configs)) - len(reachable)
            result["tcp_removed"] = len(syntax_configs) - len(reachable)
            result["validation_mode"] = "tcp"

    return {result["key"]: result for result in results}


def make_subscription_content(configs: List[str], header: str = "") -> str:
    lines = []
    if header:
        lines.append(header)
    lines.extend(configs)
    return "\n".join(lines)


def make_base64_subscription(configs: List[str], header: str = "") -> str:
    content = make_subscription_content(configs, header)
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def parse_vless_info(link: str) -> Dict:
    ok, reason = is_valid_vless(link)
    if not ok:
        return {
            "remark": link[:40] if isinstance(link, str) else "?",
            "host": "?",
            "port": "?",
            "error": reason,
        }
    parsed = urlsplit(link.strip())
    pairs, _ = _parse_query(parsed.query)
    params = {key.lower(): value for key, value in (pairs or [])}
    try:
        remark = unquote(parsed.fragment, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        remark = parsed.fragment
    host = parsed.hostname or ""
    port = parsed.port or 0
    return {
        "remark": remark or f"{host}:{port}",
        "host": host,
        "port": str(port),
        "uuid": unquote(parsed.username or ""),
        "sni": _get_param(params, "sni", "servername"),
        "type": _get_param(params, "type", "network", default="tcp"),
        "security": _get_param(params, "security", default="none"),
        "fp": _get_param(params, "fp"),
    }
