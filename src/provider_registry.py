"""Persistent, GitHub-only dynamic provider registry.

The admin interface may add public GitHub files as VLESS providers.  This
module deliberately rejects other hosts, credentials, query strings, and
private subscription URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT_DIR = Path(__file__).resolve().parents[1]
REGISTRY_FILE = ROOT_DIR / "providers.json"
REGISTRY_VERSION = 1
MAX_DYNAMIC_PROVIDERS = 20
VALID_CATEGORIES = {"white", "black"}
PROVIDER_ID_RE = re.compile(r"^dynamic_[0-9a-f]{12}$")
GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
ALLOWED_FILE_SUFFIXES = {"", ".txt", ".conf", ".list", ".json", ".yaml", ".yml"}


def _clean_segments(path: str) -> list[str]:
    segments = [unquote(part) for part in path.split("/") if part]
    if any(
        part in {".", ".."}
        or "/" in part
        or "\\" in part
        or len(part) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in part)
        for part in segments
    ):
        raise ValueError("Некорректный путь GitHub")
    return segments


def _validate_repo(owner: str, repository: str) -> tuple[str, str]:
    repository = repository.removesuffix(".git")
    if not GITHUB_NAME_RE.fullmatch(owner) or not GITHUB_NAME_RE.fullmatch(repository):
        raise ValueError("Некорректное имя GitHub-репозитория")
    return owner, repository


def parse_github_repository_url(url: str) -> tuple[str, str] | None:
    """Return ``(owner, repository)`` for an exact public GitHub repo URL."""
    try:
        parsed = urlsplit((url or "").strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        parts = _clean_segments(parsed.path)
        if len(parts) != 2:
            return None
        return _validate_repo(parts[0], parts[1])
    except ValueError:
        return None


def normalize_github_raw_url(url: str) -> str:
    """Normalize supported GitHub file links to ``raw.githubusercontent.com``."""
    try:
        parsed = urlsplit((url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректная ссылка") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or host not in {"github.com", "raw.githubusercontent.com"}
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Разрешены только публичные HTTPS-ссылки GitHub без параметров")

    parts = _clean_segments(parsed.path)
    if len(parts) < 4:
        raise ValueError("Ссылка должна вести на файл GitHub")
    owner, repository = _validate_repo(parts[0], parts[1])

    if host == "raw.githubusercontent.com":
        remainder = parts[2:]
        if remainder[:2] == ["refs", "heads"]:
            remainder = remainder[2:]
    else:
        marker = parts[2].lower()
        if marker not in {"blob", "raw"}:
            raise ValueError("Используй ссылку на файл GitHub или raw-ссылку")
        remainder = parts[3:]
        if remainder[:2] == ["refs", "heads"]:
            remainder = remainder[2:]

    if len(remainder) < 2:
        raise ValueError("В ссылке отсутствует ветка или путь файла")
    branch, file_parts = remainder[0], remainder[1:]
    if not branch or not file_parts:
        raise ValueError("В ссылке отсутствует ветка или путь файла")

    suffix = Path(file_parts[-1]).suffix.lower()
    if suffix not in ALLOWED_FILE_SUFFIXES:
        raise ValueError("Поддерживаются только текстовые файлы конфигураций")

    encoded = "/".join(quote(part, safe="-._~%") for part in [owner, repository, branch, *file_parts])
    return f"https://raw.githubusercontent.com/{encoded}"


def github_file_identity(url: str) -> tuple[str, str, str]:
    """Return repository name, filename and canonical raw URL."""
    normalized = normalize_github_raw_url(url)
    parts = [unquote(part) for part in urlsplit(normalized).path.split("/") if part]
    return f"{parts[0]}/{parts[1]}", parts[-1], normalized


def provider_id_for_url(url: str) -> str:
    normalized = normalize_github_raw_url(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"dynamic_{digest}"


def build_provider_record(
    url: str,
    category: str,
    *,
    added_at: str,
    name: str = "",
) -> dict:
    repository, filename, normalized = github_file_identity(url)
    if category not in VALID_CATEGORIES:
        raise ValueError("Категория должна быть white или black")
    display_name = (name or f"{repository} — {filename}").strip()[:120]
    return {
        "id": provider_id_for_url(normalized),
        "name": display_name,
        "description": "Публичный GitHub VLESS feed, добавленный через админ-панель",
        "category": category,
        "urls": [normalized],
        "enabled": True,
        "added_at": str(added_at),
    }


def normalize_provider_record(record: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    try:
        urls = [normalize_github_raw_url(url) for url in record.get("urls", [])]
        urls = list(dict.fromkeys(urls))
        if not urls:
            return None
        expected_id = provider_id_for_url(urls[0])
        record_id = str(record.get("id", expected_id))
        if record_id != expected_id or not PROVIDER_ID_RE.fullmatch(record_id):
            return None
        category = str(record.get("category", ""))
        if category not in VALID_CATEGORIES:
            return None
        repository, filename, _ = github_file_identity(urls[0])
        return {
            "id": record_id,
            "name": str(record.get("name") or f"{repository} — {filename}").strip()[:120],
            "description": str(record.get("description") or "Публичный GitHub VLESS feed").strip()[:300],
            "category": category,
            "urls": urls,
            "enabled": bool(record.get("enabled", True)),
            "added_at": str(record.get("added_at", ""))[:40],
        }
    except (TypeError, ValueError):
        return None


def normalize_registry(records) -> list[dict]:
    normalized = []
    seen_ids = set()
    for record in records if isinstance(records, list) else []:
        cleaned = normalize_provider_record(record)
        if cleaned and cleaned["id"] not in seen_ids:
            seen_ids.add(cleaned["id"])
            normalized.append(cleaned)
            if len(normalized) >= MAX_DYNAMIC_PROVIDERS:
                break
    return normalized


def load_provider_registry(path: Path = REGISTRY_FILE) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    records = payload.get("providers", []) if isinstance(payload, dict) else []
    return normalize_registry(records)


def registry_content(records) -> str:
    payload = {
        "version": REGISTRY_VERSION,
        "providers": normalize_registry(records),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def save_provider_registry(records, path: Path = REGISTRY_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(registry_content(records), encoding="utf-8")
    temporary.replace(path)
