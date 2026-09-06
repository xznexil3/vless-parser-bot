import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "768223541") or 768223541)
_extra_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {ADMIN_ID}
if _extra_admins:
    for x in _extra_admins.split(","):
        try:
            ADMIN_IDS.add(int(x.strip()))
        except: pass
CHANNEL_ID = os.getenv("CHANNEL_ID", "@vpncrimson")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@vpncrimson")
CHANNEL_USERNAME = "@vpncrimson"
CHANNEL_LINK = "https://t.me/vpncrimson"
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

PREMIUM = {}
def pe(name: str, fallback: str = "") -> str:
    return ""

# === Источники — только VLESS ===
SOURCES = {
    "black_all": {"name": "Чёрные — VLESS", "description": "Весь трафик через VPN", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt"]},
    "black_mobile": {"name": "Чёрные — Mobile", "description": "150 лучших", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt"]},
    "white_cidr_all": {"name": "Белые — CIDR ALL", "description": "Все белые подсети", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-all.txt"]},
    "white_cidr_checked": {"name": "Белые — VK/YA/CDN", "description": "VK, Yandex, CDN", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-checked.txt"]},
    "white_mobile": {"name": "Белые — Mobile", "description": "Для телефона", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt"]},
    "white_sni": {"name": "Белые — SNI", "description": "Fake SNI", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/WHITE-SNI-RU-all.txt"]},
    "ss_black": {"name": "Чёрные — SS", "description": "SS и т.д.", "urls": ["https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_SS+All_RUS.txt"]},
    "extra_zieng2": {"name": "zieng2", "description": "Белые", "urls": ["https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt"]},
    "extra_etoneya_white": {"name": "etoneya white", "description": "whitelist", "urls": ["https://etoneya.su/whitelist", "https://raw.githubusercontent.com/EtoNeYaProject/etoneyaproject.github.io/refs/heads/main/whitelist"]},
    "extra_etoneya_black": {"name": "etoneya black", "description": "blacklist", "urls": ["https://etoneya.su/other", "https://etoneya.su/1"]},
    "extra_bye2": {"name": "ByeWhiteLists 2.0", "description": "ByeWL", "urls": ["https://raw.githubusercontent.com/ByeWhiteLists/ByeWhiteLists2/refs/heads/main/ByeWhiteLists2.txt"]},
    "extra_cid": {"name": "CID", "description": "CID", "urls": ["https://raw.githubusercontent.com/Hidashimora/free-vpn-anti-rkn/main/configs/1.1.txt"]},
    "extra_wrtrmmu": {"name": "wrtrmmu", "description": "WARP", "urls": ["https://raw.githubusercontent.com/Hidashimora/free-vpn-anti-rkn/main/configs/2.1.txt"]},
    "extra_vercel": {"name": "Vercel", "description": "mirror", "urls": ["https://etoneya.vercel.app/whitelist"]},
    "extra_bolt": {"name": "bolt", "description": "bolt", "urls": ["https://raw.githubusercontent.com/Hidashimora/free-vpn-anti-rkn/main/configs/3.1.txt"]},
    "extra_sbornik": {"name": "Сборник", "description": "all", "urls": ["https://raw.githubusercontent.com/VAL41K/bypass-rkn-blocks/main/README.md"]},
    "universal": {"name": "Универсальные", "description": "fallback", "urls": ["https://raw.githubusercontent.com/barry-far/V2ray-Config/main/Splitted-By-Protocol/vless.txt"]},
}

AGGREGATED_SUBS = {
    "BLACK_FULL": {"filename": "BLACK_FULL.txt", "profile_title": "Free VPN • Crimson — Black", "source_keys": ["black_all", "black_mobile", "ss_black", "extra_etoneya_black", "extra_bolt", "extra_vercel"], "description": "Чёрные"},
    "WHITE_FULL": {"filename": "WHITE_FULL.txt", "profile_title": "Free VPN • Crimson — White", "source_keys": ["white_cidr_all", "white_cidr_checked", "white_mobile", "white_sni", "extra_zieng2", "extra_etoneya_white", "extra_bye2", "extra_cid", "extra_wrtrmmu", "extra_sbornik"], "description": "Белые"},
    "FULL": {"filename": "FULL.txt", "profile_title": "Free VPN • Crimson — Full", "source_keys": ["black_all", "black_mobile", "ss_black", "white_cidr_all", "white_cidr_checked", "white_mobile", "white_sni", "extra_zieng2", "extra_etoneya_white", "extra_bye2", "extra_etoneya_black", "extra_cid", "extra_wrtrmmu", "extra_vercel", "extra_bolt", "extra_sbornik"], "description": "Полный"},
    "CUSTOM_100": {"filename": "CUSTOM_100.txt", "profile_title": "Free VPN • Crimson — Custom 100", "source_keys": [], "description": "Custom 100"},
}
AGGREGATED_SUBS["COMBINED"] = AGGREGATED_SUBS["FULL"]

# Минималистичные тексты
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
<b>«Собрать подписку»</b> — 100 VLESS в одной ссылке

<b>Как подключить:</b>
1. Выбери список → скопируй ссылку
2. Вставь в Happ / Streisand / v2rayNG
3. Обнови → подключись

Вопросы — @unnervin"""

SOURCES_TEXT = """<b>Источники</b> — igareck, zieng2, etoneya и другие
Только <b>VLESS</b>, остальное вырезано
Делим по 300"""
