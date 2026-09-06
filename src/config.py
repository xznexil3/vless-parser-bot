import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "768223541") or 768223541)
# Доп. админы через запятую
_extra_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {ADMIN_ID}
if _extra_admins:
    for x in _extra_admins.split(","):
        try:
            ADMIN_IDS.add(int(x.strip()))
        except: pass
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHECK_MODE = os.getenv("CHECK_MODE", "syntax")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "30"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
PORT = int(os.getenv("PORT", "8080"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", os.getenv("GH_TOKEN", ""))
GITHUB_REPO = os.getenv("GITHUB_REPO", "xznexil3/crs-support-bot")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_SUB_PATH = os.getenv("GITHUB_SUB_PATH", "")

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# === Premium эмодзи — оставлены для совместимости, но не используются (убраны смайлики по ТЗ) ===
PREMIUM = {
    "fire_crimson": "",
    "fire": "",
    "rocket": "",
    "sparkles": "",
    "diamond": "",
    "shield": "",
    "lightning": "",
    "ghost": "",
    "heart": "",
    "black_heart": "",
    "thumbsup": "",
    "warning": "",
    "cross": "",
    "computer": "",
    "crystal": "",
}

def pe(name: str, fallback: str = "") -> str:
    """Быстрый доступ к premium emoji с fallback — теперь возвращает пусто"""
    return ""

# === Источники ===
SOURCES = {
    "black_all": {
        "name": "Чёрные списки — VLESS",
        "description": "Весь трафик через VPN",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
        ],
    },
    "black_mobile": {
        "name": "Чёрные списки — Mobile",
        "description": "150 лучших для телефона",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
        ],
    },
    "white_cidr_all": {
        "name": "Белые списки — CIDR ALL",
        "description": "Все белые подсети",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-all.txt",
        ],
    },
    "white_cidr_checked": {
        "name": "Белые списки — VK / YA / CDN",
        "description": "Только VK, Yandex, CDNVideo, Beeline — самые стабильные",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-checked.txt",
        ],
    },
    "white_mobile": {
        "name": "Белые списки — Mobile",
        "description": "Для телефона, CIDR",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
        ],
    },
    "white_sni": {
        "name": "Белые — SNI",
        "description": "Только Fake SNI",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-SNI-RU-all.txt",
        ],
    },
    "ss_black": {
        "name": "Shadowsocks — Чёрные",
        "description": "SS, Trojan, Hysteria2 для чёрных",
        "urls": [
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_SS+All_RUS.txt",
        ],
    },
    "extra_zieng2": {
        "name": "Подписка от zieng2",
        "description": "Белые списки от zieng2",
        "urls": [
            "https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt",
            "https://codeberg.org/zieng2/wl/raw/branch/main/vless_universal.txt",
            "https://gitlab.com/zieng2/wl/-/raw/main/vless_universal.txt",
        ],
    },
    "extra_etoneya_white": {
        "name": "Подписка от etoneya — белые",
        "description": "whitelist etoneya",
        "urls": [
            "https://etoneya.su/whitelist",
            "https://raw.githubusercontent.com/EtoNeYaProject/etoneyaproject.github.io/refs/heads/main/whitelist",
            "https://ety.twinkvibe.gay/whitelist",
            "https://etoneya.vercel.app/whitelist",
            "https://alley.serv00.net/whitelist",
        ],
    },
    "extra_etoneya_black": {
        "name": "Подписка от etoneya — чёрные",
        "description": "etoneya other/blacklist",
        "urls": [
            "https://etoneya.su/other",
            "https://etoneya.su/1",
            "https://raw.githubusercontent.com/EtoNeYaProject/etoneyaproject.github.io/refs/heads/main/1",
            "https://raw.githubusercontent.com/EtoNeYaProject/etoneyaproject.github.io/refs/heads/main/2",
        ],
    },
    "extra_bye2": {
        "name": "ByeWhiteLists 2.0",
        "description": "Подписка ByeWhiteLists 2.0",
        "urls": [
            "https://raw.githubusercontent.com/ByeWhiteLists/ByeWhiteLists2/refs/heads/main/ByeWhiteLists2.txt",
        ],
    },
    "extra_cid": {
        "name": "CID VPN",
        "description": "Подписка CID VPN",
        "urls": [
            "https://raw.githubusercontent.com/Hidashimora/free-vpn-anti-rkn/main/configs/1.1.txt",
        ],
    },
    "extra_wrtrmmu": {
        "name": "Подписка от wrtrmmu",
        "description": "WARP / TURN VK Calls",
        "urls": [
            "https://raw.githubusercontent.com/Hidashimora/free-vpn-anti-rkn/main/configs/2.1.txt",
        ],
    },
    "extra_vercel": {
        "name": "Подписка от Vercel",
        "description": "Зеркало через Vercel",
        "urls": [
            "https://etoneya.vercel.app/whitelist",
            "https://etoneya.vercel.app/1",
            "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/refs/heads/main/githubmirror/26.txt",
        ],
    },
    "extra_bolt": {
        "name": "VPN bolt",
        "description": "Подписка VPN bolt",
        "urls": [
            "https://raw.githubusercontent.com/Hidashimora/free-vpn-anti-rkn/main/configs/3.1.txt",
        ],
    },
    "extra_sbornik": {
        "name": "Сборник подписок против БС",
        "description": "Сборник: все белые + чёрные в одном месте",
        "urls": [
            "https://raw.githubusercontent.com/VAL41K/bypass-rkn-blocks/main/README.md",
        ],
    },
    "universal": {
        "name": "Универсальные VLESS",
        "description": "Фолбэк, если РФ-источники пустые",
        "urls": [
            "https://raw.githubusercontent.com/barry-far/V2ray-Config/main/Splitted-By-Protocol/vless.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/V2Ray-Config-By-EbraSha.txt",
            "https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/protocols/vless.txt",
        ],
    },
}

GROUPS = {
    "black": ["black_all", "black_mobile", "ss_black", "extra_etoneya_black", "extra_bolt", "extra_vercel"],
    "white": ["white_cidr_all", "white_cidr_checked", "white_mobile", "white_sni", "extra_zieng2", "extra_etoneya_white", "extra_bye2", "extra_cid", "extra_wrtrmmu", "extra_sbornik"],
    "all": ["black_all", "black_mobile", "ss_black", "white_cidr_all", "white_cidr_checked", "white_mobile", "extra_zieng2", "extra_etoneya_white", "extra_bye2", "extra_etoneya_black"],
}

AGGREGATED_SUBS = {
    "BLACK_FULL": {
        "filename": "BLACK_FULL.txt",
        "profile_title": "Free VPN • Crimson — Black",
        "source_keys": ["black_all", "black_mobile", "ss_black", "extra_etoneya_black", "extra_bolt", "extra_vercel"],
        "description": "Чёрные списки",
    },
    "WHITE_FULL": {
        "filename": "WHITE_FULL.txt",
        "profile_title": "Free VPN • Crimson — White",
        "source_keys": ["white_cidr_all", "white_cidr_checked", "white_mobile", "white_sni", "extra_zieng2", "extra_etoneya_white", "extra_bye2", "extra_cid", "extra_wrtrmmu", "extra_sbornik"],
        "description": "Белые списки",
    },
    "FULL": {
        "filename": "FULL.txt",
        "profile_title": "Free VPN • Crimson — Full",
        "source_keys": ["black_all", "black_mobile", "ss_black", "white_cidr_all", "white_cidr_checked", "white_mobile", "white_sni", "extra_zieng2", "extra_etoneya_white", "extra_bye2", "extra_etoneya_black", "extra_cid", "extra_wrtrmmu", "extra_vercel", "extra_bolt", "extra_sbornik"],
        "description": "Полный список — все белые и чёрные вместе",
    },
    "CUSTOM_100": {
        "filename": "CUSTOM_100.txt",
        "profile_title": "Free VPN • Crimson — Custom 100",
        "source_keys": [],
        "description": "Кастомная подписка из 100 рабочих конфигов",
    },
}

AGGREGATED_SUBS["COMBINED"] = AGGREGATED_SUBS["FULL"]

WELCOME_TEXT = """<b>Free VPN • Crimson</b> — рабочие автообновляемые конфиги для вашего интернета.

Два режима:
<b>Черные</b> — весь трафик через VPN
<b>Белые</b> — для жестких ТСПУ, когда работает только VK / Яндекс

Режимы протоколов внутри каждого списка:
<b>VLESS</b> · <b>Trojan</b> · <b>Shadowsocks</b> · <b>VMess</b> · <b>Hysteria2</b>

Выбери кнопку:
• <b>Полный список</b> — одна большая ссылка (делим по 300)
• <b>Белые / Черные</b> — выбери протокол, получи пакеты по 300
• <b>Собрать подписку</b> — 100 разных рабочих конфигов в одной ссылке
"""

HELP_TEXT = """<b>Free VPN • Crimson — помощь</b>

Нажми кнопку в меню:

<b>Мой профиль</b> — твой ID
<b>Белые списки</b> — для ТСПУ: выбор протокола → пакеты по 300 (VLESS/Trojan/SS/VMess/Hy2)
<b>Черные списки</b> — классический VPN: выбор протокола → пакеты по 300
<b>Полный список</b> — всё вместе: выбор протокола → пакеты по 300
<b>Собрать подписку</b> — бот соберет 100 разных рабочих конфигов и выдаст ссылку
<b>Помощь</b> — это окно

<b>Протоколы (режимы):</b>
• <b>VLESS</b> — основной, Reality, работает везде
• <b>Trojan</b> — для Sing-box / Clash
• <b>Shadowsocks</b> — SS
• <b>VMess</b> — старый V2Ray
• <b>Hysteria2 / Hy2</b> — скоростной QUIC

<b>Как подключить:</b>
1. Нажми <b>Белые / Черные / Полный</b> → выбери протокол → выбери пакет (1..N по 300)
2. Скопируй ссылку вида <code>https://raw.githubusercontent.com/.../FULL_VLESS_1.txt</code>
3. Вставь как <b>URL подписки</b> в Happ / Streisand / v2rayNG / Hiddify / Throne / NekoBox
4. Обнови подписку → выбери сервер → Connect

Клиенты: <b>Happ, Streisand, v2rayNG, Hiddify, Throne, NekoRay, Karing, Exclave</b>
Автообновление — раз в час. Все файлы по 300 для стабильной загрузки.

Вопросы — @wtfparsbot
"""

SOURCES_TEXT = """<b>Free VPN • Crimson — источники</b>
Основное: igareck, zieng2, etoneya, ByeWhiteLists 2.0, CID, wrtrmmu, Vercel, VPN bolt + зеркала.
Полный список зеркал — в конфиге бота (src/config.py → SOURCES).

Авто-очистка: приватные IP, битые UUID/порт, дубликаты — удаляются.
Всё делится по протоколам и по 300.
"""
