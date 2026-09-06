import os
import asyncio
import logging
import random
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

import config
from parser import fetch_all, is_valid_vless, parse_vless_info, is_valid_any
from subscription import save_subscription_files, generate_qr_bytes, get_subscription_stats, CHUNK_SIZE, save_aggregated_chunks, PROTOCOLS, PROTOCOL_LABELS, filter_by_protocol, save_aggregated_file
import aiohttp
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

MSK = timezone(timedelta(hours=3))
def msk_time():
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")

REPLY_MENU = ReplyKeyboardMarkup([[KeyboardButton("Главное меню")]], resize_keyboard=True, is_persistent=True)

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

def chunks_keyboard(base_filename: str, chunk_list, back_data: str = "home", back_label: str = "«Назад»"):
    rows = []
    for idx, (cfname, title, cnt) in enumerate(chunk_list, 1):
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
    counts = AGGREGATED_PROTO_COUNTS.get(base, {})
    rows = []
    rows.append([InlineKeyboardButton(f"«Все · {total}»", callback_data=f"proto:{agg_key}:all")])
    available = [(p, counts.get(p, 0)) for p in PROTOCOLS if counts.get(p, 0) > 0]
    if available:
        vless = [x for x in available if x[0]=="vless"]
        rest = [x for x in available if x[0]!="vless"]
        rest.sort(key=lambda x: x[1], reverse=True)
        ordered = vless + rest
        row = []
        for proto, cnt in ordered:
            label = f"«{PROTOCOL_LABELS.get(proto, proto.upper())} · {cnt}»"
            btn = InlineKeyboardButton(label, callback_data=f"proto:{agg_key}:{proto}")
            row.append(btn)
            if len(row)==2:
                rows.append(row)
                row=[]
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton("«Назад»", callback_data="home")])
    return InlineKeyboardMarkup(rows)

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
            base_name = filename.replace(".txt", "")
            proto_counts = {}
            for proto in PROTOCOLS:
                filtered = filter_by_protocol(all_cfgs, proto)
                proto_counts[proto] = len(filtered)
                if not filtered:
                    continue
                proto_filename = f"{base_name}_{proto.upper()}.txt"
                proto_title = f"{title} — {PROTOCOL_LABELS.get(proto, proto.upper())}"
                try:
                    p_full_path, p_full_b64, p_full_content, p_full_b64c, p_chunk_infos = save_aggregated_chunks(str(DATA_DIR), proto_filename, proto_title, filtered, CHUNK_SIZE)
                    results[proto_filename] = {"content": p_full_content, "count": len(filtered), "configs": filtered}
                    p_chunk_list = []
                    for cfname, ctitle, cnt, ccontent in p_chunk_infos:
                        results[cfname] = {"content": ccontent, "count": cnt, "configs": [], "is_chunk": True}
                        p_chunk_list.append((cfname, ctitle, cnt))
                    chunk_map[proto_filename] = p_chunk_list
                except Exception as e:
                    logger.error(f"proto build {proto_filename} error: {e}")
            proto_counts_map[filename] = proto_counts
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

async def update_cache(categories=None, mode=None):
    global CACHE, LAST_UPDATE
    mode = mode or config.CHECK_MODE
    if categories is None:
        categories = list(config.SOURCES.keys())
    result = await fetch_all(mode=mode, categories=categories)
    for key, data in result.items():
        configs = data.get("configs", [])
        title = config.SOURCES.get(key, {}).get("name", key)
        if configs:
            try:
                save_subscription_files(str(DATA_DIR), key, configs, title)
            except Exception as e:
                logger.error(f"save error {key}: {e}")
        CACHE[key] = data
    LAST_UPDATE = datetime.now(MSK)
    try:
        agg = build_aggregated_configs()
        for fname, info in agg.items():
            if fname not in AGGREGATED_CACHE:
                AGGREGATED_CACHE[fname] = {"count": info["count"], "content": info["content"], "raw_url": get_raw_url(fname)}
            else:
                AGGREGATED_CACHE[fname].update({"count": info["count"], "content": info["content"]})
        if config.GITHUB_TOKEN and agg:
            asyncio.create_task(push_aggregated_to_github(agg))
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

# ---------- Media helpers ----------

def get_banner_path(name: str) -> Path:
    """name: main, profile, protocols, configs, help"""
    return ASSETS_DIR / f"banner_{name}.png"

async def send_banner_message(update: Update, context: ContextTypes.DEFAULT_TYPE, banner_name: str, text: str, reply_markup, is_callback=False, query=None):
    """Отправляет медиа-баннер с текстом. Если is_callback и сообщение уже фото — редактирует медиа."""
    banner_path = get_banner_path(banner_name)
    if not banner_path.exists():
        # fallback без баннера
        if is_callback and query:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return

    try:
        if is_callback and query:
            # Если текущее сообщение уже с фото — редактируем медиа
            if query.message.photo:
                with open(banner_path, 'rb') as f:
                    media = InputMediaPhoto(media=f, caption=text, parse_mode=ParseMode.HTML)
                    await query.message.edit_media(media=media, reply_markup=reply_markup)
            else:
                # Текстовое сообщение — отправляем новое фото и удаляем старое для чистоты
                await query.message.reply_photo(
                    photo=open(banner_path, 'rb'),
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
                try:
                    await query.message.delete()
                except:
                    pass
        else:
            # Команда /start
            if banner_path.exists():
                await update.message.reply_photo(
                    photo=open(banner_path, 'rb'),
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"banner {banner_name} send failed: {e}")
        if is_callback and query:
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        "Клавиатура обновлена — жми «Главное меню» внизу",
        reply_markup=REPLY_MENU
    )
    await send_banner_message(update, context, "main", config.WELCOME_TEXT, main_keyboard(uid), is_callback=False)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    await send_banner_message(update, context, "help", config.HELP_TEXT, main_keyboard(uid), is_callback=False)

async def handle_main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Главное меню":
        uid = update.effective_user.id if update.effective_user else None
        await send_banner_message(update, context, "main", config.WELCOME_TEXT, main_keyboard(uid), is_callback=False)
        return True
    return False

async def send_chunk_file(query, fname, back_data="home"):
    path = DATA_DIR / fname
    if not path.exists():
        await query.message.reply_text("Файл не найден, обновляю…")
        await update_cache()
    if path.exists():
        title = fname
        for v in config.AGGREGATED_SUBS.values():
            if v["filename"] == fname:
                title = v["profile_title"]
                break
            if fname.startswith(v["filename"].replace(".txt","")):
                base_title = v["profile_title"]
                for proto in PROTOCOLS:
                    if f"_{proto.upper()}.txt" in fname or f"_{proto.upper()}_" in fname:
                        base_title = f"{base_title} — {PROTOCOL_LABELS.get(proto, proto.upper())}"
                        break
                title = f"{base_title} — {fname}"
                break
        cnt = AGGREGATED_CACHE.get(fname, {}).get("count", "?")
        raw = get_public_url(fname) or get_raw_url(fname)
        text = f"<b>{title}</b>\n<code>{raw}</code>\nКонфигов: <b>{cnt}</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("«Скачать файл»", callback_data=f"rawfile:{fname}"), InlineKeyboardButton("«Копировать ссылку»", callback_data=f"rawcopy:{fname}")],
            [InlineKeyboardButton("«Назад»", callback_data=back_data)]
        ])
        # Для конфигов показываем баннер конфигов
        await send_banner_message(None, None, "configs", text, kb, is_callback=True, query=query)
        try:
            await query.message.reply_document(document=open(path, "rb"), filename=fname, caption=f"{title} • {cnt}")
        except Exception as e:
            logger.error(e)

async def handle_build_subscription(query, user_id):
    # Показываем временный баннер конфигов
    await query.message.edit_text("Собираю подписку из 100 рабочих конфигов, подожди 5-10 сек...")
    if not CACHE:
        await update_cache()
    all_configs = []
    seen = set()
    for k, v in CACHE.items():
        for c in v.get("configs", []):
            if c not in seen:
                seen.add(c)
                all_configs.append(c)
    valid_configs = []
    for c in all_configs:
        ok, _ = is_valid_any(c)
        if ok:
            valid_configs.append(c)
    if not valid_configs:
        await send_banner_message(None, None, "configs", "Не удалось найти рабочие конфиги. Попробуй обновить кэш.", InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]), is_callback=True, query=query)
        return
    random.shuffle(valid_configs)
    selected = valid_configs[:100] if len(valid_configs) >= 100 else valid_configs
    title = "Free VPN • Crimson — Custom 100"
    filename = f"CUSTOM_100_{user_id}.txt"
    try:
        path, b64_path, content, b64_content = save_aggregated_file(str(DATA_DIR), filename, title, selected)
        raw_url = None
        public_url = get_public_url(filename)
        if config.GITHUB_TOKEN and config.GITHUB_REPO:
            try:
                from github_sync import push_aggregated_subscriptions
                p = f"{config.GITHUB_SUB_PATH}/{filename}" if config.GITHUB_SUB_PATH else filename
                p = p.lstrip("/")
                raw_map = await push_aggregated_subscriptions({p: content}, config.GITHUB_REPO, config.GITHUB_TOKEN, config.GITHUB_BRANCH)
                raw_url = raw_map.get(p)
                if raw_url:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"push custom failed: {e}")
        primary_link = public_url or raw_url
        if primary_link:
            link_text = f"<code>{primary_link}</code>"
        else:
            link_text = "Ссылка будет доступна после включения PUBLIC_URL или GITHUB. Пока используй файл ниже."
        AGGREGATED_CACHE[filename] = {"count": len(selected), "content": content, "raw_url": raw_url or primary_link or get_raw_url(filename)}
        text = (
            f"<b>Готово — собрано {len(selected)} рабочих конфигов</b>\n\n"
            f"{'Ссылка на подписку:' if primary_link else ''}\n{link_text}\n\n"
            f"Количество: <b>{len(selected)}</b>\n"
            f"Файл: <code>{filename}</code>\n\n"
            f"Добавь ссылку в Happ / Streisand / v2rayNG как URL подписки."
        )
        kb_rows = []
        if primary_link:
            kb_rows.append([InlineKeyboardButton("«Скопировать ссылку»", callback_data=f"rawcopy:{filename}")])
        kb_rows.append([
            InlineKeyboardButton("«Скачать файл»", callback_data=f"rawfile:{filename}"),
            InlineKeyboardButton("«Скопировать base64»", callback_data=f"b64copy:{filename}")
        ])
        kb_rows.append([
            InlineKeyboardButton("«Показать QR»", callback_data=f"qrfile:{filename}"),
            InlineKeyboardButton("«Собрать еще раз»", callback_data="build_subscription")
        ])
        kb_rows.append([InlineKeyboardButton("«Назад»", callback_data="home")])
        await send_banner_message(None, None, "configs", text, InlineKeyboardMarkup(kb_rows), is_callback=True, query=query)
        try:
            await query.message.reply_document(document=open(path, "rb"), filename=filename, caption=f"Custom 100 • {len(selected)} конфигов")
        except Exception as e:
            logger.error(e)
        try:
            b64_path_obj = DATA_DIR / f"{filename.replace('.txt','_base64.txt')}"
            if b64_path_obj.exists():
                await query.message.reply_document(document=open(b64_path_obj, "rb"), filename=b64_path_obj.name, caption=f"Base64 • {len(selected)}")
        except Exception as e:
            logger.error(e)
    except Exception as e:
        logger.error(f"build custom error: {e}")
        await query.message.edit_text(f"Ошибка при сборке подписки: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))

async def handle_admin_clean(query):
    await query.message.edit_text("Запущена очистка нерабочих конфигов, подожди...")
    total_before = 0
    total_after = 0
    details = []
    for key, data in list(CACHE.items()):
        configs = data.get("configs", [])
        total_before += len(configs)
        valid = []
        for c in configs:
            ok, _ = is_valid_any(c)
            if ok:
                valid.append(c)
        seen = set()
        uniq_valid = []
        for c in valid:
            if c not in seen:
                seen.add(c)
                uniq_valid.append(c)
        removed = len(configs) - len(uniq_valid)
        if removed > 0:
            details.append(f"{key}: удалено {removed}")
        CACHE[key]["configs"] = uniq_valid
        CACHE[key]["filtered_total"] = len(uniq_valid)
        total_after += len(uniq_valid)
    try:
        agg = build_aggregated_configs()
        for fname, info in agg.items():
            AGGREGATED_CACHE[fname] = {"count": info["count"], "content": info["content"], "raw_url": get_raw_url(fname)}
        if config.GITHUB_TOKEN and agg:
            asyncio.create_task(push_aggregated_to_github(agg))
    except Exception as e:
        logger.error(f"rebuild after clean failed: {e}")
    text = (
        f"<b>Очистка завершена</b>\n\n"
        f"Было: {total_before}\n"
        f"Стало: {total_after}\n"
        f"Удалено: {total_before - total_after}\n\n"
        + ("\n".join(details[:30]) if details else "Все конфиги уже чистые")
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("«Назад»", callback_data="admin_panel")],
        [InlineKeyboardButton("«Главное меню»", callback_data="home")]
    ])
    await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

# ---------- Callback ----------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id if query.from_user else 0

    if data == "home":
        await send_banner_message(update, context, "main", config.WELCOME_TEXT, main_keyboard(uid), is_callback=True, query=query)
        return

    if data == "profile":
        user = query.from_user
        text = (
            f"<b>Мой профиль</b>\n\n"
            f"ID: <code>{user.id}</code>\n"
            f"Username: @{user.username or '—'}\n"
            f"Имя: {user.first_name or '—'}"
        )
        await send_banner_message(update, context, "profile", text, InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]), is_callback=True, query=query)
        return

    if data == "help":
        await send_banner_message(update, context, "help", config.HELP_TEXT, InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]), is_callback=True, query=query)
        return

    if data == "build_subscription":
        await handle_build_subscription(query, uid)
        return

    if data == "admin_panel":
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        await query.message.edit_text("<b>Админ панель</b>\nВыбери действие:", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return

    if data in ("admin_stats", "admin_refresh", "admin_sources", "admin_clean"):
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        if data == "admin_sources":
            await query.message.reply_text(config.SOURCES_TEXT, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="admin_panel")]]))
            return
        if data == "admin_stats":
            if not CACHE:
                await update_cache()
            lines = [f"<b>Статистика • {datetime.now(MSK).strftime('%d.%m %H:%M')}</b>"]
            total=0
            for k,d in CACHE.items():
                cnt=len(d.get("configs",[])); total+=cnt
                lines.append(f"• {k}: <b>{cnt}</b>")
            lines.append(f"\nВсего: <b>{total}</b>")
            for fname, info in AGGREGATED_CACHE.items():
                if info.get("is_chunk"):
                    continue
                lines.append(f"• {fname}: <b>{info.get('count','?')}</b>")
            await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="admin_panel")]]))
            return
        if data == "admin_refresh":
            await query.message.edit_text("Обновляю кэш…")
            await update_cache()
            await query.message.edit_text(f"Готово • {datetime.now(MSK).strftime('%H:%M')}", reply_markup=admin_keyboard())
            return
        if data == "admin_clean":
            await handle_admin_clean(query)
            return

    if data in ("sources", "stats", "refresh"):
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return
        if data=="sources": data="admin_sources"
        if data=="stats": data="admin_stats"
        if data=="refresh": data="admin_refresh"
        query.data = data
        await callback_handler(update, context)
        return

    if data == "white":
        agg_key = "WHITE_FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache()
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = (
            f"<b>Белые списки</b> — для жестких ТСПУ (VK, Яндекс)\n"
            f"Всего: <b>{total}</b> • делю по 300\n\n"
            f"Выбери протокол (режим):"
        )
        # Белые — это часть конфигов, но показываем протоколы баннер
        await send_banner_message(update, context, "configs", text, protocol_keyboard(agg_key), is_callback=True, query=query)
        return

    if data == "black":
        agg_key = "BLACK_FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache()
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = (
            f"<b>Черные списки</b> — весь трафик через VPN\n"
            f"Всего: <b>{total}</b> • делю по 300\n\n"
            f"Выбери протокол (режим):"
        )
        await send_banner_message(update, context, "configs", text, protocol_keyboard(agg_key), is_callback=True, query=query)
        return

    if data == "full":
        agg_key = "FULL"
        agg = config.AGGREGATED_SUBS[agg_key]
        base = agg["filename"]
        if base not in AGGREGATED_PROTO_COUNTS:
            await update_cache()
        total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
        text = (
            f"<b>Полный список</b> — все белые + черные\n"
            f"Всего: <b>{total}</b> • делю по 300\n\n"
            f"Выбери протокол (режим):"
        )
        await send_banner_message(update, context, "configs", text, protocol_keyboard(agg_key), is_callback=True, query=query)
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
        if proto == "all":
            fname = base
            display_title = base_title
        else:
            fname = f"{base.replace('.txt','')}_{proto.upper()}.txt"
            display_title = f"{base_title} — {PROTOCOL_LABELS.get(proto, proto.upper())}"
        chunks = AGGREGATED_CHUNKS.get(fname, [])
        cnt = AGGREGATED_CACHE.get(fname, {}).get("count")
        if cnt is None:
            cnt = AGGREGATED_PROTO_COUNTS.get(base, {}).get(proto, 0)
        if cnt == 0:
            await send_banner_message(update, context, "protocols", f"<b>{display_title}</b>\nПока нет конфигов для <b>{PROTOCOL_LABELS.get(proto, proto)}</b> в этом списке.", InlineKeyboardMarkup([[InlineKeyboardButton("«К протоколам»", callback_data=agg_key.lower().replace("_full",""))]]), is_callback=True, query=query)
            return
        raw = get_public_url(fname) or get_raw_url(fname)
        back_target = agg_key.lower().replace("_full","")
        if back_target not in ("white","black","full"):
            back_target = "home"
        text = (
            f"<b>{display_title}</b>\n"
            f"Конфигов: <b>{cnt}</b> • делю по 300\n"
            f"Файл: <code>{raw}</code>\n\n"
            f"Выбери пакет:"
        )
        if chunks:
            kb = chunks_keyboard(fname, chunks, back_data=back_target, back_label="«К протоколам»")
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("«Скачать файл»", callback_data=f"rawfile:{fname}"), InlineKeyboardButton("«Копировать»", callback_data=f"rawcopy:{fname}")],
                [InlineKeyboardButton("«К протоколам»", callback_data=back_target)]
            ])
            text = f"<b>{display_title}</b>\n<code>{raw}</code>\nКонфигов: <b>{cnt}</b>"
        await send_banner_message(update, context, "protocols", text, kb, is_callback=True, query=query)
        return

    if data.startswith("chunk:"):
        fname = data.split(":",1)[1]
        back = "home"
        for proto in PROTOCOLS:
            if f"_{proto.upper()}_" in fname or fname.endswith(f"_{proto.upper()}.txt"):
                for agg_key, agg in config.AGGREGATED_SUBS.items():
                    base = agg["filename"]
                    if fname.startswith(base.replace(".txt","")):
                        back = agg_key.lower().replace("_full","")
                        break
                break
        if back == "home":
            for agg_key, agg in config.AGGREGATED_SUBS.items():
                if fname.startswith(agg["filename"].replace(".txt","")):
                    bk = agg_key.lower().replace("_full","")
                    if bk in ("white","black","full"):
                        back = bk
        await send_chunk_file(query, fname, back_data=back)
        return

    if data.startswith("rawcopy:"):
        fname = data.split(":",1)[1]
        raw = get_public_url(fname) or get_raw_url(fname)
        await query.message.reply_text(f"<code>{raw}</code>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))
        return

    if data.startswith("b64copy:"):
        fname = data.split(":",1)[1]
        path = DATA_DIR / fname
        if not path.exists():
            await query.message.reply_text("Файл не найден")
            return
        content = path.read_text(encoding="utf-8")
        b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        if len(b64) < 4000:
            await query.message.reply_text(f"<code>{b64}</code>", parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]]))
        else:
            b64_path = DATA_DIR / f"{fname.replace('.txt','_base64.txt')}"
            await query.message.reply_text(
                f"Base64 подписка слишком длинная для сообщения, вот файл.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("«Назад»", callback_data="home")]])
            )
            if b64_path.exists():
                await query.message.reply_document(document=open(b64_path, "rb"), filename=b64_path.name)
        return

    if data.startswith("qrfile:"):
        fname = data.split(":",1)[1]
        link = get_public_url(fname) or get_raw_url(fname)
        try:
            qr_bytes = generate_qr_bytes(link)
            await query.message.reply_photo(photo=qr_bytes, caption=f"QR для {fname}\n{link}")
        except Exception as e:
            logger.error(f"qr error: {e}")
            await query.message.reply_text(f"Не удалось создать QR: {e}")
        return

    if data.startswith("rawfile:"):
        fname = data.split(":",1)[1]
        path = DATA_DIR / fname
        if not path.exists():
            await update_cache()
        if path.exists():
            title = fname
            cnt = AGGREGATED_CACHE.get(fname, {}).get("count", "?")
            await query.message.reply_document(document=open(path, "rb"), filename=fname, caption=f"{title} • {cnt}")
        else:
            await query.message.reply_text("Файл не найден")
        return

    if data.startswith("raw:"):
        agg_key = data.split(":",1)[1]
        if agg_key not in config.AGGREGATED_SUBS:
            await query.message.reply_text("Неизвестная подписка")
            return
        fname = config.AGGREGATED_SUBS[agg_key]["filename"]
        title = config.AGGREGATED_SUBS[agg_key]["profile_title"]
        cnt = AGGREGATED_CACHE.get(fname, {}).get("count", "—")
        raw = get_public_url(fname) or get_raw_url(fname)
        text = f"<b>{title}</b>\n<code>{raw}</code>\nКонфигов: <b>{cnt}</b>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("«Скопировать»", callback_data=f"rawcopy:{fname}"), InlineKeyboardButton("«Скачать»", callback_data=f"rawfile:{fname}")],[InlineKeyboardButton("«Назад»", callback_data="home")]])
        await send_banner_message(update, context, "configs", text, kb, is_callback=True, query=query)
        return

    if data.startswith("get:"):
        await query.answer("Раздел перенесен в «Полный список»", show_alert=False)
        await send_banner_message(update, context, "main", config.WELCOME_TEXT, main_keyboard(uid), is_callback=True, query=query)
        return

# ---------- Message handlers ----------

async def message_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "Главное меню":
        uid = update.effective_user.id if update.effective_user else None
        await send_banner_message(update, context, "main", config.WELCOME_TEXT, main_keyboard(uid), is_callback=False)
        return
    if "vless://" in text:
        import re
        m = re.search(r'vless://[^\s]+', text)
        if m:
            link = m.group(0)
            ok, reason = is_valid_vless(link)
            info = parse_vless_info(link)
            if ok:
                await update.message.reply_text(f"VLESS: {info.get('remark')}\n{info.get('host')}:{info.get('port')} • валиден", reply_markup=main_keyboard(update.effective_user.id))
            else:
                await update.message.reply_text(f"Битый VLESS: {reason}")

# ---------- Main ----------

def main():
    if not config.BOT_TOKEN:
        print("BOT_TOKEN не задан")
        return
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print(f"Crimson bot @wtfparsbot интервальный {config.UPDATE_INTERVAL}м")
    async def _preload():
        try:
            await update_cache()
            print(f"Кэш {sum(len(v.get('configs',[])) for v in CACHE.values())}")
        except Exception as e:
            print(f"Ошибка preload: {e}")
    async def _post_init(app):
        if start_health_server:
            try: await start_health_server()
            except Exception as e: logger.warning(e)
        await _preload()
        asyncio.create_task(auto_update_loop(app))
    app.post_init = _post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

async def auto_update_loop(app):
    await asyncio.sleep(10)
    while True:
        try:
            await update_cache()
        except Exception as e:
            logger.error(e)
        await asyncio.sleep(config.UPDATE_INTERVAL*60)

if __name__ == "__main__":
    main()
