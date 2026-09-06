import os
import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import KeyboardButtonStyle, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from parser import (
    deduplicate_configs,
    fetch_all,
    is_valid_vless,
    parse_vless_info,
    is_valid_any,
    validate_configs,
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
LAST_NOTIFY = None  # для троттлинга уведомлений раз в час
AGGREGATED_CACHE = {}
AGGREGATED_CHUNKS = {}
AGGREGATED_PROTO_COUNTS = {}
UPDATE_LOCK = asyncio.Lock()
USERS_FILE = DATA_DIR / "users.json"

MSK = timezone(timedelta(hours=3))

REPLY_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton("Главное меню", style=KeyboardButtonStyle.PRIMARY)]],
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
        [
            InlineKeyboardButton(
                "«Профиль»",
                callback_data="profile",
                style=KeyboardButtonStyle.PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                "«Белые списки»",
                callback_data="white",
                style=KeyboardButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                "«Черные списки»",
                callback_data="black",
                style=KeyboardButtonStyle.SUCCESS,
            ),
        ],
        [
            InlineKeyboardButton(
                "«Полный список»",
                callback_data="full",
                style=KeyboardButtonStyle.SUCCESS,
            )
        ],
        [
            InlineKeyboardButton(
                "«Помощь»",
                callback_data="help",
                style=KeyboardButtonStyle.DANGER,
            )
        ],
    ]
    if user_id and config.is_admin(user_id):
        kb.append(
            [
                InlineKeyboardButton(
                    "«Админ панель»",
                    callback_data="admin_panel",
                    style=KeyboardButtonStyle.DANGER,
                )
            ]
        )
    return InlineKeyboardMarkup(kb)


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "«Статистика»",
                    callback_data="admin_stats",
                    style=KeyboardButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "«Обновить кэш»",
                    callback_data="admin_refresh",
                    style=KeyboardButtonStyle.SUCCESS,
                ),
            ],
            [
                InlineKeyboardButton(
                    "«Проверка и очистка»",
                    callback_data="admin_clean",
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
            [
                InlineKeyboardButton(
                    "«Источники»",
                    callback_data="admin_sources",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "«Назад»",
                    callback_data="home",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def sub_required_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "«Подписаться на канал»",
                    url=config.CHANNEL_LINK,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "«Проверить подписку»",
                    callback_data="check_sub",
                    style=KeyboardButtonStyle.SUCCESS,
                )
            ],
        ]
    )

def back_keyboard(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "«Назад»",
                callback_data=callback_data,
                style=KeyboardButtonStyle.PRIMARY,
            )
        ]]
    )


def chunks_keyboard(
    base_filename: str,
    chunk_list,
    back_data: str = "home",
    back_label: str = "«Назад»",
):
    rows = []
    for _, (cfname, _, cnt) in enumerate(chunk_list, 1):
        short = cfname.replace(".txt", "")
        label = f"«{short} · {cnt}»"
        button = InlineKeyboardButton(
            label,
            callback_data=f"chunk:{cfname}",
            style=KeyboardButtonStyle.PRIMARY,
        )
        if not rows or len(rows[-1]) == 2:
            rows.append([button])
        else:
            rows[-1].append(button)
    rows.append(
        [
            InlineKeyboardButton(
                "«Скачать полный файл»",
                callback_data=f"rawfile:{base_filename}",
                style=KeyboardButtonStyle.SUCCESS,
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                back_label,
                callback_data=back_data,
                style=KeyboardButtonStyle.PRIMARY,
            )
        ]
    )
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
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "«Назад»",
                callback_data="home",
                style=KeyboardButtonStyle.PRIMARY,
            )]]
        )
    base = agg["filename"]
    total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"«VLESS · {total}»",
                    callback_data=f"proto:{agg_key}:all",
                    style=KeyboardButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    "«Назад»",
                    callback_data="home",
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
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
    """Уведомление в канал @vpncrimson об обновлении списков — каждый раз когда обновляю кэш"""
    global LAST_NOTIFY
    if not config.CHANNEL_ID:
        return
    now = datetime.now(MSK)
    try:
        diff = new_total - old_total
        sign = f"+{diff}" if diff > 0 else str(diff) if diff != 0 else "0"
        text = (
            f"<b>Free VPN • Crimson — списки обновлены</b>\n\n"
            f"Всего VLESS: <b>{new_total}</b> ({sign})\n"
            f"Дата: {now.strftime('%d.%m.%Y %H:%M МСК')}\n\n"
            f"Получить — @wtfparsbot"
        )
        await bot.send_message(chat_id=config.CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
        LAST_NOTIFY = now
        logger.info(f"Notified channel {config.CHANNEL_ID} about update {old_total}->{new_total}")
    except Exception as e:
        logger.warning(f"Channel notify failed: {e}")

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
        # Уведомление в канал раз в час, если есть изменения
        if bot and old_total != new_total:
            asyncio.create_task(notify_channel_update(bot, old_total, new_total))
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
    uid = update.effective_user.id if update.effective_user else None
    user = update.effective_user
    get_or_create_user(uid, user.username if user else "", user.first_name if user else "")

    if not await is_user_subscribed(uid, context.bot):
        text = (
            f"<b>Доступ только по подписке</b>\n\n"
            f"Подпишись на канал {config.CHANNEL_USERNAME}, чтобы пользоваться ботом\n\n"
            f"После подписки нажми «Проверить подписку»"
        )
        await send_initial_banner(update, "main", text, sub_required_keyboard())
        return

    await update.message.reply_text("Клавиатура обновлена — жми «Главное меню» внизу", reply_markup=REPLY_MENU)
    await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not await is_user_subscribed(uid, context.bot):
        await update.message.reply_text(f"Подпишись на {config.CHANNEL_USERNAME} чтобы продолжить", reply_markup=sub_required_keyboard())
        return
    await send_initial_banner(update, "help", config.HELP_TEXT, main_keyboard(uid))

async def handle_main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Главное меню":
        uid = update.effective_user.id if update.effective_user else None
        if not await is_user_subscribed(uid, context.bot):
            await update.message.reply_text(f"Подпишись на {config.CHANNEL_USERNAME}", reply_markup=sub_required_keyboard())
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
            f"<b>{fname} больше не существует</b>\n\n"
            "Количество конфигураций изменилось, поэтому пакеты были пересобраны. "
            "Выбери актуальный пакет ниже."
        )
        keyboard = (
            chunks_keyboard(base_filename, current_chunks, back_data=back_data)
            if current_chunks
            else back_keyboard(back_data)
        )
    else:
        text = "<b>Файл больше не существует</b>\n\nОткрой список заново."
        keyboard = back_keyboard(back_data)
    await edit_message_with_banner(query, "configs", text, keyboard)


async def send_chunk_file(query, fname, back_data="home"):
    path = local_subscription_path(fname)
    if path is None:
        await query.message.reply_text("Файл изменился, обновляю список пакетов…")
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
        f"<b>{title}</b>\n\n"
        f"Конфигов в файле: <b>{cnt}</b>\n\n"
        f"{config.FILE_USAGE_TEXT}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "«Скачать .txt»",
                    callback_data=f"rawfile:{fname}",
                    style=KeyboardButtonStyle.SUCCESS,
                )
            ],
            [
                InlineKeyboardButton(
                    "«Назад»",
                    callback_data=back_data,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
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
        result = await update_cache(mode="tcp", bot=None)
    except Exception as exc:
        logger.exception("admin validation and cleanup failed")
        await edit_message_with_banner(
            query,
            "main",
            f"<b>Проверка не завершена</b>\n\nОшибка: <code>{str(exc)[:300]}</code>",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "«Назад»",
                            callback_data="admin_panel",
                            style=KeyboardButtonStyle.PRIMARY,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "«Главное меню»",
                            callback_data="home",
                            style=KeyboardButtonStyle.PRIMARY,
                        )
                    ],
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
                [
                    InlineKeyboardButton(
                        "«Назад»",
                        callback_data="admin_panel",
                        style=KeyboardButtonStyle.PRIMARY,
                    )
                ],
                [
                    InlineKeyboardButton(
                        "«Главное меню»",
                        callback_data="home",
                        style=KeyboardButtonStyle.PRIMARY,
                    )
                ],
            ]
        ),
    )

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
                f"<b>Доступ только по подписке</b>\n\nПодпишись на {config.CHANNEL_USERNAME}, чтобы пользоваться ботом",
                sub_required_keyboard())
            return

    if data == "check_sub":
        if await is_user_subscribed(uid, context.bot):
            await edit_message_with_banner(query, "main", config.WELCOME_TEXT, main_keyboard(uid))
        else:
            await query.answer("Ты еще не подписался на канал", show_alert=True)
            await edit_message_with_banner(query, "main",
                f"<b>Ты еще не подписался</b>\n\nПодпишись на {config.CHANNEL_USERNAME} и нажми проверку",
                sub_required_keyboard())
        return

    if data == "home":
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
            f"<b>Профиль</b>\n\n"
            f"<b>id:</b>{uid}\n"
            f"<b>Username:</b> {username}\n"
            f"<b>Name:</b> {user.first_name or u.get('first_name') or '—'}\n"
            f"Caste: {caste}\n\n"
            f"Дата регистрации\n{reg_date}"
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
        await edit_message_with_banner(query, "main", "<b>Админ панель</b>\n\nВыбери действие:", admin_keyboard())
        return

    if data in ("admin_stats", "admin_refresh", "admin_sources", "admin_clean"):
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        if data == "admin_sources":
            await query.message.reply_text(
                config.SOURCES_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_panel"),
            )
            return
        if data == "admin_stats":
            if not CACHE:
                await update_cache(bot=context.bot)
            lines = [f"<b>Статистика</b>"]
            total=0
            for k,d in CACHE.items():
                cnt=len(d.get("configs",[])); total+=cnt
                lines.append(f"{k}: <b>{cnt}</b>")
            lines.append(f"\nВсего VLESS: <b>{total}</b>")
            await query.message.reply_text(
                "\n".join(lines),
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
            await edit_message_with_banner(query, "main", f"<b>Готово</b>\n\nОбновлено {datetime.now(MSK).strftime('%H:%M')}", admin_keyboard())
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
        text = f"<b>Белые списки</b>\n\nВсего VLESS: <b>{total}</b>\n\nВыбери действие:"
        await edit_message_with_banner(query, "protocols", text, protocol_keyboard(agg_key))
        return

    if data == "black":
        agg_key = "BLACK_FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache(bot=context.bot)
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = f"<b>Черные списки</b>\n\nВсего VLESS: <b>{total}</b>\n\nВыбери действие:"
        await edit_message_with_banner(query, "protocols", text, protocol_keyboard(agg_key))
        return

    if data == "full":
        agg_key = "FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache(bot=context.bot)
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = f"<b>Полный список</b>\n\nВсего VLESS: <b>{total}</b>\n\nВыбери действие:"
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
            f"<b>{base_title}</b>\n\n"
            f"Всего VLESS: <b>{cnt}</b>\n\n"
            "Выбери пакет — бот отправит готовый <code>.txt</code>-файл."
        )
        if chunks:
            kb = chunks_keyboard(fname, chunks, back_data=back_target, back_label="«К протоколам»")
        else:
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "«Скачать .txt»",
                            callback_data=f"rawfile:{fname}",
                            style=KeyboardButtonStyle.SUCCESS,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "«К протоколам»",
                            callback_data=back_target,
                            style=KeyboardButtonStyle.PRIMARY,
                        )
                    ],
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

async def message_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    user = update.effective_user
    if user:
        get_or_create_user(uid, user.username or "", user.first_name or "")

    if not await is_user_subscribed(uid, context.bot):
        await update.message.reply_text(f"Подпишись на {config.CHANNEL_USERNAME} чтобы пользоваться ботом", reply_markup=sub_required_keyboard())
        return

    text = (update.message.text or "").strip()
    if text == "Главное меню":
        await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_text_handler))
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
