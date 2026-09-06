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

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", os.getenv("GH_TOKEN", ""))
GITHUB_REPO = os.getenv("GITHUB_REPO", "xznexil3/vless-parser-bot")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_SUB_PATH = os.getenv("GITHUB_SUB_PATH", "")
PUBLIC_URL = ""


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


PREMIUM = {}


def pe(name: str, fallback: str = "") -> str:
    return ""


# Provider keys requested for the white-list aggregate. Keep this tuple in the
# same order as the source list shown to users.
REQUIRED_PROVIDER_KEYS = (
    "collection",
    "zieng2",
    "etoneya",
    "igareck",
    "cid_vpn",
    "wrtrmmu",
    "wlrus",
    "byewhitelists2",
    "vercel",
    "ghost_vpn",
    "vpn_bolt",
)

# Only VLESS links are extracted from every payload. ``first_available`` means
# that the URLs are mirrors; ``all`` means that each URL is a distinct feed.
SOURCES = {
    "collection": {
        "name": "Сборник подписок против БС",
        "description": "Объединённая WL-подписка",
        "url_strategy": "first_available",
        "urls": [
            "https://codeberg.org/VALCHIK/bypass-rkn-blocks/raw/branch/main/configs/obhod_WL",
        ],
    },
    "zieng2": {
        "name": "zieng2",
        "description": "Полная VLESS WL-подписка",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt",
            "https://codeberg.org/zieng2/wl/raw/branch/main/vless_universal.txt",
            "https://gitverse.ru/api/repos/zieng2/wl/raw/branch/master/list_universal.txt",
        ],
    },
    "etoneya": {
        "name": "EtoNeYa",
        "description": "Подписка для белых списков",
        "url_strategy": "first_available",
        "urls": [
            "https://etoneya.su/whitelist",
            "https://etoneya.vercel.app/whitelist",
        ],
    },
    "igareck": {
        "name": "igareck",
        "description": "VLESS из белых CIDR",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/WHITE-CIDR-RU-all.txt",
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
        ],
    },
    "cid_vpn": {
        "name": "CID VPN",
        "description": "Основная и WL-подписки CID VPN",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/CidVpn/cid-vpn-config/refs/heads/main/general.txt",
            "https://gitverse.ru/api/repos/cid-uskoritel/cid-white/raw/branch/master/whitelist.txt",
        ],
    },
    "wrtrmmu": {
        "name": "wrtrmmu",
        "description": "Случайная WL-выборка nowmeow",
        "url_strategy": "first_available",
        "urls": [
            "https://nowmeow.pw/8ybBd3fdCAQ6Ew5H0d66Y1hMbh63GpKUtEXQClIu/whitelist",
        ],
    },
    "wlrus": {
        "name": "wlrus.lol",
        "description": "Проверенные WL RUS подсети",
        "url_strategy": "first_available",
        "urls": [
            "https://wlrus.lol/confs/wl.txt",
            "https://gitverse.ru/api/repos/bywarm/rser/raw/branch/master/wl.txt",
            "https://s3c3.001.gpucloud.ru/wlr/wl.txt",
        ],
    },
    "byewhitelists2": {
        "name": "ByeWhiteLists 2.0",
        "description": "GoodbyeWL / ByeWhiteLists 2.0",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/ByeWhiteLists/ByeWhiteLists2/refs/heads/main/ByeWhiteLists2.txt",
        ],
    },
    "vercel": {
        "name": "Vercel",
        "description": "white-lists.vercel.app, Россия",
        "url_strategy": "first_available",
        "urls": [
            "https://white-lists.vercel.app/api/filter?code=RU",
        ],
    },
    "ghost_vpn": {
        "name": "Ghost-vpn.ru",
        "description": "Две WL-подписки Ghost VPN",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist.txt",
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/Whitelist%20%E2%84%962.txt",
        ],
    },
    "vpn_bolt": {
        "name": "VPN bolt",
        "description": "VLESS Reality White из russian-white-bolt_fix",
        "url_strategy": "first_available",
        "urls": [
            "https://gitverse.ru/api/repos/RUVIPIEN/russian-white-bolt_fix/raw/branch/master/configs/v2ray/VLESS_Reality_White.txt",
        ],
    },
    # Black-list feeds remain separate so the black aggregate does not mix in
    # the white-list providers above.
    "igareck_black": {
        "name": "igareck — black",
        "description": "VLESS для обычных блокировок",
        "url_strategy": "all",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/BLACK_VLESS_RUS.txt",
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/BLACK_VLESS_RUS_mobile.txt",
        ],
    },
    "etoneya_black": {
        "name": "EtoNeYa — black",
        "description": "EtoNeYa для обычных блокировок",
        "url_strategy": "first_available",
        "urls": [
            "https://etoneya.su/other",
            "https://etoneya.vercel.app/blacklist",
        ],
    },
    "ghost_vpn_black": {
        "name": "Ghost-vpn.ru — black",
        "description": "Ghost VPN для обычных блокировок",
        "url_strategy": "first_available",
        "urls": [
            "https://raw.githubusercontent.com/SilentGhostCodes/WhiteListVpn/refs/heads/main/BlackList.txt",
        ],
    },
}

BLACK_SOURCE_KEYS = ["igareck_black", "etoneya_black", "ghost_vpn_black"]
WHITE_SOURCE_KEYS = list(REQUIRED_PROVIDER_KEYS)
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
    "CUSTOM_100": {
        "filename": "CUSTOM_100.txt",
        "profile_title": "Free VPN • Crimson — Custom 100",
        "source_keys": [],
        "description": "Custom 100",
    },
}
AGGREGATED_SUBS["COMBINED"] = AGGREGATED_SUBS["FULL"]


WELCOME_TEXT = """<b>Free VPN • Crimson</b> — рабочие автообновляемые конфиги для вашего интернета. По вопросам «Помощь»

<b>Два режима:</b>

<b>Черные</b> — весь трафик через VPN

<b>Белые</b> — для жестких ТСПУ, когда работает только VK / Яндекс

<i>Построй свой суверенитет в сети с помощью Crimson.</i>"""

HELP_TEXT = """<b>Free VPN • Crimson — помощь</b>

<b>«Профиль»</b> — твой ID
<b>«Белые списки»</b> — для ТСПУ
<b>«Черные списки»</b> — весь трафик через VPN
<b>«Полный список»</b> — все вместе

<b>Как подключить:</b>
1. Выбери список → скопируй ссылку
2. Вставь в Happ / Streisand / v2rayNG
3. Обнови → подключись

Вопросы — @unnervin"""

SOURCES_TEXT = """<b>Источники VLESS</b>

Сборник подписок против БС, zieng2, EtoNeYa, igareck, CID VPN, wrtrmmu, wlrus.lol, ByeWhiteLists 2.0, Vercel, Ghost-vpn.ru и VPN bolt.

Из payload извлекаются только корректные <b>VLESS</b>; зеркала используются как fallback, дубликаты удаляются."""
