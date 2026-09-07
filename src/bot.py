import os
import asyncio
import hashlib
import html
import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import (
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import KeyboardButtonStyle, MessageEntityType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from ui import button as build_ui_button, icon_text, is_main_menu_text, render_html
from parser import (
    deduplicate_configs,
    fetch_all,
    find_public_github_candidates,
    inspect_public_github_repository,
    inspect_public_github_urls,
    is_valid_vless,
    parse_vless_info,
    is_valid_any,
    validate_configs,
)
from provider_registry import (
    MAX_DYNAMIC_PROVIDERS,
    REGISTRY_FILE,
    build_provider_record,
    load_provider_registry,
    normalize_github_raw_url,
    parse_github_repository_url,
    registry_content,
    save_provider_registry,
)
from subscription import (
    CHUNK_SIZE,
    cleanup_stale_aggregate_chunks,
    save_aggregated_chunks,
    save_subscription_files,
)
try:
    from health import start_health_server
except ImportError:
    start_health_server = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ASSETS_DIR = Path(__file__).parent.parent / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

CACHE = {}
LAST_UPDATE = None
LAST_NOTIFY = None  # timestamp of the latest channel update notice
AGGREGATED_CACHE = {}
AGGREGATED_CHUNKS = {}
AGGREGATED_PROTO_COUNTS = {}
UPDATE_LOCK = asyncio.Lock()
PROVIDER_CANDIDATES = {}
MAX_PROVIDER_CANDIDATES = 100
USERS_FILE = DATA_DIR / "users.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "update_notifications": True,
    "last_update_notification_id": None,
}

MSK = timezone(timedelta(hours=3))


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(
                    {
                        key: stored[key]
                        for key in DEFAULT_SETTINGS
                        if key in stored
                    }
                )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("load settings failed: %s", exc)
    return settings


def save_settings():
    try:
        temporary = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.tmp")
        temporary.write_text(
            json.dumps(SETTINGS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(SETTINGS_FILE)
    except OSError as exc:
        logger.error("save settings failed: %s", exc)


SETTINGS = load_settings()


def update_notifications_enabled() -> bool:
    return bool(SETTINGS.get("update_notifications", True))


def set_update_notifications(enabled: bool):
    SETTINGS["update_notifications"] = bool(enabled)
    save_settings()


def ui_button(icon: str, text: str, **kwargs):
    """Use a configured custom emoji ID, or the Unicode fallback."""
    kwargs.setdefault("custom_emoji_id", config.CUSTOM_EMOJI_IDS.get(icon))
    return build_ui_button(icon, text, **kwargs)


REPLY_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(icon_text("home", "Главное меню"), style=KeyboardButtonStyle.PRIMARY)]],
    resize_keyboard=True,
    is_persistent=True,
)

# ---------- Users (для даты регистрации) ----------

def load_users():
    try:
        if USERS_FILE.exists():
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"load users failed: {e}")
    return {}

def save_users(users):
    try:
        USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"save users failed: {e}")

def get_or_create_user(user_id: int, username: str = "", first_name: str = ""):
    users = load_users()
    uid_str = str(user_id)
    if uid_str not in users:
        users[uid_str] = {
            "username": username or "",
            "first_name": first_name or "",
            "registration_date": datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК"),
            "first_seen": datetime.now(MSK).isoformat()
        }
        save_users(users)
    else:
        if username and users[uid_str].get("username") != username:
            users[uid_str]["username"] = username
            save_users(users)
        # обновляем формат даты если старый
        old_date = users[uid_str].get("registration_date", "")
        if old_date and "МСК" not in old_date:
            # оставляем как есть, но новые будут с временем
            pass
    return users[uid_str]

# ---------- Keyboards ----------

def main_keyboard(user_id: int = None):
    kb = [
        [ui_button("profile", "«Профиль»", callback_data="profile", style=KeyboardButtonStyle.PRIMARY)],
        [
            ui_button("white", "«Белые списки»", callback_data="white", style=KeyboardButtonStyle.SUCCESS),
            ui_button("black", "«Черные списки»", callback_data="black", style=KeyboardButtonStyle.SUCCESS),
        ],
        [ui_button("full", "«Полный список»", callback_data="full", style=KeyboardButtonStyle.SUCCESS)],
        [ui_button("help", "«Помощь»", callback_data="help", style=KeyboardButtonStyle.DANGER)],
        [ui_button("chat", "«Поддержка»", callback_data="support", style=KeyboardButtonStyle.DANGER)],
    ]
    if user_id and config.is_admin(user_id):
        kb.append([
            ui_button("admin", "«Админ панель»", callback_data="admin_panel", style=KeyboardButtonStyle.DANGER)
        ])
    return InlineKeyboardMarkup(kb)


def admin_keyboard():
    notifications_on = update_notifications_enabled()
    notification_label = (
        "«Уведомления: ВКЛ»" if notifications_on else "«Уведомления: ВЫКЛ»"
    )
    notification_style = (
        KeyboardButtonStyle.SUCCESS if notifications_on else KeyboardButtonStyle.DANGER
    )
    return InlineKeyboardMarkup(
        [
            [
                ui_button("stats", "«Статистика»", callback_data="admin_stats", style=KeyboardButtonStyle.PRIMARY),
                ui_button("refresh", "«Обновить кэш»", callback_data="admin_refresh", style=KeyboardButtonStyle.SUCCESS),
            ],
            [ui_button("clean", "«Проверка и очистка»", callback_data="admin_clean", style=KeyboardButtonStyle.DANGER)],
            [ui_button(
                "notifications",
                notification_label,
                callback_data="admin_notifications",
                style=notification_style,
            )],
            [
                ui_button("sources", "«Провайдеры»", callback_data="admin_providers", style=KeyboardButtonStyle.PRIMARY),
                ui_button("search", "«Поиск источников»", callback_data="admin_discovery", style=KeyboardButtonStyle.SUCCESS),
            ],
            [ui_button("info", "«О текущих источниках»", callback_data="admin_sources", style=KeyboardButtonStyle.PRIMARY)],
            [ui_button("back", "«Назад»", callback_data="home", style=KeyboardButtonStyle.PRIMARY)],
        ]
    )


def sub_required_keyboard():
    return InlineKeyboardMarkup(
        [
            [ui_button("subscribe", "«Подписаться на канал»", url=config.CHANNEL_LINK, style=KeyboardButtonStyle.PRIMARY)],
            [ui_button("check", "«Проверить подписку»", callback_data="check_sub", style=KeyboardButtonStyle.SUCCESS)],
        ]
    )


def back_keyboard(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ui_button("back", "«Назад»", callback_data=callback_data, style=KeyboardButtonStyle.PRIMARY)]
    ])


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ui_button(
            "back",
            "«Закрыть поддержку»",
            callback_data="support_close",
            style=KeyboardButtonStyle.PRIMARY,
        )]
    ])


def support_reply_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ui_button(
            "chat",
            "«Ответить»",
            callback_data=f"support_reply:{user_id}",
            style=KeyboardButtonStyle.SUCCESS,
        )]
    ])


def clear_support_state(context):
    context.user_data.pop("support_mode", None)
    context.user_data.pop("support_reply_to", None)


def clear_provider_input_state(context):
    context.user_data.pop("provider_add_mode", None)


def dynamic_provider_records() -> list[dict]:
    return load_provider_registry(REGISTRY_FILE)


def providers_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [ui_button("add", "«Добавить по ссылке»", callback_data="provider_add_manual", style=KeyboardButtonStyle.SUCCESS)],
        [ui_button("search", "«Найти публичные источники»", callback_data="admin_discovery", style=KeyboardButtonStyle.SUCCESS)],
    ]
    for record in dynamic_provider_records()[:20]:
        icon = "enabled" if record.get("enabled", True) else "disabled"
        category = "⬜" if record.get("category") == "white" else "⬛"
        label = f"«{category} {record['name'][:32]}»"
        rows.append([
            ui_button(
                icon,
                label,
                callback_data=f"provider_view:{record['id']}",
                style=(
                    KeyboardButtonStyle.SUCCESS
                    if record.get("enabled", True)
                    else KeyboardButtonStyle.DANGER
                ),
            )
        ])
    rows.append([ui_button("back", "«Назад»", callback_data="admin_panel", style=KeyboardButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(rows)


def provider_detail_keyboard(record: dict) -> InlineKeyboardMarkup:
    enabled = bool(record.get("enabled", True))
    rows = [
        [ui_button("vless", "«Открыть GitHub-файл»", url=record["urls"][0], style=KeyboardButtonStyle.PRIMARY)],
        [ui_button(
            "disabled" if enabled else "enabled",
            "«Выключить»" if enabled else "«Включить»",
            callback_data=f"provider_toggle:{record['id']}",
            style=KeyboardButtonStyle.DANGER if enabled else KeyboardButtonStyle.SUCCESS,
        )],
        [ui_button(
            "delete",
            "«Удалить»",
            callback_data=f"provider_delete_confirm:{record['id']}",
            style=KeyboardButtonStyle.DANGER,
        )],
        [ui_button("back", "«Назад»", callback_data="admin_providers", style=KeyboardButtonStyle.PRIMARY)],
    ]
    return InlineKeyboardMarkup(rows)


def provider_candidate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            ui_button("white", "«Добавить в белые»", callback_data=f"provider_accept:{token}:white", style=KeyboardButtonStyle.SUCCESS),
            ui_button("black", "«Добавить в черные»", callback_data=f"provider_accept:{token}:black", style=KeyboardButtonStyle.SUCCESS),
        ],
        [ui_button("skip", "«Пропустить»", callback_data=f"provider_skip:{token}", style=KeyboardButtonStyle.DANGER)],
    ])


def remember_provider_candidate(candidate: dict) -> str:
    token = hashlib.sha256(candidate["url"].encode("utf-8")).hexdigest()[:16]
    PROVIDER_CANDIDATES[token] = dict(candidate)
    while len(PROVIDER_CANDIDATES) > MAX_PROVIDER_CANDIDATES:
        PROVIDER_CANDIDATES.pop(next(iter(PROVIDER_CANDIDATES)))
    return token


def provider_by_id(provider_id: str) -> dict | None:
    return next(
        (record for record in dynamic_provider_records() if record.get("id") == provider_id),
        None,
    )


async def persist_dynamic_providers(records, commit_message: str) -> tuple[bool, str]:
    """Persist in GitHub when possible and always activate a valid local registry."""
    published = ""
    publication_error = ""
    try:
        if config.GITHUB_TOKEN and config.GITHUB_REPO:
            from github_sync import push_text_file

            published = await push_text_file(
                "providers.json",
                registry_content(records),
                config.GITHUB_REPO,
                config.GITHUB_TOKEN,
                config.GITHUB_BRANCH,
                commit_message=commit_message,
            )
            if not published:
                publication_error = "GitHub не принял обновление providers.json"
        else:
            publication_error = "GITHUB_TOKEN с Contents read/write не настроен"

        save_provider_registry(records, REGISTRY_FILE)
        normalized = load_provider_registry(REGISTRY_FILE)
        previous_dynamic = {key for key in CACHE if key.startswith("dynamic_")}
        config.apply_dynamic_providers(normalized)
        for key in previous_dynamic - set(config.SOURCES):
            CACHE.pop(key, None)
        if published:
            return True, published
        logger.warning("Provider registry is local-only: %s", publication_error)
        return True, f"local-only: {publication_error}"
    except Exception as exc:
        logger.exception("provider registry persistence failed")
        return False, str(exc)[:300]


def chunks_keyboard(
    base_filename: str,
    chunk_list,
    back_data: str = "home",
    back_label: str = "«Назад»",
):
    rows = []
    for _, (cfname, _, cnt) in enumerate(chunk_list, 1):
        short = cfname.replace(".txt", "")
        chunk_button = ui_button(
            "chunk",
            f"«{short} · {cnt}»",
            callback_data=f"chunk:{cfname}",
            style=KeyboardButtonStyle.PRIMARY,
        )
        if not rows or len(rows[-1]) == 2:
            rows.append([chunk_button])
        else:
            rows[-1].append(chunk_button)
    rows.append([
        ui_button(
            "download",
            "«Скачать полный файл»",
            callback_data=f"rawfile:{base_filename}",
            style=KeyboardButtonStyle.SUCCESS,
        )
    ])
    rows.append([
        ui_button("back", back_label, callback_data=back_data, style=KeyboardButtonStyle.PRIMARY)
    ])
    return InlineKeyboardMarkup(rows)


def aggregate_for_filename(filename: str):
    """Resolve an aggregate or numbered chunk, including old button names."""
    for aggregate_key, aggregate in config.AGGREGATED_SUBS.items():
        base_filename = aggregate["filename"]
        base_name = base_filename.removesuffix(".txt")
        if filename == base_filename:
            return aggregate_key, aggregate
        prefix = f"{base_name}_"
        if filename.startswith(prefix) and filename.endswith(".txt"):
            number = filename[len(prefix):-4]
            if number.isdigit():
                return aggregate_key, aggregate
    return None, None


def aggregate_back_callback(filename: str) -> str:
    aggregate_key, _ = aggregate_for_filename(filename)
    if aggregate_key:
        candidate = aggregate_key.lower().replace("_full", "")
        if candidate in {"white", "black", "full"}:
            return candidate
    return "home"


def protocol_keyboard(agg_key: str):
    agg = config.AGGREGATED_SUBS.get(agg_key)
    if not agg:
        return InlineKeyboardMarkup([
            [ui_button("back", "«Назад»", callback_data="home", style=KeyboardButtonStyle.PRIMARY)]
        ])
    base = agg["filename"]
    total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
    return InlineKeyboardMarkup(
        [
            [ui_button(
                "vless",
                f"«VLESS · {total}»",
                callback_data=f"proto:{agg_key}:all",
                style=KeyboardButtonStyle.SUCCESS,
            )],
            [ui_button("back", "«Назад»", callback_data="home", style=KeyboardButtonStyle.PRIMARY)],
        ]
    )

# ---------- Channel subscription check ----------

async def is_user_subscribed(user_id: int, bot) -> bool:
    try:
        chat_id = config.REQUIRED_CHANNEL
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator', 'owner']
    except Exception as e:
        err_str = str(e).lower()
        if "not enough rights" in err_str or "chat not found" in err_str or "forbidden" in err_str:
            logger.warning(f"Cannot check subscription for {user_id}: {e} — allowing")
            return True
        logger.warning(f"Sub check failed for {user_id}: {e}")
        return True

# ---------- Aggregated ----------

def build_aggregated_configs():
    results = {}
    chunk_map = {}
    proto_counts_map = {}
    seen_filenames = set()
    for agg_key, agg in config.AGGREGATED_SUBS.items():
        filename = agg["filename"]
        if filename in seen_filenames:
            continue
        seen_filenames.add(filename)
        title = agg["profile_title"]
        source_keys = agg["source_keys"]
        all_cfgs = []
        seen = set()
        for sk in source_keys:
            data = CACHE.get(sk, {})
            for c in data.get("configs", []):
                if not c.lower().startswith("vless://"):
                    continue
                # проверка на рабочие конфиги
                ok, _ = is_valid_any(c)
                if not ok:
                    continue
                if c not in seen:
                    seen.add(c)
                    all_cfgs.append(c)
        # Providers often publish the same endpoint with another remark or
        # parameter order. Deduplicate by normalized VLESS identity globally.
        all_cfgs = deduplicate_configs(all_cfgs)
        try:
            _, full_content, chunk_infos = save_aggregated_chunks(
                str(DATA_DIR), filename, title, all_cfgs, CHUNK_SIZE
            )
            results[filename] = {"content": full_content, "count": len(all_cfgs), "configs": all_cfgs}
            chunk_list = []
            for cfname, ctitle, cnt, ccontent in chunk_infos:
                results[cfname] = {"content": ccontent, "count": cnt, "configs": [], "is_chunk": True}
                chunk_list.append((cfname, ctitle, cnt))
            chunk_map[filename] = chunk_list
            proto_counts_map[filename] = {"vless": len(all_cfgs)}
        except Exception as e:
            logger.error(f"aggregated build {filename} error: {e}")
    return results, chunk_map, proto_counts_map


def activate_aggregated_configs(results, chunk_map, proto_counts_map):
    """Atomically expose one complete generated/publication generation."""
    global AGGREGATED_CACHE, AGGREGATED_CHUNKS, AGGREGATED_PROTO_COUNTS
    new_cache = {
        filename: {
            "count": info["count"],
            "content": info["content"],
        }
        for filename, info in results.items()
    }

    # No await occurs in this function, so handlers observe either the old map
    # or the complete new map, never a partially switched generation.
    AGGREGATED_CACHE = new_cache
    AGGREGATED_CHUNKS = chunk_map
    AGGREGATED_PROTO_COUNTS = proto_counts_map
    removed = cleanup_stale_aggregate_chunks(
        str(DATA_DIR),
        list(chunk_map),
        set(results),
    )
    if removed:
        logger.info("Removed stale local chunks: %s", ", ".join(sorted(removed)))


async def push_aggregated_to_github(aggregated_results):
    if not config.GITHUB_TOKEN or not config.GITHUB_REPO:
        return {}
    try:
        from github_sync import push_aggregated_subscriptions
        to_push = {}
        for filename, info in aggregated_results.items():
            path = f"{config.GITHUB_SUB_PATH}/{filename}" if config.GITHUB_SUB_PATH else filename
            path = path.lstrip("/")
            to_push[path] = info["content"]
        return await push_aggregated_subscriptions(
            to_push,
            config.GITHUB_REPO,
            config.GITHUB_TOKEN,
            config.GITHUB_BRANCH,
        )
    except Exception as e:
        logger.error(f"push error: {e}")
        return {}

async def notify_channel_update(bot, old_total, new_total):
    """Replace the previous channel update notice with the newest one."""
    global LAST_NOTIFY
    if not config.CHANNEL_ID or not update_notifications_enabled():
        return

    now = datetime.now(MSK)
    text = f"✅  • Списки обновлены\n\n🕔{now.strftime('%d.%m.%Y %H:%M МСК')}"
    previous_id = SETTINGS.get("last_update_notification_id")
    try:
        sent = await bot.send_message(
            chat_id=config.CHANNEL_ID,
            text=render_html(text, config.CUSTOM_EMOJI_IDS),
            parse_mode=ParseMode.HTML,
        )
        new_message_id = getattr(sent, "message_id", None)
        if previous_id and previous_id != new_message_id:
            try:
                await bot.delete_message(
                    chat_id=config.CHANNEL_ID,
                    message_id=int(previous_id),
                )
            except Exception as exc:
                logger.warning("Could not delete previous update notice: %s", exc)
        SETTINGS["last_update_notification_id"] = new_message_id
        save_settings()
        LAST_NOTIFY = now
        logger.info(
            "Replaced channel update notice for %s (%s -> %s VLESS)",
            config.CHANNEL_ID,
            old_total,
            new_total,
        )
    except Exception as exc:
        logger.warning("Channel notify failed: %s", exc)

async def update_cache(categories=None, mode=None, bot=None):
    """Serialize refreshes so cache, files, and rendered chunk buttons agree."""
    async with UPDATE_LOCK:
        return await _update_cache(categories=categories, mode=mode, bot=bot)


async def _update_cache(categories=None, mode=None, bot=None):
    global CACHE, LAST_UPDATE
    mode = mode or config.CHECK_MODE
    if categories is None:
        categories = list(config.SOURCES.keys())
    old_total = sum(len(v.get("configs", [])) for v in CACHE.values()) if CACHE else 0
    result = await fetch_all(mode=mode, categories=categories)
    for key, data in result.items():
        # A provider outage must not erase a previously healthy source. Recheck
        # its cached VLESS endpoints using the requested mode and retain only
        # those that still pass.
        if not data.get("raw_total") and data.get("errors") and CACHE.get(key, {}).get("configs"):
            cached = CACHE[key]["configs"]
            fallback = await validate_configs(cached, mode=mode)
            data["configs"] = fallback
            data["filtered_total"] = len(fallback)
            data["removed"] = len(cached) - len(fallback)
            data["cache_fallback"] = True

        # Только .txt и только VLESS, сразу фильтруем нерабочие
        filtered = []
        seen = set()
        for c in data.get("configs", []):
            if not c.lower().startswith("vless://"):
                continue
            if c in seen:
                continue
            ok, _ = is_valid_any(c)
            if not ok:
                continue
            seen.add(c)
            filtered.append(c)
        data["configs"] = filtered
        title = config.SOURCES.get(key, {}).get("name", key)
        if filtered:
            try:
                save_subscription_files(str(DATA_DIR), key, filtered, title)
            except Exception as e:
                logger.error(f"save error {key}: {e}")
        CACHE[key] = data
    LAST_UPDATE = datetime.now(MSK)
    new_total = sum(len(v.get("configs", [])) for v in CACHE.values())
    try:
        agg, chunk_map, proto_counts_map = build_aggregated_configs()
        if config.GITHUB_TOKEN and agg:
            # Keep the previous keyboard generation active until all new files
            # become visible together in one GitHub ref update.
            await push_aggregated_to_github(agg)
        activate_aggregated_configs(agg, chunk_map, proto_counts_map)
        # Every completed refresh replaces the previous channel notice.
        if bot:
            await notify_channel_update(bot, old_total, new_total)
    except Exception as e:
        logger.error(f"aggregated error: {e}")
    return result

def local_subscription_path(filename: str):
    """Return an active generated file or, before preload, a bootstrap copy."""
    _, aggregate = aggregate_for_filename(filename)
    if aggregate and AGGREGATED_CACHE and filename not in AGGREGATED_CACHE:
        # The name belongs to an old aggregate generation. Do not silently
        # serve a stale committed chunk after the active map has switched.
        return None
    for path in (DATA_DIR / filename, Path(__file__).parent.parent / filename):
        if path.is_file():
            return path
    return None


# ---------- Media helpers — редактируем одно сообщение ----------

def get_banner_path(name: str) -> Path:
    return ASSETS_DIR / f"banner_{name}.png"

async def edit_message_with_banner(query, banner_name: str, text: str, reply_markup):
    text = render_html(text, config.CUSTOM_EMOJI_IDS)
    banner_path = get_banner_path(banner_name)
    try:
        if banner_path.exists():
            if query.message.photo:
                with open(banner_path, 'rb') as f:
                    media = InputMediaPhoto(media=f, caption=text, parse_mode=ParseMode.HTML)
                    await query.message.edit_media(media=media, reply_markup=reply_markup)
                    return
            else:
                try:
                    with open(banner_path, 'rb') as f:
                        media = InputMediaPhoto(media=f, caption=text, parse_mode=ParseMode.HTML)
                        await query.message.edit_media(media=media, reply_markup=reply_markup)
                        return
                except Exception:
                    pass
                try:
                    await query.message.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                    return
                except Exception:
                    pass
                try:
                    await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                    return
                except Exception:
                    pass
        else:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                return
            except:
                try:
                    await query.message.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                    return
                except:
                    pass
    except Exception as e:
        logger.error(f"edit with banner {banner_name} failed: {e}")
    try:
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except:
        try:
            await query.message.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"final fallback failed: {e}")

async def send_initial_banner(update: Update, banner_name: str, text: str, reply_markup):
    text = render_html(text, config.CUSTOM_EMOJI_IDS)
    banner_path = get_banner_path(banner_name)
    if banner_path.exists():
        try:
            await update.message.reply_photo(
                photo=open(banner_path, 'rb'),
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            logger.error(f"send banner {banner_name} failed: {e}")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_support_state(context)
    clear_provider_input_state(context)
    uid = update.effective_user.id if update.effective_user else None
    user = update.effective_user
    get_or_create_user(uid, user.username if user else "", user.first_name if user else "")

    if not await is_user_subscribed(uid, context.bot):
        text = (
            f"<b>🔒 Доступ только по подписке</b>\n\n"
            f"📢 Подпишись на канал {config.CHANNEL_USERNAME}, чтобы пользоваться ботом\n\n"
            f"✅ После подписки нажми «Проверить подписку»"
        )
        await send_initial_banner(update, "main", text, sub_required_keyboard())
        return

    await update.message.reply_text(
        render_html("✅ Клавиатура обновлена — жми «🏠 Главное меню» внизу", config.CUSTOM_EMOJI_IDS),
        parse_mode=ParseMode.HTML,
        reply_markup=REPLY_MENU,
    )
    await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_support_state(context)
    clear_provider_input_state(context)
    uid = update.effective_user.id if update.effective_user else None
    if not await is_user_subscribed(uid, context.bot):
        await update.message.reply_text(f"📢 Подпишись на {config.CHANNEL_USERNAME}, чтобы продолжить", reply_markup=sub_required_keyboard())
        return
    await send_initial_banner(update, "help", config.HELP_TEXT, main_keyboard(uid))


def extract_custom_emoji_ids(message) -> list[str]:
    """Read custom emoji identifiers from a message sent or forwarded to the bot."""
    result = []
    sticker = getattr(message, "sticker", None)
    sticker_custom_id = getattr(sticker, "custom_emoji_id", None)
    if sticker_custom_id:
        result.append(sticker_custom_id)
    entities = [
        *list(getattr(message, "entities", None) or []),
        *list(getattr(message, "caption_entities", None) or []),
    ]
    for entity in entities:
        if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
            if entity.custom_emoji_id not in result:
                result.append(entity.custom_emoji_id)
    return result


async def handle_main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_main_menu_text(update.message.text):
        clear_support_state(context)
        clear_provider_input_state(context)
        uid = update.effective_user.id if update.effective_user else None
        if not await is_user_subscribed(uid, context.bot):
            await update.message.reply_text(f"📢 Подпишись на {config.CHANNEL_USERNAME}", reply_markup=sub_required_keyboard())
            return True
        await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return True
    return False

async def show_outdated_file(query, fname: str, back_data: str = "home"):
    _, aggregate = aggregate_for_filename(fname)
    if aggregate:
        base_filename = aggregate["filename"]
        current_chunks = AGGREGATED_CHUNKS.get(base_filename, [])
        text = (
            f"<b>⚠️ {fname} больше не существует</b>\n\n"
            "Количество конфигураций изменилось, поэтому пакеты были пересобраны. "
            "Выбери актуальный пакет ниже."
        )
        keyboard = (
            chunks_keyboard(base_filename, current_chunks, back_data=back_data)
            if current_chunks
            else back_keyboard(back_data)
        )
    else:
        text = "<b>⚠️ Файл больше не существует</b>\n\nОткрой список заново."
        keyboard = back_keyboard(back_data)
    await edit_message_with_banner(query, "configs", text, keyboard)


async def send_chunk_file(query, fname, back_data="home"):
    path = local_subscription_path(fname)
    if path is None:
        await query.message.reply_text("🔄 Файл изменился, обновляю список пакетов…")
        await update_cache(bot=query.get_bot() if hasattr(query, "get_bot") else None)
        path = local_subscription_path(fname)

    if path is None:
        await show_outdated_file(query, fname, back_data)
        return

    _, aggregate = aggregate_for_filename(fname)
    title = fname
    if aggregate:
        title = aggregate["profile_title"]
        if aggregate["filename"] != fname:
            title = f"{title} — {fname}"
    cnt = AGGREGATED_CACHE.get(fname, {}).get("count", "?")
    text = (
        f"<b>📦 {title}</b>\n\n"
        f"🔗 Конфигов в файле: <b>{cnt}</b>\n\n"
        f"{config.FILE_USAGE_TEXT}"
    )
    kb = InlineKeyboardMarkup(
        [
            [ui_button(
                "download",
                "«Скачать .txt»",
                callback_data=f"rawfile:{fname}",
                style=KeyboardButtonStyle.SUCCESS,
            )],
            [ui_button("back", "«Назад»", callback_data=back_data, style=KeyboardButtonStyle.PRIMARY)],
        ]
    )
    await edit_message_with_banner(query, "configs", text, kb)
    try:
        with open(path, "rb") as document:
            await query.message.reply_document(
                document=document,
                filename=fname,
                caption=f"{title} • {cnt}",
            )
    except Exception as exc:
        logger.error("send chunk %s failed: %s", fname, exc)

async def handle_admin_clean(query):
    try:
        await query.message.edit_caption(
            caption="Проверяю источники, синтаксис и доступность каждого VLESS endpoint...",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        try:
            await query.message.edit_text(
                "Проверяю источники, синтаксис и доступность каждого VLESS endpoint..."
            )
        except Exception:
            pass

    total_before = sum(len(data.get("configs", [])) for data in CACHE.values())
    try:
        # This is a real refresh, not a second syntax pass over stale CACHE.
        # ``tcp`` first applies strict VLESS validation, then checks every
        # unique host:port and removes configs on unreachable endpoints.
        result = await update_cache(mode="tcp", bot=query.get_bot())
    except Exception as exc:
        logger.exception("admin validation and cleanup failed")
        await edit_message_with_banner(
            query,
            "main",
            f"<b>Проверка не завершена</b>\n\nОшибка: <code>{str(exc)[:300]}</code>",
            InlineKeyboardMarkup(
                [
                    [ui_button("back", "«Назад»", callback_data="admin_panel", style=KeyboardButtonStyle.PRIMARY)],
                    [ui_button("home", "«Главное меню»", callback_data="home", style=KeyboardButtonStyle.PRIMARY)],
                ]
            ),
        )
        return

    total_after = sum(len(data.get("configs", [])) for data in CACHE.values())
    details = []
    unavailable = []
    for key, data in result.items():
        raw_total = data.get("raw_total", 0)
        kept = len(CACHE.get(key, {}).get("configs", []))
        removed = data.get("removed", max(0, raw_total - kept))
        if removed:
            details.append(f"{key}: {raw_total} → {kept}")
        if data.get("errors"):
            marker = " (проверен старый кэш)" if data.get("cache_fallback") else ""
            unavailable.append(f"{key}{marker}")

    report = [
        "<b>Проверка и очистка завершена</b>",
        "",
        "Источники загружены заново.",
        "Проверено: строгий VLESS URI + TCP host:port.",
        f"В кэше было: <b>{total_before}</b>",
        f"В кэше стало: <b>{total_after}</b>",
    ]
    if details:
        report.extend(["", "<b>Изменения:</b>", *details[:20]])
    if unavailable:
        report.extend(["", "<b>Ошибки/недоступные зеркала:</b>", ", ".join(unavailable[:20])])
    if not details and not unavailable:
        report.extend(["", "Все конфигурации прошли проверку."])

    await edit_message_with_banner(
        query,
        "main",
        "\n".join(report),
        InlineKeyboardMarkup(
            [
                [ui_button("back", "«Назад»", callback_data="admin_panel", style=KeyboardButtonStyle.PRIMARY)],
                [ui_button("home", "«Главное меню»", callback_data="home", style=KeyboardButtonStyle.PRIMARY)],
            ]
        ),
    )

def provider_panel_text() -> str:
    records = dynamic_provider_records()
    enabled = sum(1 for record in records if record.get("enabled", True))
    return (
        "<b>🗂️ Динамические провайдеры</b>\n\n"
        "Можно добавить прямой GitHub-файл или публичный GitHub-репозиторий. "
        "Бот принимает только текстовые feed-ы с валидными VLESS.\n\n"
        f"Добавлено: <b>{len(records)}</b> • включено: <b>{enabled}</b>\n\n"
        "Найденные источники не подключаются автоматически: сначала выбери категорию."
    )


async def send_provider_candidates(message, candidates: list[dict], errors=None) -> int:
    configured_urls = {
        url
        for source in config.SOURCES.values()
        for url in source.get("urls", [])
    }
    configured_urls.update(
        url
        for record in dynamic_provider_records()
        for url in record.get("urls", [])
    )
    sent = 0
    for candidate in candidates:
        url = candidate.get("url", "")
        if not url or url in configured_urls:
            continue
        token = remember_provider_candidate(candidate)
        repository = html.escape(str(candidate.get("repository") or "GitHub"))
        filename = html.escape(str(candidate.get("filename") or "feed"))
        safe_url = html.escape(url, quote=True)
        await message.reply_text(
            render_html(
                "<b>🔎 Найден публичный VLESS-источник</b>\n\n"
                f"Репозиторий: <b>{repository}</b>\n"
                f"Файл: <code>{filename}</code>\n"
                f"Валидных VLESS: <b>{int(candidate.get('valid_count', 0))}</b>\n\n"
                f'<a href="{safe_url}">Открыть публичный GitHub-файл</a>',
                config.CUSTOM_EMOJI_IDS,
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=provider_candidate_keyboard(token),
        )
        sent += 1

    if not sent:
        error_text = "; ".join(str(error) for error in (errors or []) if error)
        suffix = f"\n\n<code>{html.escape(error_text[:800])}</code>" if error_text else ""
        await message.reply_text(
            f"Новых подходящих публичных GitHub-источников не найдено.{suffix}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard("admin_providers"),
        )
    return sent


async def handle_manual_provider_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("provider_add_mode"):
        return False
    uid = update.effective_user.id if update.effective_user else 0
    if not config.is_admin(uid):
        clear_provider_input_state(context)
        return False

    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text(
            "Пришли текстовую HTTPS-ссылку на GitHub-файл или репозиторий.",
            reply_markup=back_keyboard("admin_providers"),
        )
        return True

    await update.effective_message.reply_text("🔎 Проверяю публичный GitHub-источник…")
    repository = parse_github_repository_url(text)
    if repository:
        candidates, errors = await inspect_public_github_repository(text, max_results=5)
    else:
        try:
            normalized = normalize_github_raw_url(text)
        except ValueError as exc:
            await update.effective_message.reply_text(
                f"❌ {html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_providers"),
            )
            return True
        candidates = await inspect_public_github_urls(
            [normalized],
            min_valid=1,
            max_results=1,
        )
        errors = [] if candidates else ["Файл не содержит валидных VLESS"]

    clear_provider_input_state(context)
    await send_provider_candidates(update.effective_message, candidates, errors)
    return True


# ---------- Callback ----------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id if query.from_user else 0
    user = query.from_user

    if user:
        get_or_create_user(uid, user.username or "", user.first_name or "")

    if data != "check_sub":
        if not await is_user_subscribed(uid, context.bot):
            await edit_message_with_banner(query, "main",
                f"<b>🔒 Доступ только по подписке</b>\n\n📢 Подпишись на {config.CHANNEL_USERNAME}, чтобы пользоваться ботом",
                sub_required_keyboard())
            return

    if data == "check_sub":
        if await is_user_subscribed(uid, context.bot):
            await edit_message_with_banner(query, "main", config.WELCOME_TEXT, main_keyboard(uid))
        else:
            await query.answer("Ты еще не подписался на канал", show_alert=True)
            await edit_message_with_banner(query, "main",
                f"<b>⚠️ Ты еще не подписался</b>\n\n📢 Подпишись на {config.CHANNEL_USERNAME} и нажми проверку",
                sub_required_keyboard())
        return

    if data == "support":
        clear_provider_input_state(context)
        context.user_data["support_mode"] = True
        context.user_data.pop("support_reply_to", None)
        text = (
            "<b>💬 Поддержка</b>\n\n"
            "Напиши сообщение прямо сюда. Можно отправить текст, фотографию, "
            "документ, видео, голосовое сообщение или стикер.\n\n"
            "Команда поддержки получит обращение, а ответ придёт в этот чат."
        )
        await edit_message_with_banner(query, "help", text, support_keyboard())
        return

    if data == "support_close":
        clear_support_state(context)
        clear_provider_input_state(context)
        await edit_message_with_banner(query, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return

    if data.startswith("support_reply:"):
        clear_provider_input_state(context)
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        try:
            target_user_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await query.answer("Некорректный пользователь", show_alert=True)
            return
        context.user_data["support_reply_to"] = target_user_id
        context.user_data.pop("support_mode", None)
        await query.message.reply_text(
            render_html(
                f"<b>💬 Ответ пользователю</b> <code>{target_user_id}</code>\n\n"
                "Отправь текст, файл, фотографию, видео, голосовое сообщение или стикер. "
                "Бот доставит ответ без раскрытия личного аккаунта администратора.",
                config.CUSTOM_EMOJI_IDS,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard("admin_panel"),
        )
        return

    # Any regular navigation action closes an unfinished support input mode.
    clear_support_state(context)

    if data in {"admin_providers", "provider_add_manual", "admin_discovery"} or data.startswith("provider_"):
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return

        if data == "admin_providers":
            clear_provider_input_state(context)
            await edit_message_with_banner(
                query,
                "main",
                provider_panel_text(),
                providers_keyboard(),
            )
            return

        if data == "provider_add_manual":
            context.user_data["provider_add_mode"] = True
            await edit_message_with_banner(
                query,
                "main",
                "<b>➕ Добавление провайдера</b>\n\n"
                "Пришли одним сообщением:\n"
                "• прямую ссылку на публичный GitHub-файл; или\n"
                "• ссылку на публичный GitHub-репозиторий.\n\n"
                "Для репозитория бот сам проверит ограниченное число наиболее подходящих файлов.",
                back_keyboard("admin_providers"),
            )
            return

        if data == "admin_discovery":
            clear_provider_input_state(context)
            await edit_message_with_banner(
                query,
                "main",
                "<b>🔎 Поиск публичных источников</b>\n\n"
                "Ищу на GitHub по строгим фильтрам VLESS + VPN/config/subscription/list/blacklist. "
                "Ничего не будет добавлено без подтверждения.",
                back_keyboard("admin_providers"),
            )
            candidates, errors = await find_public_github_candidates()
            sent = await send_provider_candidates(query.message, candidates, errors)
            if sent:
                await query.message.reply_text(
                    f"Найдено кандидатов: <b>{sent}</b>. Выбери категорию под каждым источником.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_keyboard("admin_providers"),
                )
            return

        if data.startswith("provider_skip:"):
            token = data.split(":", 1)[1]
            PROVIDER_CANDIDATES.pop(token, None)
            await query.edit_message_reply_markup(reply_markup=back_keyboard("admin_providers"))
            await query.message.reply_text("Источник пропущен.")
            return

        if data.startswith("provider_accept:"):
            try:
                _, token, category = data.split(":", 2)
            except ValueError:
                await query.message.reply_text("Некорректная команда добавления.")
                return
            candidate = PROVIDER_CANDIDATES.get(token)
            if not candidate or category not in {"white", "black"}:
                await query.message.reply_text(
                    "Кандидат устарел. Запусти поиск или добавление по ссылке заново.",
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            verified = await inspect_public_github_urls(
                [candidate["url"]],
                min_valid=1,
                max_results=1,
            )
            if not verified:
                await query.message.reply_text(
                    "Источник больше не содержит валидных VLESS и не был добавлен.",
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            record = build_provider_record(
                candidate["url"],
                category,
                added_at=datetime.now(MSK).isoformat(),
            )
            records = dynamic_provider_records()
            if any(existing["id"] == record["id"] for existing in records):
                await query.message.reply_text(
                    "Этот GitHub-файл уже добавлен в провайдеры.",
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            if len(records) >= MAX_DYNAMIC_PROVIDERS:
                await query.message.reply_text(
                    f"Достигнут лимит динамических провайдеров: {MAX_DYNAMIC_PROVIDERS}. "
                    "Удали ненужный источник перед добавлением нового.",
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            records.append(record)
            ok, detail = await persist_dynamic_providers(
                records,
                f"Add VLESS provider {record['name']}",
            )
            if not ok:
                await query.message.reply_text(
                    f"❌ Не удалось сохранить провайдера.\n<code>{html.escape(detail)}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            PROVIDER_CANDIDATES.pop(token, None)
            await query.edit_message_reply_markup(reply_markup=back_keyboard("admin_providers"))
            storage_note = (
                "сохранён в GitHub"
                if detail.startswith("https://")
                else "добавлен локально; для сохранения после redeploy настрой GITHUB_TOKEN"
            )
            await query.message.reply_text(
                f"✅ Провайдер <b>{html.escape(record['name'])}</b> {storage_note}. "
                "Обновляю списки…",
                parse_mode=ParseMode.HTML,
            )
            await update_cache(bot=context.bot)
            await query.message.reply_text(
                "✅ Провайдер включён в парсер и списки пересобраны.",
                reply_markup=providers_keyboard(),
            )
            return

        provider_id = data.split(":", 1)[1] if ":" in data else ""
        record = provider_by_id(provider_id)
        if not record:
            await query.message.reply_text(
                "Провайдер не найден. Возможно, список уже изменился.",
                reply_markup=providers_keyboard(),
            )
            return

        if data.startswith("provider_view:"):
            category_label = "Белые списки" if record["category"] == "white" else "Черные списки"
            status = "включён" if record.get("enabled", True) else "выключен"
            await edit_message_with_banner(
                query,
                "main",
                f"<b>🗂️ {html.escape(record['name'])}</b>\n\n"
                f"Категория: <b>{category_label}</b>\n"
                f"Статус: <b>{status}</b>\n"
                f"Добавлен: <code>{html.escape(record.get('added_at') or '—')}</code>",
                provider_detail_keyboard(record),
            )
            return

        if data.startswith("provider_toggle:"):
            records = dynamic_provider_records()
            for item in records:
                if item["id"] == provider_id:
                    item["enabled"] = not item.get("enabled", True)
                    record = item
                    break
            ok, detail = await persist_dynamic_providers(
                records,
                f"{'Enable' if record['enabled'] else 'Disable'} VLESS provider {record['name']}",
            )
            if not ok:
                await query.message.reply_text(
                    f"❌ Не удалось изменить провайдера.\n<code>{html.escape(detail)}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            persistence_note = (
                ""
                if detail.startswith("https://")
                else " Сохранение пока локальное: настрой GITHUB_TOKEN для переживания redeploy."
            )
            await query.message.reply_text(
                f"Изменение сохранено. Пересобираю списки…{persistence_note}"
            )
            await update_cache(bot=context.bot)
            await edit_message_with_banner(query, "main", provider_panel_text(), providers_keyboard())
            return

        if data.startswith("provider_delete_confirm:"):
            await edit_message_with_banner(
                query,
                "main",
                f"<b>Удалить провайдера?</b>\n\n{html.escape(record['name'])}\n\n"
                "После подтверждения источник будет удалён из providers.json и списки пересоберутся.",
                InlineKeyboardMarkup([
                    [ui_button("delete", "«Да, удалить»", callback_data=f"provider_delete:{provider_id}", style=KeyboardButtonStyle.DANGER)],
                    [ui_button("back", "«Отмена»", callback_data=f"provider_view:{provider_id}", style=KeyboardButtonStyle.PRIMARY)],
                ]),
            )
            return

        if data.startswith("provider_delete:"):
            records = [
                item for item in dynamic_provider_records() if item["id"] != provider_id
            ]
            ok, detail = await persist_dynamic_providers(
                records,
                f"Remove VLESS provider {record['name']}",
            )
            if not ok:
                await query.message.reply_text(
                    f"❌ Не удалось удалить провайдера.\n<code>{html.escape(detail)}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_keyboard("admin_providers"),
                )
                return
            persistence_note = (
                ""
                if detail.startswith("https://")
                else " Удаление пока локальное: настрой GITHUB_TOKEN для переживания redeploy."
            )
            await query.message.reply_text(
                f"Провайдер удалён. Пересобираю списки…{persistence_note}"
            )
            await update_cache(bot=context.bot)
            await edit_message_with_banner(query, "main", provider_panel_text(), providers_keyboard())
            return

    clear_provider_input_state(context)

    if data == "home":
        clear_provider_input_state(context)
        await edit_message_with_banner(query, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return

    if data == "profile":
        u = get_or_create_user(uid, user.username if user else "", user.first_name if user else "")
        caste = "Owner" if config.is_admin(uid) else "Client"
        username = user.username or u.get('username') or '—'
        if username != '—' and not username.startswith('@'):
            username = f"@{username}"
        # жирный id, username, name, форзацы как просил
        reg_date = u.get('registration_date', '—')
        # если старый формат без времени, добавляем время
        if reg_date and "МСК" not in reg_date and "." in reg_date:
            # конвертим старую дату в новый формат с временем first_seen если есть
            try:
                fs = u.get('first_seen')
                if fs:
                    dt = datetime.fromisoformat(fs)
                    reg_date = dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")
            except:
                pass
        text = (
            f"<b>👤 Профиль</b>\n\n"
            f"<b>🆔 id:</b> {uid}\n"
            f"<b>Username:</b> {username}\n"
            f"<b>Имя:</b> {user.first_name or u.get('first_name') or '—'}\n"
            f"Роль: {caste}\n\n"
            f"🗓️ Дата регистрации\n{reg_date}"
        )
        await edit_message_with_banner(query, "profile", text, back_keyboard())
        return

    if data == "help":
        await edit_message_with_banner(query, "help", config.HELP_TEXT, back_keyboard())
        return

    if data == "admin_panel":
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        clear_provider_input_state(context)
        await edit_message_with_banner(query, "main", "<b>⚙️ Админ панель</b>\n\nВыбери действие:", admin_keyboard())
        return

    if data in (
        "admin_stats",
        "admin_refresh",
        "admin_sources",
        "admin_clean",
        "admin_notifications",
    ):
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        if data == "admin_notifications":
            enabled = not update_notifications_enabled()
            set_update_notifications(enabled)
            status = "включены" if enabled else "выключены"
            await edit_message_with_banner(
                query,
                "main",
                f"<b>🔔 Админ панель</b>\n\nУведомления обновления списков <b>{status}</b>.",
                admin_keyboard(),
            )
            return
        if data == "admin_sources":
            await query.message.reply_text(
                render_html(config.SOURCES_TEXT, config.CUSTOM_EMOJI_IDS),
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_panel"),
            )
            return
        if data == "admin_stats":
            if not CACHE:
                await update_cache(bot=context.bot)
            lines = [f"<b>📊 Статистика</b>"]
            total=0
            for k,d in CACHE.items():
                cnt=len(d.get("configs",[])); total+=cnt
                lines.append(f"{k}: <b>{cnt}</b>")
            lines.append(f"\nВсего VLESS: <b>{total}</b>")
            await query.message.reply_text(
                render_html("\n".join(lines), config.CUSTOM_EMOJI_IDS),
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_panel"),
            )
            return
        if data == "admin_refresh":
            try:
                await query.message.edit_caption(caption="Обновляю кэш…", parse_mode=ParseMode.HTML)
            except:
                await query.message.edit_text("Обновляю кэш…")
            await update_cache(bot=context.bot)
            await edit_message_with_banner(query, "main", f"<b>✅ Готово</b>\n\n🔄 Обновлено {datetime.now(MSK).strftime('%H:%M')}", admin_keyboard())
            return
        if data == "admin_clean":
            await handle_admin_clean(query)
            return

    if data == "white":
        agg_key = "WHITE_FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache(bot=context.bot)
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = f"<b>⬜ Белые списки</b>\n\n🔗 Всего VLESS: <b>{total}</b>\n\nВыбери действие:"
        await edit_message_with_banner(query, "protocols", text, protocol_keyboard(agg_key))
        return

    if data == "black":
        agg_key = "BLACK_FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache(bot=context.bot)
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = f"<b>⬛ Черные списки</b>\n\n🔗 Всего VLESS: <b>{total}</b>\n\nВыбери действие:"
        await edit_message_with_banner(query, "protocols", text, protocol_keyboard(agg_key))
        return

    if data == "full":
        agg_key = "FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache(bot=context.bot)
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = f"<b>📚 Полный список</b>\n\n🔗 Всего VLESS: <b>{total}</b>\n\nВыбери действие:"
        await edit_message_with_banner(query, "protocols", text, protocol_keyboard(agg_key))
        return

    if data.startswith("proto:"):
        try:
            _, agg_key, proto = data.split(":", 2)
        except:
            await query.answer("Ошибка", show_alert=True)
            return
        agg = config.AGGREGATED_SUBS.get(agg_key)
        if not agg:
            await query.message.reply_text("Неизвестный список")
            return
        base = agg["filename"]
        base_title = agg["profile_title"]
        fname = base
        chunks = AGGREGATED_CHUNKS.get(fname, [])
        cnt = AGGREGATED_CACHE.get(fname, {}).get("count", "?")
        back_target = agg_key.lower().replace("_full","")
        if back_target not in ("white","black","full"):
            back_target = "home"
        text = (
            f"<b>📦 {base_title}</b>\n\n"
            f"🔗 Всего VLESS: <b>{cnt}</b>\n\n"
            "Выбери пакет — бот отправит готовый <code>.txt</code>-файл."
        )
        if chunks:
            kb = chunks_keyboard(fname, chunks, back_data=back_target, back_label="«К протоколам»")
        else:
            kb = InlineKeyboardMarkup(
                [
                    [ui_button(
                        "download",
                        "«Скачать .txt»",
                        callback_data=f"rawfile:{fname}",
                        style=KeyboardButtonStyle.SUCCESS,
                    )],
                    [ui_button(
                        "back",
                        "«К протоколам»",
                        callback_data=back_target,
                        style=KeyboardButtonStyle.PRIMARY,
                    )],
                ]
            )
        await edit_message_with_banner(query, "configs", text, kb)
        return

    if data.startswith("chunk:"):
        fname = data.split(":", 1)[1]
        await send_chunk_file(
            query,
            fname,
            back_data=aggregate_back_callback(fname),
        )
        return

    # Compatibility for buttons left in old Telegram messages: every old
    # link/base64/QR action now sends the corresponding .txt file instead.
    if data.startswith(("rawcopy:", "b64copy:", "qrfile:", "rawfile:")):
        fname = data.split(":", 1)[1]
        await send_chunk_file(
            query,
            fname,
            back_data=aggregate_back_callback(fname),
        )
        return

# ---------- Message handlers ----------

async def relay_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Relay user messages and admin replies without exposing admin accounts."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return False

    user_id = user.id
    reply_target = context.user_data.get("support_reply_to")
    if reply_target and config.is_admin(user_id):
        try:
            await context.bot.send_message(
                chat_id=int(reply_target),
                text=render_html("💬 <b>Ответ поддержки</b>", config.CUSTOM_EMOJI_IDS),
                parse_mode=ParseMode.HTML,
            )
            await context.bot.copy_message(
                chat_id=int(reply_target),
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            await message.reply_text(
                render_html(
                    "✅ Ответ отправлен пользователю. Можно отправить следующее сообщение "
                    "или вернуться в админ-панель.",
                    config.CUSTOM_EMOJI_IDS,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_panel"),
            )
        except Exception as exc:
            logger.warning("Support reply delivery failed: %s", exc)
            await message.reply_text(
                "❌ Не удалось доставить ответ. Возможно, пользователь заблокировал бота.",
                reply_markup=back_keyboard("admin_panel"),
            )
        return True

    if not context.user_data.get("support_mode"):
        return False

    delivered = 0
    for admin_id in sorted(config.ADMIN_IDS):
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=render_html(
                    "💬 <b>Новое обращение в поддержку</b>\n\n"
                    f"🆔 ID пользователя: <code>{user_id}</code>",
                    config.CUSTOM_EMOJI_IDS,
                ),
                parse_mode=ParseMode.HTML,
            )
            await context.bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
                reply_markup=support_reply_keyboard(user_id),
            )
            delivered += 1
        except Exception as exc:
            logger.warning("Support message delivery to %s failed: %s", admin_id, exc)

    if delivered:
        await message.reply_text(
            render_html(
                "✅ Сообщение передано поддержке. Ответ придёт в этот чат.",
                config.CUSTOM_EMOJI_IDS,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=support_keyboard(),
        )
    else:
        await message.reply_text(
            "❌ Сейчас не удалось связаться с поддержкой. Попробуй немного позже.",
            reply_markup=support_keyboard(),
        )
    return True


async def message_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    user = update.effective_user
    if user:
        get_or_create_user(uid, user.username or "", user.first_name or "")

    if not await is_user_subscribed(uid, context.bot):
        await update.message.reply_text(f"📢 Подпишись на {config.CHANNEL_USERNAME}, чтобы пользоваться ботом", reply_markup=sub_required_keyboard())
        return

    text = (update.message.text or "").strip()
    if is_main_menu_text(text):
        clear_support_state(context)
        clear_provider_input_state(context)
        await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return

    if await handle_manual_provider_input(update, context):
        return

    if await relay_support_message(update, context):
        return

    custom_ids = extract_custom_emoji_ids(update.message)
    if custom_ids:
        rows = [
            "🔎 Найдены custom emoji ID:",
            *[f"<code>{custom_id}</code>" for custom_id in custom_ids],
            "",
            "Добавь их в CUSTOM_EMOJI_IDS в формате JSON, сопоставив с ключами из src/ui.py.",
        ]
        await update.message.reply_text(
            render_html("\n".join(rows), config.CUSTOM_EMOJI_IDS),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(uid),
        )
        return

    if "vless://" in text:
        import re
        m = re.search(r'vless://[^\s]+', text)
        if m:
            link = m.group(0)
            ok, reason = is_valid_vless(link)
            info = parse_vless_info(link)
            if ok:
                await update.message.reply_text(f"VLESS: {info.get('remark')}\n{info.get('host')}:{info.get('port')} • валиден", reply_markup=main_keyboard(uid))
            else:
                await update.message.reply_text(f"Битый VLESS: {reason}")

def main():
    if not config.BOT_TOKEN:
        print("BOT_TOKEN не задан")
        return
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, message_text_handler)
    )
    app.add_handler(CallbackQueryHandler(callback_handler))
    print(f"Crimson bot @vpncrimson интервальный {config.UPDATE_INTERVAL}м — только .txt на репо")

    async def _preload():
        try:
            await update_cache(bot=app.bot)
            print(f"Кэш {sum(len(v.get('configs',[])) for v in CACHE.values())} VLESS")
        except Exception as e:
            print(f"Ошибка preload: {e}")

    async def _post_init(app_obj):
        if start_health_server:
            try:
                await start_health_server()
            except Exception as e:
                logger.warning(e)
        await _preload()
        asyncio.create_task(auto_update_loop(app_obj))

    app.post_init = _post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

async def auto_update_loop(app):
    await asyncio.sleep(10)
    while True:
        try:
            await update_cache(bot=app.bot)
        except Exception as e:
            logger.error(e)
        # Автообновление раз в час
        await asyncio.sleep(config.UPDATE_INTERVAL*60)

if __name__ == "__main__":
    main()
