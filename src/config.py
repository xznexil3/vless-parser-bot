import os
from dotenv import load_dotenv

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
AUTO_DISCOVERY = os.getenv("AUTO_DISCOVERY", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
DISCOVERY_MAX_REPOS = max(1, min(int(os.getenv("DISCOVERY_MAX_REPOS", "6")), 12))
DISCOVERY_MAX_FEEDS = max(1, min(int(os.getenv("DISCOVERY_MAX_FEEDS", "8")), 16))
DISCOVERY_MIN_VALID = max(1, min(int(os.getenv("DISCOVERY_MIN_VALID", "10")), 100))
DISCOVERY_MAX_CONFIGS = max(
    100,
    min(int(os.getenv("DISCOVERY_MAX_CONFIGS", "1200")), 3000),
)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", os.getenv("GH_TOKEN", ""))
GITHUB_REPO = os.getenv("GITHUB_REPO", "xznexil3/vless-parser-bot")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_SUB_PATH = os.getenv("GITHUB_SUB_PATH", "")


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


PREMIUM = {}


def pe(name: str, fallback: str = "") -> str:
    return ""


# Only explicitly selected GitHub feeds and strict GitHub discovery are used.
# Non-GitHub mirrors and broad collection/index feeds are intentionally excluded.
SOURCES = {
    "zieng2": {
        "name": "zieng2 — GitHub",
        "connect_name": "zieng2",
        "description": "VLESS whitelist feed",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt",
        ],
    },
    "igareck": {
        "name": "igareck — white GitHub",
        "connect_name": "igareck",
        "description": "VLESS из белых CIDR",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/WHITE-CIDR-RU-all.txt",
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
        ],
    },
    "cid_vpn": {
        "name": "CID VPN — GitHub",
        "connect_name": "CID VPN",
        "description": "Основная VLESS-подписка CID VPN",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/CidVpn/cid-vpn-config/refs/heads/main/general.txt",
        ],
    },
    "byewhitelists2": {
        "name": "ByeWhiteLists 2.0 — GitHub",
        "connect_name": "ByeWhiteLists 2.0",
        "description": "GoodbyeWL / ByeWhiteLists 2.0",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/ByeWhiteLists/ByeWhiteLists2/refs/heads/main/ByeWhiteLists2.txt",
        ],
    },
    "ghost_vpn": {
        "name": "Ghost VPN — white GitHub",
        "connect_name": "Ghost VPN",
        "description": "White-list VLESS feeds",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist.txt",
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist%20%E2%84%962.txt",
        ],
    },
    "igareck_black": {
        "name": "igareck — black GitHub",
        "connect_name": "igareck №2",
        "description": "VLESS для обычных блокировок",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/BLACK_VLESS_RUS.txt",
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/BLACK_VLESS_RUS_mobile.txt",
        ],
    },
    "ghost_vpn_black": {
        "name": "Ghost VPN — black GitHub",
        "connect_name": "Ghost VPN №2",
        "description": "Black-list VLESS feed",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/BlackList.txt",
        ],
    },
    "aetris_vpn": {
        "name": "AetrisVPN — black GitHub",
        "connect_name": "AetrisVPN",
        "description": "Автообновляемая VLESS-подписка AetrisVPN",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/flaafix/AetrisVPN-black-list/main/configs.txt",
        ],
    },
    "github_discovery": {
        "name": "Строгий GitHub-поиск VLESS",
        "connect_name": "GitHub discovery",
        "description": "Поиск VLESS/VPN/config/list/blacklist feed-ов",
        "discovery": True,
        "search_queries": [
            "vless vpn config blacklist in:name,description,readme stars:>5",
            "vless vpn config list in:name,description,readme stars:>10",
            "vless subscription blacklist in:name,description,readme stars:>3",
        ],
        "urls": [],
    },
}
if not AUTO_DISCOVERY:
    SOURCES.pop("github_discovery", None)

WHITE_SOURCE_KEYS = [
    "zieng2",
    "igareck",
    "cid_vpn",
    "byewhitelists2",
    "ghost_vpn",
]
BLACK_SOURCE_KEYS = [
    "igareck_black",
    "ghost_vpn_black",
    "aetris_vpn",
]
if AUTO_DISCOVERY:
    BLACK_SOURCE_KEYS.append("github_discovery")
FULL_SOURCE_KEYS = WHITE_SOURCE_KEYS + BLACK_SOURCE_KEYS

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


WELCOME_TEXT = """<b>Free VPN • Crimson</b> — рабочие автообновляемые VLESS-подписки для вашего интернета.

Нажми <b>«Подключить»</b>, выбери источник и добавь его raw-ссылку в VPN-клиент как подписку.

<i>Построй свой суверенитет в сети с помощью Crimson.</i>"""

HELP_TEXT = """<b>Free VPN • Crimson — помощь</b>

<b>«Профиль»</b> — твой ID
<b>«Подключить»</b> — все доступные подписки и их raw-ссылки без разделения на белые и чёрные списки.

<b>Как подключиться:</b>
1. Открой «Подключить».
2. Скопируй raw-ссылку выбранного источника.
3. В Happ, Hiddify, Streisand, v2rayNG или NekoRay выбери добавление подписки по ссылке.
4. Вставь ссылку и обнови подписку.

Количество VLESS рядом с каждым источником обновляется автоматически.
Вопросы — @unnervin"""

SOURCES_TEXT = """<b>Источники VLESS</b>

Во вкладке «Подключить» отображаются рабочие GitHub raw-ссылки zieng2, igareck, CID VPN, ByeWhiteLists 2.0, Ghost VPN, AetrisVPN и строгого GitHub-поиска.

Поиск учитывает слова VLESS, VPN, config, list и blacklist. Широкие collection/index-источники отключены. Из подписок принимаются только корректные <b>VLESS</b>; дубликаты удаляются."""
