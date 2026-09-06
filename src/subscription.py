import os
import re
import base64
import qrcode
from io import BytesIO
from datetime import datetime, timezone, timedelta
from parser import make_subscription_content

MSK = timezone(timedelta(hours=3))
BOT_USERNAME = "@wtfparsbot"

def msk_now_str():
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК")

def msk_igareck_str():
    return datetime.now(MSK).strftime("%Y-%m-%d / %H:%M (Moscow)")

def generate_header(profile_title: str, count: int) -> str:
    return "\n".join([
        f"# profile-title: {profile_title}",
        f"# profile-update-interval: 1",
        f"# subscription-userinfo: upload=0; download=0; total=10737418240000000; expire=2546249531",
        f"# Date/Time: {msk_now_str()}",
        f"# Количество: {count}",
        f"# Bot: {BOT_USERNAME}",
        "",
    ])

def generate_igareck_style_header(profile_title: str, count: int) -> str:
    # Минималистичная шапка в стиле igareck, но с @wtfparsbot
    return "\n".join([
        f"# profile-title: {profile_title}",
        f"# profile-update-interval: 1",
        f"# Date/Time: {msk_igareck_str()}",
        f"# Количество: {count}",
        f"# For more info — {BOT_USERNAME}",
        "",
        "# Только VLESS. URI проверены; дубликаты удалены.",
        "# VLESS only. URIs validated; duplicates removed.",
        "",
    ])

def generate_aggregated_content(profile_title: str, configs: list) -> str:
    header = generate_igareck_style_header(profile_title, len(configs))
    return make_subscription_content(configs, header)

def save_subscription_files(base_dir: str, category_key: str, configs: list, profile_title: str, use_igareck_header: bool = False):
    # Работаем только с опубликованными .txt-файлами.
    os.makedirs(base_dir, exist_ok=True)
    header = generate_igareck_style_header(profile_title, len(configs)) if use_igareck_header else generate_header(profile_title, len(configs))
    plain_content = make_subscription_content(configs, header)
    b64_content = base64.b64encode(plain_content.encode('utf-8')).decode('utf-8')
    plain_path = os.path.join(base_dir, f"{category_key}.txt")
    # Atomic replacement prevents readers from observing a half-written feed.
    temp_path = f"{plain_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(plain_content)
    os.replace(temp_path, plain_path)
    b64_path = os.path.join(base_dir, f"{category_key}_base64.txt")
    # не пишем base64 файл, возвращаем путь для совместимости
    return plain_path, b64_path, plain_content, b64_content

def save_aggregated_file(base_dir: str, filename: str, profile_title: str, configs: list):
    # Только .txt на репо, без base64 файлов
    os.makedirs(base_dir, exist_ok=True)
    content = generate_aggregated_content(profile_title, configs)
    path = os.path.join(base_dir, filename)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(temp_path, path)
    b64_path = os.path.join(base_dir, filename.replace(".txt", "_base64.txt"))
    b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    # base64 файл не создаем физически, только .txt
    return path, b64_path, content, b64

CHUNK_SIZE = 300

# Протоколы — теперь только VLESS (по ТЗ вырезать все остальные)
PROTOCOLS = ["vless"]

PROTOCOL_LABELS = {"vless": "VLESS"}


def detect_protocol(link: str) -> str:
    return "vless" if link.lower().strip().startswith("vless://") else "unknown"

def filter_by_protocol(configs: list, proto: str) -> list:
    if proto == "all":
        return list(configs)
    return [c for c in configs if detect_protocol(c) == proto]

def chunk_configs(configs: list, size: int = CHUNK_SIZE):
    for i in range(0, len(configs), size):
        yield configs[i:i+size]

def save_aggregated_chunks(
    base_dir: str,
    filename: str,
    profile_title: str,
    configs: list,
    chunk_size: int = CHUNK_SIZE,
):
    """Write an aggregate and its numbered chunks without deleting old files.

    Obsolete chunks are removed only after all new aggregates are generated and
    the in-memory keyboard map is switched. This prevents a button from briefly
    pointing at a chunk that was already deleted during a concurrent refresh.
    """
    os.makedirs(base_dir, exist_ok=True)
    base_name = filename.replace(".txt", "")
    full_path, full_b64, full_content, full_b64c = save_aggregated_file(
        base_dir, filename, profile_title, configs
    )
    chunk_infos = []
    for idx, chunk in enumerate(chunk_configs(configs, chunk_size), 1):
        chunk_title = f"{profile_title} — {idx}"
        chunk_filename = f"{base_name}_{idx}.txt"
        _, _, chunk_content, _ = save_aggregated_file(
            base_dir, chunk_filename, chunk_title, chunk
        )
        chunk_infos.append((chunk_filename, chunk_title, len(chunk), chunk_content))
    return full_path, full_b64, full_content, full_b64c, chunk_infos


def cleanup_stale_aggregate_chunks(
    base_dir: str,
    aggregate_filenames: list,
    active_filenames: set,
) -> list:
    """Delete numbered aggregate chunks that are no longer in the active map."""
    removed = []
    for filename in aggregate_filenames:
        base_name = filename.removesuffix(".txt")
        chunk_pattern = re.compile(rf"^{re.escape(base_name)}_\d+\.txt$")
        for existing in os.listdir(base_dir):
            if chunk_pattern.fullmatch(existing) and existing not in active_filenames:
                try:
                    os.remove(os.path.join(base_dir, existing))
                    removed.append(existing)
                except OSError:
                    pass
    return removed

def generate_qr_bytes(text: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def get_subscription_stats(configs: list) -> str:
    if not configs:
        return "0 конфигов"
    hosts = set()
    for c in configs:
        try:
            host = c.split("@", 1)[1].split(":", 1)[0]
            hosts.add(host)
        except:
            pass
    return f"{len(configs)} конфигов · {len(hosts)} серверов"
