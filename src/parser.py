"""VLESS subscription fetching, extraction, validation, and endpoint checks."""

import asyncio
import base64
import ipaddress
import json
import re
import uuid
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import aiohttp

from config import (
    AUTO_DISCOVERY,
    DISCOVERY_MAX_CONFIGS,
    DISCOVERY_MAX_FEEDS,
    DISCOVERY_MAX_FILES_PER_REPO,
    DISCOVERY_MAX_REPOS,
    DISCOVERY_MIN_VALID,
    GITHUB_REPO,
    GITHUB_TOKEN,
    SOURCES,
)
from provider_registry import (
    ALLOWED_FILE_SUFFIXES,
    normalize_github_raw_url,
    parse_github_repository_url,
)

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
DISCOVERY_MAX_FILE_BYTES = 8 * 1024 * 1024
DISCOVERY_MAX_CONFIGS_PER_FEED = 300
DISCOVERY_ALLOWED_SUFFIXES = {"", ".txt", ".conf", ".list", ".json", ".yaml", ".yml"}
DISCOVERY_EXCLUDED_NAMES = {
    "readme",
    "license",
    "changelog",
    "requirements",
    "domain",
    "domains",
    "cidr",
    "rules",
}

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


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    request_headers: Optional[Dict[str, str]] = None,
) -> str:
    """Fetch one provider payload, returning an empty string on failure."""
    try:
        async with session.get(
            url,
            headers=request_headers or HEADERS,
            timeout=FETCH_TIMEOUT,
        ) as response:
            if response.status != 200:
                print(f"[fetch] {url} -> {response.status}")
                return ""
            chunks = []
            total = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    print(f"[fetch] {url} -> response exceeds {MAX_RESPONSE_BYTES} bytes")
                    return ""
                chunks.append(chunk)
            body = b"".join(chunks)
            charset = response.charset or "utf-8"
            try:
                return body.decode(charset, errors="replace")
            except LookupError:
                return body.decode("utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as exc:
        print(f"[fetch error] {url}: {exc}")
        return ""


def _discovery_url_score(url: str) -> int:
    """Rank public GitHub text files related to VPN configs and subscriptions."""
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() != "raw.githubusercontent.com":
        return -100
    parts = [unquote(part).lower() for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return -100
    name = parts[-1]
    stem, dot, suffix = name.rpartition(".")
    suffix = f".{suffix}" if dot else ""
    if suffix not in DISCOVERY_ALLOWED_SUFFIXES:
        return -100

    file_tokens = set(re.split(r"[^a-z0-9]+", "/".join(parts[3:])))
    if file_tokens & DISCOVERY_EXCLUDED_NAMES:
        return -100
    searchable = "/".join([parts[0], parts[1], *parts[3:]])
    vpn_markers = ("vless", "vpn", "proxy", "xray", "sing-box", "singbox")
    feed_markers = (
        "config",
        "subscription",
        "subscribe",
        "sub",
        "feed",
        "list",
        "whitelist",
        "blacklist",
        "white",
        "black",
    )
    if not any(term in searchable for term in vpn_markers):
        return -100
    if not any(term in searchable for term in feed_markers):
        return -100

    score = 10
    if "vless" in searchable:
        score += 30
    if "vpn" in searchable:
        score += 18
    if "xray" in searchable or "singbox" in searchable or "sing-box" in searchable:
        score += 10
    if "blacklist" in searchable or "black" in file_tokens:
        score += 12
    if "whitelist" in searchable or "white" in file_tokens:
        score += 12
    if "config" in searchable:
        score += 8
    if "list" in searchable:
        score += 6
    if "subscription" in searchable or "subscribe" in searchable or "sub" in file_tokens:
        score += 6
    if suffix == ".txt":
        score += 4
    if stem in {"all", "full", "general", "vless", "configs", "config", "subscription"}:
        score += 3
    return score


async def _fetch_json(session: aiohttp.ClientSession, url: str):
    headers = dict(HEADERS)
    if GITHUB_TOKEN and (urlsplit(url).hostname or "").lower() == "api.github.com":
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    text = await fetch_text(session, url, request_headers=headers)
    if not text and "Authorization" in headers:
        # A revoked/expired publication token must not disable public discovery.
        text = await fetch_text(session, url, request_headers=HEADERS)
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


async def discover_github_feed_urls(
    session: aiohttp.ClientSession,
    source: Dict,
) -> Tuple[List[str], List[str]]:
    """Find a bounded set of public feeds through GitHub search only.

    Repository and file paths must match VPN/proxy plus config/subscription/
    white/black/list filters. Every payload then passes the VLESS validator.
    """
    if not AUTO_DISCOVERY:
        return [], ["автопоиск отключён"]

    errors = []
    candidates = []
    repository_buckets = []
    globally_seen_repositories = set()
    for query in source.get("search_queries", []):
        search_url = "https://api.github.com/search/repositories?" + urlencode(
            {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": DISCOVERY_MAX_REPOS,
            }
        )
        payload = await _fetch_json(session, search_url)
        if not isinstance(payload, dict):
            errors.append(f"GitHub search — ошибка запроса: {query}")
            continue
        bucket = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            full_name = item.get("full_name", "")
            lowered = full_name.lower()
            if (
                full_name
                and lowered != GITHUB_REPO.lower()
                and lowered not in globally_seen_repositories
                and not item.get("archived")
                and not item.get("disabled")
                and not item.get("private")
            ):
                globally_seen_repositories.add(lowered)
                bucket.append((full_name, item))
        repository_buckets.append(bucket)

    # Select repositories round-robin so whitelist, blacklist, subscription,
    # config, and general VPN queries all contribute within the global limit.
    repositories = {}
    position = 0
    while len(repositories) < DISCOVERY_MAX_REPOS:
        added = False
        for bucket in repository_buckets:
            if position < len(bucket):
                full_name, item = bucket[position]
                repositories.setdefault(full_name, item)
                added = True
                if len(repositories) >= DISCOVERY_MAX_REPOS:
                    break
        if not added:
            break
        position += 1

    for full_name, repository in list(repositories.items())[:DISCOVERY_MAX_REPOS]:
        branch = repository.get("default_branch") or "main"
        tree_url = (
            f"https://api.github.com/repos/{full_name}/git/trees/"
            f"{quote(branch, safe='')}?recursive=1"
        )
        payload = await _fetch_json(session, tree_url)
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            errors.append(f"{full_name} — не удалось прочитать GitHub tree")
            continue
        ranked = []
        for item in payload["tree"]:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = item.get("path", "")
            size = item.get("size")
            if not path or not isinstance(size, int) or not 0 < size <= DISCOVERY_MAX_FILE_BYTES:
                continue
            download_url = (
                f"https://raw.githubusercontent.com/{full_name}/"
                f"{quote(branch, safe='')}/{quote(path, safe='/%')}"
            )
            score = _discovery_url_score(download_url)
            if score >= 1:
                ranked.append((score, download_url))
        candidates.extend(
            url
            for _, url in sorted(ranked, key=lambda item: (-item[0], item[1]))[
                :DISCOVERY_MAX_FILES_PER_REPO
            ]
        )

    configured_urls = {
        url
        for configured_source in SOURCES.values()
        for url in configured_source.get("urls", [])
    }
    configured_repositories = {
        "/".join(parts[:2]).lower()
        for url in configured_urls
        if len(parts := [part for part in urlsplit(url).path.split("/") if part]) >= 2
    }
    unique = []
    seen = set()
    per_repository = Counter()
    for url in sorted(set(candidates), key=lambda item: (-_discovery_url_score(item), item)):
        if _discovery_url_score(url) < 1 or url in configured_urls or url in seen:
            continue
        parts = [part for part in urlsplit(url).path.split("/") if part]
        repository = "/".join(parts[:2]).lower() if len(parts) >= 2 else url
        # Known repositories are already fetched explicitly; discovery must add
        # independent sources rather than another file/mirror from the same repo.
        if repository in configured_repositories:
            continue
        # Keep a bounded number per repository so one large mirror cannot
        # dominate, while still parsing several independent feeds it exposes.
        if per_repository[repository] >= DISCOVERY_MAX_FILES_PER_REPO:
            continue
        per_repository[repository] += 1
        seen.add(url)
        unique.append(url)
        if len(unique) >= DISCOVERY_MAX_FEEDS:
            break
    return unique, errors


def _repository_file_score(repository: str, path: str) -> int:
    """Rank bounded text candidates inside an admin-supplied GitHub repo."""
    filename = path.rsplit("/", 1)[-1].lower()
    stem, dot, suffix = filename.rpartition(".")
    suffix = f".{suffix}" if dot else ""
    if suffix not in ALLOWED_FILE_SUFFIXES:
        return -100
    tokens = set(re.split(r"[^a-z0-9]+", f"{repository}/{path}".lower()))
    if tokens & DISCOVERY_EXCLUDED_NAMES:
        return -100
    weights = {
        "vless": 50,
        "vpn": 20,
        "config": 16,
        "configs": 16,
        "subscription": 14,
        "subscriptions": 14,
        "sub": 8,
        "list": 10,
        "blacklist": 12,
        "proxy": 5,
    }
    score = sum(weight for token, weight in weights.items() if token in tokens)
    if not tokens.intersection(weights):
        return -100
    if suffix == ".txt":
        score += 8
    if stem in {"all", "full", "general", "vless", "configs", "config"}:
        score += 4
    return score


async def discover_github_repository_feed_urls(
    session: aiohttp.ClientSession,
    repository_url: str,
    limit: int = 12,
) -> Tuple[List[str], List[str]]:
    """Return bounded candidate files from an explicitly supplied GitHub repo."""
    repository_parts = parse_github_repository_url(repository_url)
    if not repository_parts:
        return [], ["Некорректная ссылка на публичный GitHub-репозиторий"]
    owner, repository_name = repository_parts
    full_name = f"{owner}/{repository_name}"
    metadata = await _fetch_json(session, f"https://api.github.com/repos/{full_name}")
    if (
        not isinstance(metadata, dict)
        or metadata.get("archived")
        or metadata.get("disabled")
        or metadata.get("private")
    ):
        return [], [f"{full_name} — репозиторий недоступен или не является публичным"]
    branch = metadata.get("default_branch") or "main"
    tree_url = (
        f"https://api.github.com/repos/{full_name}/git/trees/"
        f"{quote(branch, safe='')}?recursive=1"
    )
    payload = await _fetch_json(session, tree_url)
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
        return [], [f"{full_name} — не удалось прочитать GitHub tree"]

    ranked = []
    for item in payload["tree"]:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = item.get("path", "")
        size = item.get("size")
        if not path or not isinstance(size, int) or not 0 < size <= DISCOVERY_MAX_FILE_BYTES:
            continue
        score = _repository_file_score(full_name, path)
        if score < 1:
            continue
        raw_url = (
            f"https://raw.githubusercontent.com/{full_name}/"
            f"{quote(branch, safe='')}/{quote(path, safe='/%')}"
        )
        try:
            ranked.append((score, normalize_github_raw_url(raw_url)))
        except ValueError:
            continue
    urls = [url for _, url in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]]
    errors = [] if urls else [f"{full_name} — подходящие текстовые файлы не найдены"]
    return urls, errors


async def _inspect_github_feed_urls(
    session: aiohttp.ClientSession,
    urls: Iterable[str],
    *,
    min_valid: int,
    max_results: int,
) -> List[Dict]:
    normalized_urls = []
    for url in urls:
        try:
            normalized = normalize_github_raw_url(url)
        except ValueError:
            continue
        if normalized not in normalized_urls:
            normalized_urls.append(normalized)

    async def inspect(url: str):
        text = await fetch_text(session, url)
        if not text:
            return None
        extracted = extract_configs(text, proto_filter="vless")[:DISCOVERY_MAX_CONFIGS]
        valid = deduplicate_configs(await validate_configs(extracted, mode="syntax"))
        if len(valid) < min_valid:
            return None
        parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
        return {
            "url": url,
            "repository": "/".join(parts[:2]),
            "filename": parts[-1] if parts else "feed",
            "extracted_count": len(extracted),
            "valid_count": len(valid),
        }

    inspected = await asyncio.gather(*(inspect(url) for url in normalized_urls))
    return [item for item in inspected if item is not None][:max_results]


async def inspect_public_github_urls(
    urls: Iterable[str],
    *,
    min_valid: int = 1,
    max_results: int = 8,
) -> List[Dict]:
    """Download and validate admin-supplied public GitHub feed candidates."""
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        return await _inspect_github_feed_urls(
            session,
            urls,
            min_valid=max(1, min_valid),
            max_results=max(1, max_results),
        )


async def find_public_github_candidates() -> Tuple[List[Dict], List[str]]:
    """Run strict bounded discovery for admin approval without adding sources."""
    source = SOURCES.get("github_discovery")
    if not source:
        return [], ["Расширенный GitHub-поиск отключён в настройках"]
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        urls, errors = await discover_github_feed_urls(session, source)
        candidates = await _inspect_github_feed_urls(
            session,
            urls,
            min_valid=DISCOVERY_MIN_VALID,
            max_results=DISCOVERY_MAX_FEEDS,
        )
    if not candidates and not errors:
        errors.append("Новые подходящие публичные GitHub-источники не найдены")
    return candidates, errors


async def inspect_public_github_repository(
    repository_url: str,
    *,
    max_results: int = 5,
) -> Tuple[List[Dict], List[str]]:
    """Find and validate VLESS files in an admin-supplied public repository."""
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        urls, errors = await discover_github_repository_feed_urls(
            session,
            repository_url,
            limit=max(5, max_results * 2),
        )
        candidates = await _inspect_github_feed_urls(
            session,
            urls,
            min_valid=1,
            max_results=max_results,
        )
    if not candidates and not errors:
        errors.append("В репозитории не найдено файлов с валидными VLESS")
    return candidates, errors


async def fetch_discovered_category(
    session: aiohttp.ClientSession,
    category_key: str,
    source: Dict,
    mode: str,
) -> Dict:
    candidate_urls, errors = await discover_github_feed_urls(session, source)

    async def inspect(url: str):
        text = await fetch_text(session, url)
        if not text:
            return url, [], 0
        extracted = extract_configs(text, proto_filter="vless")
        filtered = await validate_configs(extracted, mode=mode)
        return url, filtered[:DISCOVERY_MAX_CONFIGS_PER_FEED], len(extracted)

    inspected = await asyncio.gather(*(inspect(url) for url in candidate_urls))
    configs = []
    used_urls = []
    raw_total = 0
    for url, valid, extracted_count in inspected:
        if len(valid) < DISCOVERY_MIN_VALID:
            continue
        used_urls.append(url)
        raw_total += extracted_count
        configs = deduplicate_configs(
            [*configs, *valid[:DISCOVERY_MAX_CONFIGS_PER_FEED]]
        )[:DISCOVERY_MAX_CONFIGS]
        if len(configs) >= DISCOVERY_MAX_CONFIGS:
            break
    if not configs:
        errors.append("автопоиск не нашёл подходящих VLESS-подписок")
    return {
        "key": category_key,
        "name": source["name"],
        "description": source.get("description", ""),
        "configs": configs,
        "raw_total": raw_total,
        "filtered_total": len(configs),
        "removed": max(0, raw_total - len(configs)),
        "validation_mode": mode,
        "invalid_reasons": {},
        "errors": errors,
        "urls": [],
        "used_urls": used_urls,
        "discovered_urls": candidate_urls,
        "raw_text": "\n".join(f"# Discovered: {url}" for url in used_urls),
    }


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
    if cfg.get("discovery"):
        return await fetch_discovered_category(session, category_key, cfg, mode)

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
