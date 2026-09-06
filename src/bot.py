import os
import asyncio
import logging
import random
import base64
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

import config
from parser import fetch_all, is_valid_vless, parse_vless_info, is_valid_any
from subscription import save_subscription_files, generate_qr_bytes, CHUNK_SIZE, save_aggregated_chunks, PROTOCOLS, PROTOCOL_LABELS, filter_by_protocol, save_aggregated_file
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
AGGREGATED_CACHE = {}
AGGREGATED_CHUNKS = {}
AGGREGATED_PROTO_COUNTS = {}
TEMP_FILES = {}  # filename -> {user_id, created_at, expires_at}
USERS_FILE = DATA_DIR / "users.json"

MSK = timezone(timedelta(hours=3))

REPLY_MENU = ReplyKeyboardMarkup([[KeyboardButton("Главное меню")]], resize_keyboard=True, is_persistent=True)

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
            "registration_date": datetime.now(MSK).strftime("%d.%m.%Y"),
            "first_seen": datetime.now(MSK).isoformat()
        }
        save_users(users)
    else:
        # обновляем username если изменился
        if username and users[uid_str].get("username") != username:
            users[uid_str]["username"] = username
            save_users(users)
    return users[uid_str]

# ---------- Keyboards ----------

def main_keyboard(user_id: int = None):
    kb = [
        [InlineKeyboardButton("«Профиль»", callback_data="profile")],
        [InlineKeyboardButton("«Белые списки»", callback_data="white"),
         InlineKeyboardButton("«Черные списки»", callback_data="black")],
        [InlineKeyboardButton("«Полный список»", callback_data="full")],
        [InlineKeyboardButton("«Собрать подписку»", callback_data="build_subscription")],
        [InlineKeyboardButton("«Помощь»", callback_data="help")],
    ]
    if user_id and config.is_admin(user_id):
        kb.append([InlineKeyboardButton("«Админ панель»", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("«Статистика»", callback_data="admin_stats"),
         InlineKeyboardButton("«Обновить кэш»", callback_data="admin_refresh")],
        [InlineKeyboardButton("«Очистить нерабочие»", callback_data="admin_clean")],
        [InlineKeyboardButton("«Источники»", callback_data="admin_sources")],
        [InlineKeyboardButton("«Назад»", callback_data="home")],
    ])

def sub_required_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("«Подписаться на канал»", url=config.CHANNEL_LINK)],
        [InlineKeyboardButton("«Проверить подписку»", callback_data="check_sub")]
    ])

def chunks_keyboard(base_filename: str, chunk_list, back_data: str = "home", back_label: str = "«Назад»"):
    rows = []
    for _, (cfname, _, cnt) in enumerate(chunk_list, 1):
        short = cfname.replace(".txt","")
        label = f"«{short} · {cnt}»"
        if len(rows)==0 or len(rows[-1])==2:
            rows.append([InlineKeyboardButton(label, callback_data=f"chunk:{cfname}")])
        else:
            rows[-1].append(InlineKeyboardButton(label, callback_data=f"chunk:{cfname}"))
    rows.append([InlineKeyboardButton(f"«Скачать полный файл»", callback_data=f"rawfile:{base_filename}")])
    rows.append([InlineKeyboardButton(back_label, callback_data=back_data)])
    return InlineKeyboardMarkup(rows)

def protocol_keyboard(agg_key: str):
    agg = config.AGGREGATED_SUBS.get(agg_key)
    if not agg:
        return InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]])
    base = agg["filename"]
    total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
    rows = []
    rows.append([InlineKeyboardButton(f"«VLESS · {total}»", callback_data=f"proto:{agg_key}:all")])
    rows.append([InlineKeyboardButton("«Назад»", callback_data="home")])
    return InlineKeyboardMarkup(rows)

# ---------- Channel subscription check ----------

async def is_user_subscribed(user_id: int, bot) -> bool:
    """Проверяет подписку на @vpncrimson"""
    try:
        chat_id = config.REQUIRED_CHANNEL
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator', 'owner']
    except Exception as e:
        # Если бот не админ в канале или канал приватный — не блокируем, но логируем
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
        if agg_key == "CUSTOM_100" and not source_keys:
            continue
        for sk in source_keys:
            data = CACHE.get(sk, {})
            for c in data.get("configs", []):
                if not c.lower().startswith("vless://"):
                    continue
                if c not in seen:
                    seen.add(c)
                    all_cfgs.append(c)
        try:
            full_path, full_b64, full_content, full_b64c, chunk_infos = save_aggregated_chunks(str(DATA_DIR), filename, title, all_cfgs, CHUNK_SIZE)
            results[filename] = {"content": full_content, "count": len(all_cfgs), "configs": all_cfgs}
            chunk_list = []
            for cfname, ctitle, cnt, ccontent in chunk_infos:
                results[cfname] = {"content": ccontent, "count": cnt, "configs": [], "is_chunk": True}
                chunk_list.append((cfname, ctitle, cnt))
            chunk_map[filename] = chunk_list
            proto_counts_map[filename] = {"vless": len(all_cfgs)}
        except Exception as e:
            logger.error(f"aggregated build {filename} error: {e}")
    global AGGREGATED_CHUNKS, AGGREGATED_PROTO_COUNTS
    AGGREGATED_CHUNKS = chunk_map
    AGGREGATED_PROTO_COUNTS = proto_counts_map
    return results

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
        raw_map = await push_aggregated_subscriptions(to_push, config.GITHUB_REPO, config.GITHUB_TOKEN, config.GITHUB_BRANCH)
        for path, raw_url in raw_map.items():
            fname = path.split("/")[-1]
            if fname in aggregated_results:
                AGGREGATED_CACHE[fname] = {"raw_url": raw_url, "count": aggregated_results[fname]["count"], "content": aggregated_results[fname]["content"]}
        return raw_map
    except Exception as e:
        logger.error(f"push error: {e}")
        return {}

async def notify_channel_update(bot, old_total, new_total):
    """Уведомление в канал @vpncrimson об обновлении списков"""
    if not config.CHANNEL_ID:
        return
    try:
        diff = new_total - old_total
        sign = f"+{diff}" if diff > 0 else str(diff)
        text = (
            f"<b>Free VPN • Crimson — списки обновлены</b>\n\n"
            f"Всего VLESS: <b>{new_total}</b> ({sign})\n"
            f"Дата: {datetime.now(MSK).strftime('%d.%m.%Y %H:%M МСК')}\n\n"
            f"Получить — @wtfparsbot"
        )
        await bot.send_message(chat_id=config.CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
        logger.info(f"Notified channel {config.CHANNEL_ID} about update")
    except Exception as e:
        logger.warning(f"Channel notify failed: {e}")

async def update_cache(categories=None, mode=None, bot=None):
    global CACHE, LAST_UPDATE
    mode = mode or config.CHECK_MODE
    if categories is None:
        categories = list(config.SOURCES.keys())
    old_total = sum(len(v.get("configs", [])) for v in CACHE.values()) if CACHE else 0
    result = await fetch_all(mode=mode, categories=categories)
    for key, data in result.items():
        filtered = [c for c in data.get("configs", []) if c.lower().startswith("vless://")]
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
        agg = build_aggregated_configs()
        for fname, info in agg.items():
            if fname not in AGGREGATED_CACHE:
                AGGREGATED_CACHE[fname] = {"count": info["count"], "content": info["content"], "raw_url": get_raw_url(fname)}
            else:
                AGGREGATED_CACHE[fname].update({"count": info["count"], "content": info["content"]})
        if config.GITHUB_TOKEN and agg:
            asyncio.create_task(push_aggregated_to_github(agg))
        # Уведомление в канал если есть изменения
        if bot and old_total != new_total and abs(new_total - old_total) > 5:
            asyncio.create_task(notify_channel_update(bot, old_total, new_total))
    except Exception as e:
        logger.error(f"aggregated error: {e}")
    return result

def get_raw_url(filename: str) -> str:
    cached = AGGREGATED_CACHE.get(filename, {})
    if cached.get("raw_url"):
        return cached["raw_url"]
    repo = config.GITHUB_REPO
    branch = config.GITHUB_BRANCH
    path = f"{config.GITHUB_SUB_PATH}/{filename}" if config.GITHUB_SUB_PATH else filename
    path = path.lstrip("/")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

def get_public_url(filename: str) -> str:
    if config.PUBLIC_URL:
        base = config.PUBLIC_URL.rstrip("/")
        return f"{base}/sub/{filename}"
    return ""

def get_temp_raw_url(filename: str) -> str:
    """Временный .txt на самом репо vless-parser-bot (без доменов)"""
    # Используем основной репо бота для временных файлов
    repo = "xznexil3/vless-parser-bot"
    branch = "main"
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}"

# ---------- Media helpers — редактируем одно сообщение ----------

def get_banner_path(name: str) -> Path:
    return ASSETS_DIR / f"banner_{name}.png"

async def edit_message_with_banner(query, banner_name: str, text: str, reply_markup):
    """Редактирует существующее сообщение — не создает новых"""
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

# ---------- Temp files (3 часа) ----------

async def delete_temp_file_from_github(filename: str):
    """Удаляет временный файл из репо через GitHub API"""
    try:
        import aiohttp, base64
        token = config.GITHUB_TOKEN
        if not token:
            return
        repo = "xznexil3/vless-parser-bot"
        # Получаем sha
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com/repos/{repo}/contents/{filename}"
            headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                sha = data.get("sha")
            if not sha:
                return
            payload = {"message": f"delete temp {filename} after 3h", "sha": sha, "branch": "main"}
            async with session.delete(url, headers=headers, json=payload) as resp:
                txt = await resp.text()
                if resp.status in (200, 204):
                    logger.info(f"Deleted temp file {filename} from GitHub")
                else:
                    logger.warning(f"Delete temp {filename} failed {resp.status}: {txt[:200]}")
    except Exception as e:
        logger.error(f"delete temp file error {filename}: {e}")

async def schedule_temp_deletion(filename: str, delay_seconds: int = 10800):
    """Удаляет файл через 3 часа"""
    await asyncio.sleep(delay_seconds)
    # Удаляем локально
    try:
        local_path = Path(__file__).parent.parent / filename
        if local_path.exists():
            local_path.unlink()
            logger.info(f"Deleted local temp {filename}")
        data_path = DATA_DIR / filename
        if data_path.exists():
            data_path.unlink()
        b64_path = DATA_DIR / filename.replace(".txt", "_base64.txt")
        if b64_path.exists():
            b64_path.unlink()
    except Exception as e:
        logger.error(f"local delete temp failed {filename}: {e}")
    # Удаляем из GitHub
    await delete_temp_file_from_github(filename)
    TEMP_FILES.pop(filename, None)

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    user = update.effective_user
    get_or_create_user(uid, user.username if user else "", user.first_name if user else "")

    # Проверка подписки на канал
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

async def send_chunk_file(query, fname, back_data="home"):
    path = DATA_DIR / fname
    if not path.exists():
        # Проверяем в корне репо (временные файлы)
        root_path = Path(__file__).parent.parent / fname
        if root_path.exists():
            path = root_path
        else:
            await query.message.reply_text("Файл не найден, обновляю…")
            await update_cache(bot=query.get_bot() if hasattr(query, 'get_bot') else None)
    if path.exists():
        title = fname
        for v in config.AGGREGATED_SUBS.values():
            if v["filename"] == fname:
                title = v["profile_title"]
                break
            if fname.startswith(v["filename"].replace(".txt","")):
                title = f"{v['profile_title']} — {fname}"
                break
        cnt = AGGREGATED_CACHE.get(fname, {}).get("count", "?")
        # Для временных файлов используем прямую ссылку на репо
        if fname.startswith("TEMP_") or fname.startswith("CUSTOM_100_"):
            raw = get_temp_raw_url(fname)
        else:
            raw = get_public_url(fname) or get_raw_url(fname)
        text = f"<b>{title}</b>\n\n<code>{raw}</code>\n\nКонфигов: <b>{cnt}</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("«Скачать файл»", callback_data=f"rawfile:{fname}"), InlineKeyboardButton("«Копировать ссылку»", callback_data=f"rawcopy:{fname}")],
            [InlineKeyboardButton("«Назад»", callback_data=back_data)]
        ])
        await edit_message_with_banner(query, "configs", text, kb)
        try:
            await query.message.reply_document(document=open(path, "rb"), filename=fname, caption=f"{title} • {cnt}")
        except Exception as e:
            logger.error(e)

async def handle_build_subscription(query, user_id):
    try:
        await query.message.edit_caption(caption="Собираю подписку из 100 VLESS, подожди 5 сек...", parse_mode=ParseMode.HTML)
    except:
        try:
            await query.message.edit_text("Собираю подписку из 100 VLESS, подожди 5 сек...")
        except:
            pass

    if not CACHE:
        await update_cache(bot=query.get_bot() if hasattr(query, 'get_bot') else None)

    all_configs = []
    seen = set()
    for k, v in CACHE.items():
        for c in v.get("configs", []):
            if not c.lower().startswith("vless://"):
                continue
            if c not in seen:
                seen.add(c)
                all_configs.append(c)

    valid_configs = []
    for c in all_configs:
        ok, _ = is_valid_any(c)
        if ok:
            valid_configs.append(c)

    if not valid_configs:
        await edit_message_with_banner(query, "configs", "Не удалось найти VLESS. Попробуй обновить кэш.", InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))
        return

    random.shuffle(valid_configs)
    selected = valid_configs[:100] if len(valid_configs) >= 100 else valid_configs
    title = "Free VPN • Crimson — Custom 100"
    # Временный .txt на самом репо, живет 3 часа
    timestamp = datetime.now(MSK).strftime("%H%M")
    filename = f"TEMP_100_{user_id}_{timestamp}.txt"

    try:
        # Сохраняем в data и в корень репо для пуша
        path_data, _, content, _ = save_aggregated_file(str(DATA_DIR), filename, title, selected)
        root_path = Path(__file__).parent.parent / filename
        root_path.write_text(content, encoding="utf-8")

        # Пушим в основной репо vless-parser-bot (без доменов, просто raw github)
        raw_url = None
        if config.GITHUB_TOKEN:
            try:
                from github_sync import push_aggregated_subscriptions
                # Пушим именно в vless-parser-bot для временных файлов
                temp_repo = "xznexil3/vless-parser-bot"
                raw_map = await push_aggregated_subscriptions({filename: content}, temp_repo, config.GITHUB_TOKEN, "main")
                raw_url = raw_map.get(filename) or get_temp_raw_url(filename)
                # Если пуш успешен, raw_url уже будет
                if not raw_url:
                    raw_url = get_temp_raw_url(filename)
            except Exception as e:
                logger.error(f"push temp failed: {e}")
                raw_url = get_temp_raw_url(filename)
        else:
            raw_url = get_temp_raw_url(filename)

        # Сохраняем инфу о временном файле и планируем удаление через 3 часа
        TEMP_FILES[filename] = {
            "user_id": user_id,
            "created_at": datetime.now(MSK).isoformat(),
            "expires_at": (datetime.now(MSK) + timedelta(hours=3)).isoformat()
        }
        asyncio.create_task(schedule_temp_deletion(filename, 10800))

        AGGREGATED_CACHE[filename] = {"count": len(selected), "content": content, "raw_url": raw_url}

        text = (
            f"<b>Готово — {len(selected)} VLESS</b>\n\n"
            f"<code>{raw_url}</code>\n\n"
            f"Файл: <code>{filename}</code>\n"
            f"<i>Живет 3 часа, потом удалится</i>"
        )

        kb_rows = [
            [InlineKeyboardButton("«Скопировать ссылку»", callback_data=f"rawcopy:{filename}")],
            [InlineKeyboardButton("«Скачать файл»", callback_data=f"rawfile:{filename}"), InlineKeyboardButton("«QR»", callback_data=f"qrfile:{filename}")],
            [InlineKeyboardButton("«Собрать еще раз»", callback_data="build_subscription"), InlineKeyboardButton("«Назад»", callback_data="home")]
        ]

        await edit_message_with_banner(query, "configs", text, InlineKeyboardMarkup(kb_rows))

        try:
            await query.message.reply_document(document=open(path_data, "rb"), filename=filename, caption=f"Custom 100 • {len(selected)} VLESS • живет 3ч")
        except Exception as e:
            logger.error(e)

    except Exception as e:
        logger.error(f"build custom error: {e}")
        await edit_message_with_banner(query, "configs", f"Ошибка: {e}", InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))

async def handle_admin_clean(query):
    try:
        await query.message.edit_caption(caption="Очищаю нерабочие...", parse_mode=ParseMode.HTML)
    except:
        try:
            await query.message.edit_text("Очищаю нерабочие...")
        except:
            pass

    total_before = 0
    total_after = 0
    details = []
    for key, data in list(CACHE.items()):
        configs = data.get("configs", [])
        total_before += len(configs)
        valid = [c for c in configs if c.lower().startswith("vless://")]
        checked = []
        for c in valid:
            ok, _ = is_valid_any(c)
            if ok:
                checked.append(c)
        seen = set()
        uniq = []
        for c in checked:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        removed = len(configs) - len(uniq)
        if removed > 0:
            details.append(f"{key}: -{removed}")
        CACHE[key]["configs"] = uniq
        total_after += len(uniq)

    try:
        agg = build_aggregated_configs()
        for fname, info in agg.items():
            AGGREGATED_CACHE[fname] = {"count": info["count"], "content": info["content"], "raw_url": get_raw_url(fname)}
        if config.GITHUB_TOKEN and agg:
            asyncio.create_task(push_aggregated_to_github(agg))
    except Exception as e:
        logger.error(f"rebuild after clean failed: {e}")

    text = f"<b>Очистка завершена</b>\n\nБыло: {total_before}\nСтало: {total_after}\nУдалено: {total_before - total_after}\n\n" + ("\n".join(details[:20]) if details else "Все чистые")

    await edit_message_with_banner(query, "main", text, InlineKeyboardMarkup([
        [InlineKeyboardButton("«Назад»", callback_data="admin_panel")],
        [InlineKeyboardButton("«Главное меню»", callback_data="home")]
    ]))

# ---------- Callback ----------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id if query.from_user else 0
    user = query.from_user

    # Сохраняем пользователя для даты регистрации
    if user:
        get_or_create_user(uid, user.username or "", user.first_name or "")

    # Проверка подписки на канал (кроме кнопки проверки)
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
        text = (
            f"<b>Профиль</b>\n\n"
            f"<b>Id</b>\n<code>{uid}</code>\n\n"
            f"<b>Username</b>\n@{user.username or u.get('username') or '—'}\n\n"
            f"<b>Name</b>\n{user.first_name or u.get('first_name') or '—'}\n\n"
            f"<b>Дата регистрации</b>\n{u.get('registration_date', '—')}"
        )
        await edit_message_with_banner(query, "profile", text, InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))
        return

    if data == "help":
        await edit_message_with_banner(query, "help", config.HELP_TEXT, InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))
        return

    if data == "build_subscription":
        await handle_build_subscription(query, uid)
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
            await query.message.reply_text(config.SOURCES_TEXT, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="admin_panel")]]))
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
            await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="admin_panel")]]))
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
        raw = get_public_url(fname) or get_raw_url(fname)
        back_target = agg_key.lower().replace("_full","")
        if back_target not in ("white","black","full"):
            back_target = "home"
        text = f"<b>{base_title}</b>\n\n<code>{raw}</code>\n\nКонфигов: <b>{cnt}</b>\n\nВыбери пакет:"
        if chunks:
            kb = chunks_keyboard(fname, chunks, back_data=back_target, back_label="«К протоколам»")
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("«Скачать файл»", callback_data=f"rawfile:{fname}"), InlineKeyboardButton("«Копировать»", callback_data=f"rawcopy:{fname}")],
                [InlineKeyboardButton("«К протоколам»", callback_data=back_target)]
            ])
            text = f"<b>{base_title}</b>\n\n<code>{raw}</code>\n\nКонфигов: <b>{cnt}</b>"
        await edit_message_with_banner(query, "configs", text, kb)
        return

    if data.startswith("chunk:"):
        fname = data.split(":",1)[1]
        back = "home"
        for agg_key, agg in config.AGGREGATED_SUBS.items():
            if fname.startswith(agg["filename"].replace(".txt","")):
                bk = agg_key.lower().replace("_full","")
                if bk in ("white","black","full"):
                    back = bk
        await send_chunk_file(query, fname, back_data=back)
        return

    if data.startswith("rawcopy:"):
        fname = data.split(":",1)[1]
        if fname.startswith("TEMP_") or fname.startswith("CUSTOM_100_"):
            raw = get_temp_raw_url(fname)
        else:
            raw = get_public_url(fname) or get_raw_url(fname)
        await query.message.reply_text(f"<code>{raw}</code>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))
        return

    if data.startswith("b64copy:"):
        fname = data.split(":",1)[1]
        path = DATA_DIR / fname
        if not path.exists():
            path = Path(__file__).parent.parent / fname
        if not path.exists():
            await query.message.reply_text("Файл не найден")
            return
        content = path.read_text(encoding="utf-8")
        b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        if len(b64) < 4000:
            await query.message.reply_text(f"<code>{b64}</code>", parse_mode=ParseMode.HTML)
        else:
            b64_path = DATA_DIR / f"{fname.replace('.txt','_base64.txt')}"
            if b64_path.exists():
                await query.message.reply_document(document=open(b64_path, "rb"), filename=b64_path.name)
        return

    if data.startswith("qrfile:"):
        fname = data.split(":",1)[1]
        if fname.startswith("TEMP_") or fname.startswith("CUSTOM_100_"):
            link = get_temp_raw_url(fname)
        else:
            link = get_public_url(fname) or get_raw_url(fname)
        try:
            qr_bytes = generate_qr_bytes(link)
            await query.message.reply_photo(photo=qr_bytes, caption=f"{fname}\n{link}")
        except Exception as e:
            logger.error(f"qr error: {e}")
            await query.message.reply_text(f"QR ошибка: {e}")
        return

    if data.startswith("rawfile:"):
        fname = data.split(":",1)[1]
        path = DATA_DIR / fname
        if not path.exists():
            path = Path(__file__).parent.parent / fname
        if not path.exists():
            await update_cache(bot=context.bot)
            path = DATA_DIR / fname
            if not path.exists():
                path = Path(__file__).parent.parent / fname
        if path.exists():
            await query.message.reply_document(document=open(path, "rb"), filename=fname)
        else:
            await query.message.reply_text("Файл не найден")
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
    print(f"Crimson bot @vpncrimson интервальный {config.UPDATE_INTERVAL}м")

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
        await asyncio.sleep(config.UPDATE_INTERVAL*60)

if __name__ == "__main__":
    main()
