"""Validated private registry for paid white/black VLESS products."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT_DIR = Path(__file__).resolve().parents[1]
STORAGE_DIR = Path(os.getenv("PAID_STORAGE_DIR", str(ROOT_DIR / "data"))).expanduser()
REGISTRY_FILE = STORAGE_DIR / "paid_subscriptions.json"
FILES_DIR = STORAGE_DIR / "paid_files"
REGISTRY_VERSION = 1
MAX_PAID_PLANS = 40
VALID_CATEGORIES = {"white", "black"}
PLAN_ID_RE = re.compile(r"^paid_[0-9a-f]{12}$")


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_delivery_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректная HTTPS-ссылка") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or len(value) > 1000
    ):
        raise ValueError("Нужна HTTPS-ссылка без логина и пароля")
    return value


def paid_plan_id(name: str, category: str, created_at: str) -> str:
    seed = f"{category}\0{name}\0{created_at}".encode("utf-8")
    return f"paid_{hashlib.sha256(seed).hexdigest()[:12]}"


def paid_file_path(plan_id: str) -> Path:
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("Некорректный ID платной подписки")
    return FILES_DIR / f"{plan_id}.txt"


def build_paid_plan(
    *,
    name: str,
    category: str,
    stars_price: int,
    description: str,
    created_at: str,
    delivery_url: str = "",
    has_file: bool = False,
) -> dict:
    name = _clean_text(name, 64)
    description = _clean_text(description, 300)
    category = str(category or "").strip().lower()
    if not name:
        raise ValueError("Название не может быть пустым")
    if category not in VALID_CATEGORIES:
        raise ValueError("Категория должна быть white или black")
    try:
        stars_price = int(stars_price)
    except (TypeError, ValueError) as exc:
        raise ValueError("Цена Stars должна быть целым числом") from exc
    if not 1 <= stars_price <= 1_000_000:
        raise ValueError("Цена Stars должна быть от 1 до 1000000")
    delivery_url = normalize_delivery_url(delivery_url)
    plan_id = paid_plan_id(name, category, created_at)
    file_path = f"paid_files/{plan_id}.txt" if has_file else ""
    if not file_path and not delivery_url:
        raise ValueError("Добавь .txt-файл или HTTPS-ссылку для выдачи")
    return {
        "id": plan_id,
        "name": name,
        "description": description,
        "category": category,
        "stars_price": stars_price,
        "delivery_url": delivery_url,
        "file_path": file_path,
        "enabled": True,
        "created_at": str(created_at)[:40],
    }


def normalize_paid_plan(record: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    try:
        name = _clean_text(record.get("name"), 64)
        category = str(record.get("category", "")).lower()
        created_at = str(record.get("created_at", ""))[:40]
        plan_id = str(record.get("id", ""))
        if (
            not name
            or category not in VALID_CATEGORIES
            or not PLAN_ID_RE.fullmatch(plan_id)
            or paid_plan_id(name, category, created_at) != plan_id
        ):
            return None
        stars_price = int(record.get("stars_price", 0))
        if not 1 <= stars_price <= 1_000_000:
            return None
        delivery_url = normalize_delivery_url(record.get("delivery_url", ""))
        file_path = str(record.get("file_path", ""))
        expected_file = f"paid_files/{plan_id}.txt"
        if file_path not in {"", expected_file}:
            return None
        if not file_path and not delivery_url:
            return None
        return {
            "id": plan_id,
            "name": name,
            "description": _clean_text(record.get("description"), 300),
            "category": category,
            "stars_price": stars_price,
            "delivery_url": delivery_url,
            "file_path": file_path,
            "enabled": bool(record.get("enabled", True)),
            "created_at": created_at,
        }
    except (TypeError, ValueError):
        return None


def normalize_paid_registry(records) -> list[dict]:
    normalized = []
    seen = set()
    for record in records if isinstance(records, list) else []:
        cleaned = normalize_paid_plan(record)
        if cleaned and cleaned["id"] not in seen:
            seen.add(cleaned["id"])
            normalized.append(cleaned)
            if len(normalized) >= MAX_PAID_PLANS:
                break
    return normalized


def load_paid_registry(path: Path = REGISTRY_FILE) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    records = payload.get("subscriptions", []) if isinstance(payload, dict) else []
    return normalize_paid_registry(records)


def paid_registry_content(records) -> str:
    return json.dumps(
        {
            "version": REGISTRY_VERSION,
            "subscriptions": normalize_paid_registry(records),
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def save_paid_registry(records, path: Path = REGISTRY_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(paid_registry_content(records), encoding="utf-8")
    temporary.replace(path)


__all__ = [
    "FILES_DIR",
    "MAX_PAID_PLANS",
    "REGISTRY_FILE",
    "build_paid_plan",
    "load_paid_registry",
    "normalize_delivery_url",
    "paid_file_path",
    "paid_registry_content",
    "save_paid_registry",
]
