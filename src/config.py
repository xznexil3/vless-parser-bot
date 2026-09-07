import json
import os
from dotenv import load_dotenv
from provider_registry import load_provider_registry

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "768223541") or 768223541)
_extra_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {ADMIN_ID}
if _extra_admins:
    for value in _extra_admins.split(","):
        try:
            ADMIN_IDS.add(int(value.strip()))
        except ValueError:
            pass

CHANNEL_ID = os.getenv("CHANNEL_ID", "@vpncrimson")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@vpncrimson")
CHANNEL_USERNAME = "@vpncrimson"
CHANNEL_LINK = "https://t.me/vpncrimson"

# Periodic refreshes use strict URI validation by default. The admin
# "Проверка и очистка" action explicitly adds endpoint connectivity checks.
CHECK_MODE = os.getenv("CHECK_MODE", "syntax").strip().lower()
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "8080"))
DISCOVERY_MAX_REPOS = max(1, min(int(os.getenv("DISCOVERY_MAX_REPOS", "12")), 24))
DISCOVERY_MAX_FEEDS = max(1, min(int(os.getenv("DISCOVERY_MAX_FEEDS", "16")), 16))
DISCOVERY_MAX_FILES_PER_REPO = max(
    1,
    min(int(os.getenv("DISCOVERY_MAX_FILES_PER_REPO", "3")), 5),
)
DISCOVERY_MIN_VALID = max(1, min(int(os.getenv("DISCOVERY_MIN_VALID", "1")), 100))
DISCOVERY_MAX_CONFIGS = max(
    100,
    min(int(os.getenv("DISCOVERY_MAX_CONFIGS", "3000")), 3000),
)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", os.getenv("GH_TOKEN", ""))
GITHUB_REPO = os.getenv("GITHUB_REPO", "xznexil3/vless-parser-bot")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_SUB_PATH = os.getenv("GITHUB_SUB_PATH", "")

# Optional Bot API 9.4 custom emoji IDs. Keep empty for Unicode-only mode.
# Example: CUSTOM_EMOJI_IDS='{"profile":"6039422865189638057"}'
try:
    _custom_emoji_payload = json.loads(os.getenv("CUSTOM_EMOJI_IDS", "{}"))
except json.JSONDecodeError:
    _custom_emoji_payload = {}
if not isinstance(_custom_emoji_payload, dict):
    _custom_emoji_payload = {}
CUSTOM_EMOJI_IDS = {
    str(key): str(value).strip()
    for key, value in _custom_emoji_payload.items()
    if str(value).strip()
}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


PREMIUM = {}


def pe(name: str, fallback: str = "") -> str:
    return ""


# Only explicitly selected GitHub feeds are automatic aggregation sources.
# Non-GitHub mirrors and broad collection/index feeds are intentionally excluded.
SOURCES = {
    "zieng2": {
        "name": "zieng2 — GitHub",
        "description": "VLESS whitelist feed",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt",
        ],
    },
    "igareck": {
        "name": "igareck — white GitHub",
        "description": "VLESS из белых CIDR",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/WHITE-CIDR-RU-all.txt",
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
        ],
    },
    "cid_vpn": {
        "name": "CID VPN — GitHub",
        "description": "Основная VLESS-подписка CID VPN",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/CidVpn/cid-vpn-config/refs/heads/main/general.txt",
        ],
    },
    "byewhitelists2": {
        "name": "ByeWhiteLists 2.0 — GitHub",
        "description": "GoodbyeWL / ByeWhiteLists 2.0",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/ByeWhiteLists/ByeWhiteLists2/refs/heads/main/ByeWhiteLists2.txt",
        ],
    },
    "ghost_vpn": {
        "name": "Ghost VPN — white GitHub",
        "description": "White-list VLESS feeds",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist.txt",
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist%20%E2%84%962.txt",
        ],
    },
    "igareck_black": {
        "name": "igareck — black GitHub",
        "description": "VLESS для обычных блокировок",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/BLACK_VLESS_RUS.txt",
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/BLACK_VLESS_RUS_mobile.txt",
        ],
    },
    "ghost_vpn_black": {
        "name": "Ghost VPN — black GitHub",
        "description": "Black-list VLESS feed",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/BlackList.txt",
        ],
    },
    "aetris_vpn": {
        "name": "AetrisVPN — black GitHub",
        "description": "Автообновляемая VLESS-подписка AetrisVPN",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/flaafix/AetrisVPN-black-list/main/configs.txt",
        ],
    },
}

# This definition is used only when an administrator explicitly presses
# «Поиск источников». Search results never enter aggregation until individually
# approved and persisted as dynamic providers.
PUBLIC_GITHUB_SEARCH = {
    "search_queries": [
        "vless vpn config in:name,description,readme",
        "vless subscription in:name,description,readme",
        "vless config list in:name,description,readme",
        "vpn subscription config in:name,description,readme",
        "vpn whitelist config in:name,description,readme",
        "vpn blacklist config in:name,description,readme",
        "vless white list in:name,description,readme",
        "vless black list in:name,description,readme",
    ],
}

STATIC_WHITE_SOURCE_KEYS = (
    "zieng2",
    "igareck",
    "cid_vpn",
    "byewhitelists2",
    "ghost_vpn",
)
STATIC_BLACK_SOURCE_KEYS = (
    "igareck_black",
    "ghost_vpn_black",
    "aetris_vpn",
)
STATIC_SOURCE_KEYS = frozenset(SOURCES)
WHITE_SOURCE_KEYS = list(STATIC_WHITE_SOURCE_KEYS)
BLACK_SOURCE_KEYS = list(STATIC_BLACK_SOURCE_KEYS)
FULL_SOURCE_KEYS = WHITE_SOURCE_KEYS + BLACK_SOURCE_KEYS
DYNAMIC_PROVIDER_RECORDS = []


def apply_dynamic_providers(records=None):
    """Atomically rebuild runtime source lists from the persistent registry."""
    global DYNAMIC_PROVIDER_RECORDS
    if records is None:
        records = load_provider_registry()
    records = [record for record in records if isinstance(record, dict)]

    for key in list(SOURCES):
        if key not in STATIC_SOURCE_KEYS:
            SOURCES.pop(key, None)
    WHITE_SOURCE_KEYS[:] = STATIC_WHITE_SOURCE_KEYS
    BLACK_SOURCE_KEYS[:] = STATIC_BLACK_SOURCE_KEYS

    accepted = []
    for record in records:
        source_key = str(record.get("id", ""))
        urls = list(record.get("urls", []))
        category = record.get("category")
        if not source_key.startswith("dynamic_") or not urls or category not in {"white", "black"}:
            continue
        accepted.append(record)
        if not record.get("enabled", True):
            continue
        SOURCES[source_key] = {
            "name": str(record.get("name") or source_key),
            "description": str(record.get("description") or "Публичный GitHub VLESS feed"),
            "url_strategy": "all",
            "urls": urls,
            "dynamic": True,
        }
        target = WHITE_SOURCE_KEYS if category == "white" else BLACK_SOURCE_KEYS
        if source_key not in target:
            target.append(source_key)

    FULL_SOURCE_KEYS[:] = [*WHITE_SOURCE_KEYS, *BLACK_SOURCE_KEYS]
    DYNAMIC_PROVIDER_RECORDS = accepted
    if "AGGREGATED_SUBS" in globals():
        AGGREGATED_SUBS["WHITE_FULL"]["source_keys"] = WHITE_SOURCE_KEYS
        AGGREGATED_SUBS["BLACK_FULL"]["source_keys"] = BLACK_SOURCE_KEYS
        AGGREGATED_SUBS["FULL"]["source_keys"] = FULL_SOURCE_KEYS
    return list(DYNAMIC_PROVIDER_RECORDS)


apply_dynamic_providers()

AGGREGATED_SUBS = {
    "BLACK_FULL": {
        "filename": "BLACK_FULL.txt",
        "profile_title": "Free VPN • Crimson — Black",
        "source_keys": BLACK_SOURCE_KEYS,
        "description": "Чёрные",
    },
    "WHITE_FULL": {
        "filename": "WHITE_FULL.txt",
        "profile_title": "Free VPN • Crimson — White",
        "source_keys": WHITE_SOURCE_KEYS,
        "description": "Белые",
    },
    "FULL": {
        "filename": "FULL.txt",
        "profile_title": "Free VPN • Crimson — Full",
        "source_keys": FULL_SOURCE_KEYS,
        "description": "Полный",
    },
}
AGGREGATED_SUBS["COMBINED"] = AGGREGATED_SUBS["FULL"]


WELCOME_TEXT = """<b>🔑 Free VPN • Crimson</b> — рабочие автообновляемые конфиги для вашего интернета. По вопросам «❔ Помощь»

<b>🎛️ Два режима:</b>

<b>⬛ Черные</b> — весь трафик через VPN

<b>⬜ Белые</b> — для жестких ТСПУ, когда работает только VK / Яндекс

<i>🔐 Построй свой суверенитет в сети с помощью Crimson.</i>"""

FILE_USAGE_TEXT = """<b>📄 Как использовать файл:</b>
1. ⬇️ Скачай полученный <code>.txt</code> и открой его.
2. 📋 Нажми «Выделить всё», затем «Копировать» — нужно скопировать <b>всё содержимое файла целиком</b>, то есть все строки <code>vless://…</code>, а не одну конфигурацию.
3. 🔗 В VPN-клиенте выбери «Импорт из буфера обмена» и вставь <b>весь скопированный текст сразу</b>.
4. ✅ Подтверди импорт: клиент добавит все конфигурации из файла."""

HELP_TEXT = f"""<b>❔ Free VPN • Crimson — помощь</b>

<b>👤 «Профиль»</b> — твой ID
<b>⬜ «Белые списки»</b> — для ТСПУ
<b>⬛ «Черные списки»</b> — весь трафик через VPN
<b>📚 «Полный список»</b> — все вместе
<b>💎 «Платные подписки»</b> — белые и чёрные планы с оплатой Telegram Stars
<b>🧾 Пакеты и конфиги</b> — сначала выбери пакет до 300 конфигов, затем открой нужный VLESS: его параметры и проверка TCP-задержки появятся прямо в сообщении
<b>💬 «Поддержка»</b> — написать команде бота, не переходя в личные сообщения

📄 Бот отправляет только готовые <code>.txt</code>-файлы, без ссылок на подписки.

{FILE_USAGE_TEXT}

🧩 Подходящие клиенты: Happ, Hiddify, Streisand, v2rayNG и NekoRay.

💬 Если возник вопрос, нажми «Поддержка» в главном меню и напиши сообщение прямо в бот."""

SOURCES_TEXT = """<b>🗂️ Источники VLESS</b>

Автоматически используются только отобранные GitHub feed-ы: zieng2, igareck, CID VPN, ByeWhiteLists 2.0, Ghost VPN, AetrisVPN и одобренные администратором провайдеры.

🔎 Ручной админ-поиск охватывает VLESS, VPN, proxy/Xray, config, subscription, white/whitelist, black/blacklist и list. Найденные файлы не участвуют в обновлении или очистке, пока администратор явно не одобрит каждый источник. Из содержимого принимаются только корректные <b>VLESS</b>; дубликаты удаляются."""
