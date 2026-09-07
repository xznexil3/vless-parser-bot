import os
import asyncio
import hashlib
import html
import logging
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import (
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import MessageEntityType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

import config
from ui import button as build_ui_button, icon_text, is_main_menu_text, render_html
from parser import (
    deduplicate_configs,
    extract_configs,
    fetch_all,
    find_public_github_candidates,
    inspect_public_github_repository,
    inspect_public_github_urls,
    is_valid_vless,
    measure_tcp_latency,
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
from paid_subscriptions import (
    FILES_DIR as PAID_FILES_DIR,
    MAX_PAID_PLANS,
    REGISTRY_FILE as PAID_REGISTRY_FILE,
    build_paid_plan,
    load_paid_registry,
    normalize_delivery_url,
    paid_file_path,
    save_paid_registry,
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
CONFIGS_PER_PAGE = 8
USERS_FILE = DATA_DIR / "users.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
PAID_ORDERS_FILE = PAID_REGISTRY_FILE.parent / "paid_orders.json"
MAX_PAID_FILE_BYTES = 8 * 1024 * 1024
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


def load_paid_orders() -> dict:
    try:
        payload = json.loads(PAID_ORDERS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save_paid_orders(orders: dict) -> None:
    try:
        PAID_ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PAID_ORDERS_FILE.with_name(f"{PAID_ORDERS_FILE.name}.tmp")
        temporary.write_text(
            json.dumps(orders, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(PAID_ORDERS_FILE)
    except OSError as exc:
        logger.error("save paid orders failed: %s", exc)


def update_notifications_enabled() -> bool:
    return bool(SETTINGS.get("update_notifications", True))


def set_update_notifications(enabled: bool):
    SETTINGS["update_notifications"] = bool(enabled)
    save_settings()


def ui_button(icon: str, text: str, **kwargs):
    """Build an uncoloured inline button with emoji fallback."""
    kwargs.pop("style", None)
    kwargs.setdefault("custom_emoji_id", config.CUSTOM_EMOJI_IDS.get(icon))
    return build_ui_button(icon, text, **kwargs)


REPLY_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(icon_text("home", "Главное меню"))]],
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
        [ui_button("profile", "«Профиль»", callback_data="profile")],
        [
            ui_button("white", "«Белые списки»", callback_data="white"),
            ui_button("black", "«Черные списки»", callback_data="black"),
        ],
        [ui_button("full", "«Полный список»", callback_data="full")],
        [ui_button("premium", "«Платные подписки»", callback_data="paid")],
        [ui_button("help", "«Помощь»", callback_data="help")],
        [ui_button("chat", "«Поддержка»", callback_data="support")],
    ]
    if user_id and config.is_admin(user_id):
        kb.append([
            ui_button("admin", "«Админ панель»", callback_data="admin_panel")
        ])
    return InlineKeyboardMarkup(kb)


def admin_keyboard():
    notifications_on = update_notifications_enabled()
    notification_label = (
        "«Уведомления: ВКЛ»" if notifications_on else "«Уведомления: ВЫКЛ»"
    )
    return InlineKeyboardMarkup(
        [
            [
                ui_button("stats", "«Статистика»", callback_data="admin_stats"),
                ui_button("refresh", "«Обновить кэш»", callback_data="admin_refresh"),
            ],
            [ui_button("clean", "«Проверка и очистка»", callback_data="admin_clean")],
            [ui_button(
                "notifications",
                notification_label,
                callback_data="admin_notifications",
            )],
            [
                ui_button("sources", "«Провайдеры»", callback_data="admin_providers"),
                ui_button("search", "«Поиск источников»", callback_data="admin_discovery"),
            ],
            [ui_button("premium", "«Платные подписки»", callback_data="admin_paid")],
            [ui_button("info", "«О текущих источниках»", callback_data="admin_sources")],
            [ui_button("back", "«Назад»", callback_data="home")],
        ]
    )


def sub_required_keyboard():
    return InlineKeyboardMarkup(
        [
            [ui_button("subscribe", "«Подписаться на канал»", url=config.CHANNEL_LINK)],
            [ui_button("check", "«Проверить подписку»", callback_data="check_sub")],
        ]
    )


def back_keyboard(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ui_button("back", "«Назад»", callback_data=callback_data)]
    ])


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ui_button(
            "back",
            "«Закрыть поддержку»",
            callback_data="support_close",
        )]
    ])


def support_reply_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [ui_button(
            "chat",
            "«Ответить»",
            callback_data=f"support_reply:{user_id}",
        )]
    ])


def clear_support_state(context):
    context.user_data.pop("support_mode", None)
    context.user_data.pop("support_reply_to", None)


def clear_provider_input_state(context):
    context.user_data.pop("provider_add_mode", None)


def clear_paid_input_state(context):
    context.user_data.pop("paid_plan_input", None)


def paid_plan_records() -> list[dict]:
    return load_paid_registry(PAID_REGISTRY_FILE)


def paid_plan_by_id(plan_id: str) -> dict | None:
    return next(
        (plan for plan in paid_plan_records() if plan.get("id") == plan_id),
        None,
    )


def paid_plan_available(plan: dict | None) -> bool:
    if not plan or not plan.get("enabled", True):
        return False
    has_link = bool(plan.get("delivery_url"))
    relative_file = plan.get("file_path", "")
    has_file = False
    if relative_file:
        candidate = PAID_REGISTRY_FILE.parent / relative_file
        has_file = candidate.is_file() and candidate.parent == PAID_FILES_DIR
    return has_link or has_file


def dynamic_provider_records() -> list[dict]:
    return load_provider_registry(REGISTRY_FILE)


def providers_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [ui_button("add", "«Добавить по ссылке»", callback_data="provider_add_manual")],
        [ui_button("search", "«Найти публичные источники»", callback_data="admin_discovery")],
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
            )
        ])
    rows.append([ui_button("back", "«Назад»", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def provider_detail_keyboard(record: dict) -> InlineKeyboardMarkup:
    enabled = bool(record.get("enabled", True))
    rows = [
        [ui_button("vless", "«Открыть GitHub-файл»", url=record["urls"][0])],
        [ui_button(
            "disabled" if enabled else "enabled",
            "«Выключить»" if enabled else "«Включить»",
            callback_data=f"provider_toggle:{record['id']}",
        )],
        [ui_button(
            "delete",
            "«Удалить»",
            callback_data=f"provider_delete_confirm:{record['id']}",
        )],
        [ui_button("back", "«Назад»", callback_data="admin_providers")],
    ]
    return InlineKeyboardMarkup(rows)


def provider_candidate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            ui_button("white", "«Добавить в белые»", callback_data=f"provider_accept:{token}:white"),
            ui_button("black", "«Добавить в черные»", callback_data=f"provider_accept:{token}:black"),
        ],
        [ui_button("skip", "«Пропустить»", callback_data=f"provider_skip:{token}")],
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


def paid_catalog_text() -> str:
    plans = [plan for plan in paid_plan_records() if paid_plan_available(plan)]
    white = sum(plan["category"] == "white" for plan in plans)
    black = sum(plan["category"] == "black" for plan in plans)
    return (
        "<b>💎 Платные подписки</b>\n\n"
        "Выбери нужный тип подписки. Цифровые подписки внутри Telegram "
        "оплачиваются безопасным встроенным счётом Telegram Stars.\n\n"
        f"⬜ Белые списки: <b>{white}</b>\n"
        f"⬛ Черные списки: <b>{black}</b>"
    )


def paid_catalog_keyboard() -> InlineKeyboardMarkup:
    plans = [plan for plan in paid_plan_records() if paid_plan_available(plan)]
    white = sum(plan["category"] == "white" for plan in plans)
    black = sum(plan["category"] == "black" for plan in plans)
    return InlineKeyboardMarkup([
        [ui_button("white", f"«Белые списки · {white}»", callback_data="paidcat:white")],
        [ui_button("black", f"«Черные списки · {black}»", callback_data="paidcat:black")],
        [ui_button("back", "«Назад»", callback_data="home")],
    ])


def paid_category_keyboard(category: str) -> InlineKeyboardMarkup:
    rows = []
    for plan in paid_plan_records():
        if paid_plan_available(plan) and plan.get("category") == category:
            rows.append([
                ui_button(
                    "premium",
                    f"«{plan['name'][:42]}»",
                    callback_data=f"paidplan:{plan['id']}",
                )
            ])
    rows.append([ui_button("back", "«Назад»", callback_data="paid")])
    return InlineKeyboardMarkup(rows)


def paid_category_text(category: str) -> str:
    title = "⬜ Белые списки" if category == "white" else "⬛ Черные списки"
    count = sum(
        paid_plan_available(plan) and plan.get("category") == category
        for plan in paid_plan_records()
    )
    suffix = "Выбери подписку:" if count else "Сейчас активных предложений нет."
    return f"<b>💎 {title}</b>\n\nДоступно подписок: <b>{count}</b>\n\n{suffix}"


def paid_plan_text(plan: dict) -> str:
    category = "⬜ Белые списки" if plan["category"] == "white" else "⬛ Черные списки"
    methods = [f"⭐ Telegram Stars: <b>{int(plan['stars_price'])} XTR</b>"]
    delivery = []
    if plan.get("file_path") and paid_file_path(plan["id"]).is_file():
        delivery.append("готовый .txt-файл")
    if plan.get("delivery_url"):
        delivery.append("ссылка на подписку")
    description = html.escape(plan.get("description") or "Без дополнительного описания")
    return (
        f"<b>💎 {html.escape(plan['name'])}</b>\n\n"
        f"Категория: <b>{category}</b>\n"
        f"Описание: {description}\n\n"
        "<b>Способы оплаты:</b>\n"
        + "\n".join(methods)
        + "\n\n"
        f"После подтверждения оплаты: <b>{' и '.join(delivery)}</b>."
    )


def paid_plan_keyboard(plan: dict) -> InlineKeyboardMarkup:
    rows = []
    if plan.get("stars_price", 0):
        rows.append([
            ui_button(
                "stars",
                f"«Оплатить {int(plan['stars_price'])} Stars»",
                callback_data=f"paystars:{plan['id']}",
            )
        ])
    rows.append([
        ui_button("back", "«К списку»", callback_data=f"paidcat:{plan['category']}")
    ])
    return InlineKeyboardMarkup(rows)


def admin_paid_text() -> str:
    plans = paid_plan_records()
    enabled = sum(plan.get("enabled", True) for plan in plans)
    return (
        "<b>💎 Платные подписки</b>\n\n"
        f"Добавлено: <b>{len(plans)}</b> • включено: <b>{enabled}</b>\n\n"
        "Каждый план относится к белому или чёрному списку, имеет цену в Stars "
        "и выдаёт проверенный .txt-файл, HTTPS-ссылку либо оба варианта."
    )


def admin_paid_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [ui_button("add", "«Добавить подписку»", callback_data="paidadminadd")],
    ]
    for plan in paid_plan_records():
        category = "⬜" if plan["category"] == "white" else "⬛"
        status = "✅" if plan.get("enabled", True) else "⏸"
        rows.append([
            ui_button(
                "premium",
                f"«{status} {category} {plan['name'][:35]}»",
                callback_data=f"paidadminview:{plan['id']}",
            )
        ])
    rows.append([ui_button("back", "«Назад»", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def admin_paid_add_category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            ui_button("white", "«Белые списки»", callback_data="paidaddcat:white"),
            ui_button("black", "«Черные списки»", callback_data="paidaddcat:black"),
        ],
        [ui_button("back", "«Отмена»", callback_data="admin_paid")],
    ])


def admin_paid_plan_keyboard(plan: dict) -> InlineKeyboardMarkup:
    enabled_label = "«Выключить»" if plan.get("enabled", True) else "«Включить»"
    return InlineKeyboardMarkup([
        [ui_button("file", "«Добавить/заменить .txt»", callback_data=f"paidadminfile:{plan['id']}")],
        [ui_button("network", "«Добавить/изменить ссылку»", callback_data=f"paidadminlink:{plan['id']}")],
        [ui_button("enabled", enabled_label, callback_data=f"paidadmintoggle:{plan['id']}")],
        [ui_button("delete", "«Удалить»", callback_data=f"paidadmindeleteask:{plan['id']}")],
        [ui_button("back", "«Назад»", callback_data="admin_paid")],
    ])


def admin_paid_plan_text(plan: dict) -> str:
    status = "включена" if plan.get("enabled", True) else "выключена"
    file_status = "нет"
    if plan.get("file_path"):
        file_status = "доступен" if paid_file_path(plan["id"]).is_file() else "файл отсутствует"
    link_status = "есть" if plan.get("delivery_url") else "нет"
    return (
        paid_plan_text(plan)
        + "\n\n"
        f"Статус: <b>{status}</b>\n"
        f".txt: <b>{file_status}</b> • ссылка: <b>{link_status}</b>\n"
        f"Создана: <code>{html.escape(plan.get('created_at') or '—')}</code>"
    )


async def persist_paid_plans(
    records: list[dict],
    commit_message: str,
    *,
    file_updates: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Save paid metadata/content only in the configured private storage."""
    file_updates = file_updates or {}
    try:
        PAID_FILES_DIR.mkdir(parents=True, exist_ok=True)
        for relative_path, content in file_updates.items():
            target = PAID_REGISTRY_FILE.parent / relative_path
            if target.parent != PAID_FILES_DIR or target.suffix.lower() != ".txt":
                raise ValueError("Некорректный путь платного файла")
            target.write_text(content, encoding="utf-8")
        save_paid_registry(records, PAID_REGISTRY_FILE)
        # Paid files and delivery links must never be published to the public
        # GitHub repository. Mount PAID_STORAGE_DIR as a Railway volume for
        # persistence across deploys.
        logger.info("%s; paid registry saved to %s", commit_message, PAID_REGISTRY_FILE)
        return True, f"storage:{PAID_REGISTRY_FILE}"
    except Exception as exc:
        logger.exception("paid subscription persistence failed")
        return False, str(exc)[:300]


async def read_paid_document(document) -> str:
    filename = str(getattr(document, "file_name", "") or "")
    size = int(getattr(document, "file_size", 0) or 0)
    if not filename.lower().endswith(".txt"):
        raise ValueError("Нужен файл с расширением .txt")
    if size and size > MAX_PAID_FILE_BYTES:
        raise ValueError("Файл превышает лимит 8 МБ")
    telegram_file = await document.get_file()
    raw = bytes(await telegram_file.download_as_bytearray())
    if len(raw) > MAX_PAID_FILE_BYTES:
        raise ValueError("Файл превышает лимит 8 МБ")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Файл должен быть в UTF-8") from exc
    extracted = extract_configs(text, proto_filter="vless")
    valid = deduplicate_configs(
        [link for link in extracted if is_valid_vless(link)[0]]
    )
    if not valid:
        raise ValueError("В файле нет валидных VLESS-конфигураций")
    return (
        "# Платная подписка Free VPN • Crimson\n"
        f"# Количество: {len(valid)}\n\n"
        + "\n".join(valid)
        + "\n"
    )


async def finish_paid_plan_creation(message, state: dict) -> tuple[bool, str]:
    document = getattr(message, "document", None)
    text = (getattr(message, "text", None) or "").strip()
    delivery_url = ""
    content = ""
    if document:
        content = await read_paid_document(document)
    elif text:
        delivery_url = normalize_delivery_url(text)
    else:
        raise ValueError("Отправь .txt-файл или HTTPS-ссылку")

    plan = build_paid_plan(
        name=state["name"],
        category=state["category"],
        stars_price=state["stars_price"],
        description=state["description"],
        created_at=state["created_at"],
        delivery_url=delivery_url,
        has_file=bool(content),
    )
    records = paid_plan_records()
    if len(records) >= MAX_PAID_PLANS:
        raise ValueError(f"Достигнут лимит: {MAX_PAID_PLANS}")
    if any(existing["id"] == plan["id"] for existing in records):
        raise ValueError("Такая подписка уже существует")
    records.append(plan)
    file_updates = {plan["file_path"]: content} if content else {}
    ok, detail = await persist_paid_plans(
        records,
        f"Add paid subscription {plan['name']}",
        file_updates=file_updates,
    )
    return ok, detail


async def handle_paid_plan_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("paid_plan_input")
    if not isinstance(state, dict):
        return False
    user_id = update.effective_user.id if update.effective_user else 0
    message = update.effective_message
    if not config.is_admin(user_id) or message is None:
        clear_paid_input_state(context)
        return False

    mode = state.get("mode", "create")
    text = (message.text or "").strip()
    try:
        if mode == "replace_file":
            plan = paid_plan_by_id(state.get("plan_id", ""))
            if not plan or not message.document:
                raise ValueError("Отправь новый .txt-файл")
            content = await read_paid_document(message.document)
            records = paid_plan_records()
            for record in records:
                if record["id"] == plan["id"]:
                    record["file_path"] = f"paid_files/{plan['id']}.txt"
            ok, detail = await persist_paid_plans(
                records,
                f"Update paid file {plan['name']}",
                file_updates={f"paid_files/{plan['id']}.txt": content},
            )
            if not ok:
                raise ValueError(detail)
            clear_paid_input_state(context)
            await message.reply_text("✅ .txt-файл проверен и сохранён.", reply_markup=admin_paid_keyboard())
            return True

        if mode == "replace_link":
            plan = paid_plan_by_id(state.get("plan_id", ""))
            if not plan:
                raise ValueError("Подписка больше не найдена")
            delivery_url = normalize_delivery_url(text)
            records = paid_plan_records()
            for record in records:
                if record["id"] == plan["id"]:
                    record["delivery_url"] = delivery_url
            ok, detail = await persist_paid_plans(
                records,
                f"Update paid link {plan['name']}",
            )
            if not ok:
                raise ValueError(detail)
            clear_paid_input_state(context)
            await message.reply_text("✅ Ссылка сохранена.", reply_markup=admin_paid_keyboard())
            return True

        step = state.get("step")
        if step == "name":
            name = " ".join(text.split())[:64]
            if not name:
                raise ValueError("Название не может быть пустым")
            state["name"] = name
            state["step"] = "stars"
            await message.reply_text(
                "⭐ Введи цену целым числом Telegram Stars.",
                reply_markup=back_keyboard("admin_paid"),
            )
            return True
        if step == "stars":
            stars = int(text)
            if not 1 <= stars <= 1_000_000:
                raise ValueError("Цена Stars должна быть от 1 до 1000000")
            state["stars_price"] = stars
            state["step"] = "description"
            await message.reply_text(
                "📝 Отправь описание подписки или <code>-</code>, чтобы пропустить.",
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_paid"),
            )
            return True
        if step == "description":
            state["description"] = "" if text == "-" else " ".join(text.split())[:300]
            state["step"] = "delivery"
            await message.reply_text(
                "📦 Отправь проверяемый <code>.txt</code>-файл с VLESS или HTTPS-ссылку для выдачи после оплаты. "
                "Второй вариант можно добавить позже в карточке плана.",
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard("admin_paid"),
            )
            return True
        if step == "delivery":
            ok, detail = await finish_paid_plan_creation(message, state)
            if not ok:
                raise ValueError(detail)
            clear_paid_input_state(context)
            note = (
                "сохранена в приватном локальном хранилище. Для сохранения после redeploy "
                "подключи Railway Volume к PAID_STORAGE_DIR"
            )
            await message.reply_text(
                f"✅ Платная подписка {note}.",
                reply_markup=admin_paid_keyboard(),
            )
            return True
        raise ValueError("Сценарий добавления устарел. Начни заново")
    except (ValueError, TypeError) as exc:
        await message.reply_text(
            f"❌ {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard("admin_paid"),
        )
        return True


async def deliver_paid_plan(bot_instance, chat_id: int, plan: dict) -> bool:
    delivered = False
    relative_file = plan.get("file_path", "")
    if relative_file:
        path = PAID_REGISTRY_FILE.parent / relative_file
        if path.is_file() and path.parent == PAID_FILES_DIR:
            with path.open("rb") as document:
                await bot_instance.send_document(
                    chat_id=chat_id,
                    document=document,
                    filename=path.name,
                    caption=f"💎 {plan['name']} • оплачено",
                    protect_content=True,
                )
            delivered = True
    if plan.get("delivery_url"):
        await bot_instance.send_message(
            chat_id=chat_id,
            text=render_html(
                f"💎 <b>{html.escape(plan['name'])}</b>\n\nОплата подтверждена. Ссылка доступна по кнопке ниже.",
                config.CUSTOM_EMOJI_IDS,
            ),
            parse_mode=ParseMode.HTML,
            protect_content=True,
            reply_markup=InlineKeyboardMarkup([
                [ui_button("network", "«Открыть подписку»", url=plan["delivery_url"])],
            ]),
        )
        delivered = True
    if not delivered:
        await bot_instance.send_message(
            chat_id=chat_id,
            text="⚠️ Оплата подтверждена, но содержимое временно недоступно. Поддержка уже может проверить заказ.",
            reply_markup=support_keyboard(),
        )
    return delivered


async def paid_precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        prefix, plan_id, expected_user = query.invoice_payload.split(":", 2)
        plan = paid_plan_by_id(plan_id)
        valid = (
            prefix == "paid"
            and paid_plan_available(plan)
            and int(expected_user) == query.from_user.id
            and query.currency == "XTR"
            and query.total_amount == int(plan.get("stars_price", 0))
            and query.total_amount > 0
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="План или цена изменились. Открой подписку заново.")


async def paid_successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    payment = getattr(message, "successful_payment", None)
    if not payment:
        return
    try:
        prefix, plan_id, expected_user = payment.invoice_payload.split(":", 2)
        user_id = update.effective_user.id
        plan = paid_plan_by_id(plan_id)
        if (
            prefix != "paid"
            or int(expected_user) != user_id
            or not plan
            or payment.currency != "XTR"
            or payment.total_amount != int(plan.get("stars_price", 0))
        ):
            raise ValueError("Параметры платежа не совпали")
        order_id = f"stars:{payment.telegram_payment_charge_id}"
        orders = load_paid_orders()
        if orders.get(order_id, {}).get("delivered"):
            await message.reply_text("✅ Этот платёж уже обработан.")
            return
        delivered = await deliver_paid_plan(context.bot, user_id, plan)
        orders[order_id] = {
            "method": "stars",
            "user_id": user_id,
            "plan_id": plan_id,
            "amount": payment.total_amount,
            "currency": "XTR",
            "delivered": delivered,
            "paid_at": datetime.now(MSK).isoformat(),
        }
        save_paid_orders(orders)
        await message.reply_text(
            "✅ Оплата Telegram Stars подтверждена.",
            reply_markup=back_keyboard("paid"),
        )
    except Exception as exc:
        logger.exception("Stars payment fulfillment failed")
        await message.reply_text(
            f"⚠️ Оплата получена, но автоматическая выдача не завершилась. Код: <code>{html.escape(str(exc)[:120])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=support_keyboard(),
        )


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
            [ui_button("back", "«Назад»", callback_data="home")]
        ])
    base = agg["filename"]
    total = AGGREGATED_CACHE.get(base, {}).get("count", "?")
    return InlineKeyboardMarkup(
        [
            [ui_button(
                "vless",
                f"«VLESS · {total}»",
                callback_data=f"proto:{agg_key}:all",
            )],
            [ui_button("back", "«Назад»", callback_data="home")],
        ]
    )


def aggregate_back_target(agg_key: str) -> str:
    candidate = agg_key.lower().replace("_full", "")
    return candidate if candidate in {"white", "black", "full"} else "home"


def config_scope_configs(filename: str) -> list[str]:
    """Return configs from one generated package, never the whole aggregate implicitly."""
    if not aggregate_for_filename(filename)[1]:
        return []
    return list(AGGREGATED_CACHE.get(filename, {}).get("configs", []))


def package_list_keyboard(agg_key: str) -> InlineKeyboardMarkup:
    aggregate = config.AGGREGATED_SUBS.get(agg_key)
    if not aggregate:
        return back_keyboard("home")
    base_filename = aggregate["filename"]
    rows = []
    for index, (filename, _, count) in enumerate(
        AGGREGATED_CHUNKS.get(base_filename, []),
        1,
    ):
        # Never advertise a package unless its active generated file exists.
        if filename not in AGGREGATED_CACHE or local_subscription_path(filename) is None:
            continue
        rows.append([
            ui_button(
                "chunk",
                f"«Пакет {index} · {count} конфигов»",
                callback_data=f"pkgcfg:{filename}:0",
            )
        ])
    rows.append([
        ui_button(
            "back",
            "«Назад»",
            callback_data=aggregate_back_target(agg_key),
        )
    ])
    return InlineKeyboardMarkup(rows)


def package_list_text(agg_key: str) -> str:
    aggregate = config.AGGREGATED_SUBS.get(agg_key, {})
    base_filename = aggregate.get("filename", "")
    chunks = AGGREGATED_CHUNKS.get(base_filename, [])
    available_count = sum(
        filename in AGGREGATED_CACHE and local_subscription_path(filename) is not None
        for filename, _, _ in chunks
    )
    total = AGGREGATED_CACHE.get(base_filename, {}).get("count", 0)
    return (
        f"<b>📦 {html.escape(str(aggregate.get('profile_title', 'VLESS-пакеты')))}</b>\n\n"
        f"Всего конфигов: <b>{total}</b>\n"
        f"Пакетов: <b>{available_count}</b>\n\n"
        f"В каждом пакете до <b>{CHUNK_SIZE}</b> конфигов. Открой пакет, чтобы "
        "посмотреть его конфиги, проверить соединение или скачать этот .txt-файл."
    )


async def show_package_list(query, agg_key: str, *, switch_banner: bool = False):
    text = package_list_text(agg_key)
    keyboard = package_list_keyboard(agg_key)
    if switch_banner:
        await edit_message_with_banner(query, "configs", text, keyboard)
    else:
        await edit_config_message_content(query, text, keyboard)


def current_vless_count() -> int:
    """Return the current unique FULL count, including bootstrap before preload."""
    full = config.AGGREGATED_SUBS.get("FULL", {})
    filename = full.get("filename", "FULL.txt")
    cached = AGGREGATED_CACHE.get(filename, {}).get("count")
    if isinstance(cached, int):
        return cached
    if CACHE:
        links = [
            link
            for source_key in full.get("source_keys", [])
            for link in CACHE.get(source_key, {}).get("configs", [])
        ]
        if links:
            return len(deduplicate_configs(links))
    bootstrap = DATA_DIR.parent / filename
    try:
        match = re.search(
            r"^# Количество:\s*(\d+)\s*$",
            bootstrap.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        if match:
            return int(match.group(1))
    except OSError:
        pass
    return 0


def admin_panel_text() -> str:
    return (
        "<b>⚙️ Админ панель</b>\n\n"
        f"Текущее количество VLESS-конфигов: <b>{current_vless_count()}</b>\n\n"
        "Выбери действие:"
    )


def config_token(link: str) -> str:
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:12]


def is_active_package(filename: str) -> bool:
    aggregate_key, aggregate = aggregate_for_filename(filename)
    if not aggregate_key or not aggregate or filename == aggregate["filename"]:
        return False
    return (
        any(
            current_filename == filename
            for current_filename, _, _ in AGGREGATED_CHUNKS.get(aggregate["filename"], [])
        )
        and filename in AGGREGATED_CACHE
        and local_subscription_path(filename) is not None
    )


def package_scope_for_token(scope: str, token: str = "") -> str | None:
    """Resolve a package filename and migrate callbacks from previous generations."""
    if is_active_package(scope) and (
        not token
        or any(config_token(link) == token for link in config_scope_configs(scope))
    ):
        return scope
    aggregate_key, aggregate = aggregate_for_filename(scope)
    if not aggregate:
        aggregate_key = scope if scope in config.AGGREGATED_SUBS else None
        aggregate = config.AGGREGATED_SUBS.get(scope)
    if not aggregate_key or not aggregate:
        return None
    chunks = AGGREGATED_CHUNKS.get(aggregate["filename"], [])
    if token:
        for filename, _, _ in chunks:
            if (
                is_active_package(filename)
                and any(
                    config_token(link) == token
                    for link in config_scope_configs(filename)
                )
            ):
                return filename
    return next(
        (filename for filename, _, _ in chunks if is_active_package(filename)),
        None,
    )


def package_list_callback(filename: str) -> str:
    aggregate_key, _ = aggregate_for_filename(filename)
    return f"pkglist:{aggregate_key}" if aggregate_key else "home"


def file_return_callback(filename: str) -> str:
    aggregate_key, aggregate = aggregate_for_filename(filename)
    if not aggregate_key or not aggregate:
        return "home"
    if filename == aggregate["filename"]:
        return f"pkglist:{aggregate_key}"
    return f"pkgcfg:{filename}:0"


def config_by_token(filename: str, token: str):
    for index, link in enumerate(config_scope_configs(filename)):
        if config_token(link) == token:
            return index, link
    return None, None


def _config_page(filename: str, requested_page: int) -> tuple[list[str], int, int]:
    configs = config_scope_configs(filename)
    page_count = max(1, (len(configs) + CONFIGS_PER_PAGE - 1) // CONFIGS_PER_PAGE)
    page = max(0, min(requested_page, page_count - 1))
    return configs, page, page_count


def config_list_keyboard(filename: str, requested_page: int = 0) -> InlineKeyboardMarkup:
    configs, page, page_count = _config_page(filename, requested_page)
    start = page * CONFIGS_PER_PAGE
    rows = []
    for index, link in enumerate(configs[start:start + CONFIGS_PER_PAGE], start=start):
        info = parse_vless_info(link)
        raw_name = str(info.get("remark") or info.get("host") or "VLESS")
        if info.get("error") or raw_name.lower().startswith("vless://"):
            raw_name = "VLESS-конфиг"
        name = " ".join(raw_name.split())
        name = name[:42] + ("…" if len(name) > 42 else "")
        rows.append([
            ui_button(
                "vless",
                f"«{index + 1}. {name}»",
                callback_data=f"cfgdetail:{filename}:{config_token(link)}:{page}",
            )
        ])
    navigation = []
    if page > 0:
        navigation.append(ui_button(
            "back",
            "«Предыдущая»",
            callback_data=f"pkgcfg:{filename}:{page - 1}",
        ))
    if page + 1 < page_count:
        navigation.append(ui_button(
            "next",
            "«Следующая»",
            callback_data=f"pkgcfg:{filename}:{page + 1}",
        ))
    if navigation:
        rows.append(navigation)
    if filename in AGGREGATED_CACHE and local_subscription_path(filename) is not None:
        rows.append([
            ui_button(
                "download",
                "«Скачать этот .txt»",
                callback_data=f"rawfile:{filename}",
            )
        ])
    rows.append([
        ui_button(
            "back",
            "«К пакетам»",
            callback_data=package_list_callback(filename),
        )
    ])
    return InlineKeyboardMarkup(rows)


def config_list_text(filename: str, requested_page: int = 0) -> str:
    _, aggregate = aggregate_for_filename(filename)
    configs, page, page_count = _config_page(filename, requested_page)
    chunks = AGGREGATED_CHUNKS.get(aggregate["filename"], []) if aggregate else []
    package_number = next(
        (index for index, (name, _, _) in enumerate(chunks, 1) if name == filename),
        1,
    )
    title = aggregate.get("profile_title", "VLESS-конфиги") if aggregate else "VLESS-конфиги"
    return (
        f"<b>🧾 {html.escape(str(title))} — пакет {package_number}</b>\n\n"
        f"Конфигов в пакете: <b>{len(configs)}</b>\n"
        f"Страница: <b>{page + 1}/{page_count}</b>\n\n"
        "Выбери конфиг, чтобы посмотреть параметры и проверить соединение."
    )


def config_detail_text(
    filename: str,
    link: str,
    index: int,
    *,
    ping_status=None,
) -> str:
    configs = config_scope_configs(filename)
    info = parse_vless_info(link)
    raw_remark = str(info.get("remark") or "Без названия")
    if info.get("error") or raw_remark.lower().startswith("vless://"):
        raw_remark = "Без названия"
    remark = html.escape(raw_remark)
    host = html.escape(str(info.get("host") or "?"))
    port = html.escape(str(info.get("port") or "?"))
    transport = html.escape(str(info.get("type") or "tcp"))
    security = html.escape(str(info.get("security") or "none"))
    sni = html.escape(str(info.get("sni") or "—"))
    if ping_status == "checking":
        ping_line = "⏳ Проверяю TCP-соединение и задержку…"
    elif isinstance(ping_status, int):
        ping_line = f"✅ TCP-соединение установлено • <b>{ping_status} мс</b>"
    elif ping_status == "failed":
        ping_line = "❌ TCP-соединение не установлено за 3 секунды"
    else:
        ping_line = "📶 Соединение ещё не проверялось"
    return (
        f"<b>🔗 VLESS-конфиг {index + 1}/{len(configs)}</b>\n\n"
        f"Название: <b>{remark}</b>\n"
        f"Сервер: <code>{host}:{port}</code>\n"
        f"Транспорт: <b>{transport}</b>\n"
        f"Защита: <b>{security}</b>\n"
        f"SNI: <code>{sni}</code>\n\n"
        f"{ping_line}\n\n"
        "<i>Проверка измеряет установку TCP-соединения, включая DNS, но не выполняет VLESS-авторизацию.</i>"
    )


def config_detail_keyboard(
    filename: str,
    link: str,
    page: int,
) -> InlineKeyboardMarkup:
    token = config_token(link)
    return InlineKeyboardMarkup([
        [ui_button(
            "ping",
            "«Проверить соединение»",
            callback_data=f"cfgping:{filename}:{token}:{page}",
        )],
        [ui_button(
            "back",
            "«К списку конфигов»",
            callback_data=f"pkgcfg:{filename}:{page}",
        )],
    ])


async def edit_config_message_content(query, text: str, reply_markup):
    """Edit only caption/text, avoiding a redundant banner upload on ping/pages."""
    rendered = render_html(text, config.CUSTOM_EMOJI_IDS)
    try:
        if getattr(query.message, "photo", None):
            await query.message.edit_caption(
                caption=rendered,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        else:
            await query.message.edit_text(
                rendered,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
    except Exception as exc:
        logger.warning("config message edit failed: %s", exc)
        await edit_message_with_banner(query, "configs", text, reply_markup)


async def show_config_list(
    query,
    filename: str,
    page: int = 0,
    *,
    switch_banner: bool = False,
):
    text = config_list_text(filename, page)
    keyboard = config_list_keyboard(filename, page)
    if switch_banner:
        await edit_message_with_banner(query, "configs", text, keyboard)
    else:
        await edit_config_message_content(query, text, keyboard)


async def show_config_detail(query, filename: str, token: str, page: int, ping_status=None):
    index, link = config_by_token(filename, token)
    if link is None:
        await edit_config_message_content(
            query,
            "<b>⚠️ Конфиг больше не найден</b>\n\nСписки успели обновиться. Открой актуальный пакет.",
            back_keyboard(f"pkgcfg:{filename}:{page}"),
        )
        return None
    await edit_config_message_content(
        query,
        config_detail_text(filename, link, index, ping_status=ping_status),
        config_detail_keyboard(filename, link, page),
    )
    return link

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
            chunk_offset = 0
            for cfname, ctitle, cnt, ccontent in chunk_infos:
                chunk_configs = all_cfgs[chunk_offset:chunk_offset + cnt]
                chunk_offset += cnt
                results[cfname] = {
                    "content": ccontent,
                    "count": cnt,
                    "configs": chunk_configs,
                    "is_chunk": True,
                }
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
            "configs": list(info.get("configs", [])),
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
    difference = int(new_total) - int(old_total)
    difference_text = f"+{difference}" if difference > 0 else str(difference)
    text = (
        f"✅  • Списки обновлены ({difference_text})\n\n"
        f"🕔{now.strftime('%d.%m.%Y %H:%M МСК')}"
    )
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
    old_total = current_vless_count()
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
    try:
        agg, chunk_map, proto_counts_map = build_aggregated_configs()
        full_filename = config.AGGREGATED_SUBS["FULL"]["filename"]
        new_total = int(agg.get(full_filename, {}).get("count", 0))
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
    if aggregate and AGGREGATED_CACHE:
        if filename not in AGGREGATED_CACHE:
            # The name belongs to an old aggregate generation. Do not silently
            # serve a stale committed chunk after the active map has switched.
            return None
        generated_path = DATA_DIR / filename
        return generated_path if generated_path.is_file() else None
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
    clear_paid_input_state(context)
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
    clear_paid_input_state(context)
    uid = update.effective_user.id if update.effective_user else None
    if not await is_user_subscribed(uid, context.bot):
        await update.message.reply_text(f"📢 Подпишись на {config.CHANNEL_USERNAME}, чтобы продолжить", reply_markup=sub_required_keyboard())
        return
    await send_initial_banner(update, "help", config.HELP_TEXT, main_keyboard(uid))


async def paysupport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Always allow payment support, including after a user leaves the channel."""
    clear_provider_input_state(context)
    clear_paid_input_state(context)
    context.user_data["support_mode"] = True
    context.user_data.pop("support_reply_to", None)
    await update.effective_message.reply_text(
        render_html(
            "<b>💬 Поддержка по оплате</b>\n\n"
            "Опиши проблему с платежом Telegram Stars. Если сохранился чек или ID операции, "
            "приложи его к сообщению. Ответ придёт в этот чат.",
            config.CUSTOM_EMOJI_IDS,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=support_keyboard(),
    )


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
        clear_paid_input_state(context)
        uid = update.effective_user.id if update.effective_user else None
        if not await is_user_subscribed(uid, context.bot):
            await update.message.reply_text(f"📢 Подпишись на {config.CHANNEL_USERNAME}", reply_markup=sub_required_keyboard())
            return True
        await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return True
    return False

async def show_outdated_file(query, fname: str, back_data: str = "home"):
    aggregate_key, aggregate = aggregate_for_filename(fname)
    if aggregate:
        text = (
            f"<b>⚠️ {fname} больше не существует</b>\n\n"
            "Количество конфигураций изменилось, поэтому пакеты были пересобраны. "
            "Выбери актуальный пакет ниже."
        )
        keyboard = package_list_keyboard(aggregate_key)
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
            )],
            [ui_button("back", "«Назад»", callback_data=back_data)],
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

    total_before = current_vless_count()
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
                    [ui_button("back", "«Назад»", callback_data="admin_panel")],
                    [ui_button("home", "«Главное меню»", callback_data="home")],
                ]
            ),
        )
        return

    total_after = current_vless_count()
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
        f"Уникальных VLESS было: <b>{total_before}</b>",
        f"Уникальных VLESS стало: <b>{total_after}</b>",
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
                [ui_button("back", "«Назад»", callback_data="admin_panel")],
                [ui_button("home", "«Главное меню»", callback_data="home")],
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

    if data == "paid":
        clear_support_state(context)
        clear_provider_input_state(context)
        clear_paid_input_state(context)
        await edit_message_with_banner(
            query,
            "main",
            paid_catalog_text(),
            paid_catalog_keyboard(),
        )
        return

    if data.startswith("paidcat:"):
        clear_support_state(context)
        category = data.split(":", 1)[1]
        if category not in {"white", "black"}:
            await query.message.reply_text("Неизвестная категория подписок.")
            return
        await edit_message_with_banner(
            query,
            "main",
            paid_category_text(category),
            paid_category_keyboard(category),
        )
        return

    if data.startswith("paidplan:"):
        clear_support_state(context)
        plan = paid_plan_by_id(data.split(":", 1)[1])
        if not paid_plan_available(plan):
            await query.message.reply_text(
                "Эта подписка больше недоступна.",
                reply_markup=back_keyboard("paid"),
            )
            return
        await edit_message_with_banner(
            query,
            "main",
            paid_plan_text(plan),
            paid_plan_keyboard(plan),
        )
        return

    if data.startswith("paystars:"):
        plan = paid_plan_by_id(data.split(":", 1)[1])
        if not paid_plan_available(plan) or not plan.get("stars_price"):
            await query.message.reply_text("Оплата Stars для этого плана недоступна.")
            return
        await context.bot.send_invoice(
            chat_id=uid,
            title=plan["name"][:32],
            description=(plan.get("description") or "Платная VLESS-подписка")[:255],
            payload=f"paid:{plan['id']}:{uid}",
            currency="XTR",
            prices=[LabeledPrice(plan["name"][:32], int(plan["stars_price"]))],
            provider_token="",
            protect_content=True,
        )
        return

    if data == "support":
        clear_provider_input_state(context)
        clear_paid_input_state(context)
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
        clear_paid_input_state(context)
        await edit_message_with_banner(query, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return

    if data.startswith("support_reply:"):
        clear_provider_input_state(context)
        clear_paid_input_state(context)
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

    if data == "admin_paid" or data.startswith(("paidadmin", "paidaddcat:")):
        clear_provider_input_state(context)
        if not config.is_admin(uid):
            await query.answer("Только для админа", show_alert=True)
            return

        if data == "admin_paid":
            clear_paid_input_state(context)
            await edit_message_with_banner(query, "main", admin_paid_text(), admin_paid_keyboard())
            return

        if data == "paidadminadd":
            clear_paid_input_state(context)
            await edit_message_with_banner(
                query,
                "main",
                "<b>➕ Новая платная подписка</b>\n\nВыбери категорию:",
                admin_paid_add_category_keyboard(),
            )
            return

        if data.startswith("paidaddcat:"):
            category = data.split(":", 1)[1]
            if category not in {"white", "black"}:
                await query.message.reply_text("Неизвестная категория.")
                return
            context.user_data["paid_plan_input"] = {
                "mode": "create",
                "step": "name",
                "category": category,
                "created_at": datetime.now(MSK).isoformat(),
            }
            await edit_message_with_banner(
                query,
                "main",
                "<b>➕ Новая платная подписка</b>\n\nОтправь короткое название плана.",
                back_keyboard("admin_paid"),
            )
            return

        plan_id = data.split(":", 1)[1] if ":" in data else ""
        plan = paid_plan_by_id(plan_id)
        if not plan:
            await query.message.reply_text(
                "Платная подписка не найдена.",
                reply_markup=admin_paid_keyboard(),
            )
            return

        if data.startswith("paidadminview:"):
            clear_paid_input_state(context)
            await edit_message_with_banner(
                query,
                "main",
                admin_paid_plan_text(plan),
                admin_paid_plan_keyboard(plan),
            )
            return

        if data.startswith("paidadminfile:"):
            context.user_data["paid_plan_input"] = {
                "mode": "replace_file",
                "plan_id": plan_id,
            }
            await edit_message_with_banner(
                query,
                "main",
                f"<b>📄 {html.escape(plan['name'])}</b>\n\n"
                "Отправь новый UTF-8 <code>.txt</code>-файл. Бот оставит только валидные VLESS.",
                back_keyboard(f"paidadminview:{plan_id}"),
            )
            return

        if data.startswith("paidadminlink:"):
            context.user_data["paid_plan_input"] = {
                "mode": "replace_link",
                "plan_id": plan_id,
            }
            await edit_message_with_banner(
                query,
                "main",
                f"<b>🔗 {html.escape(plan['name'])}</b>\n\nОтправь новую HTTPS-ссылку для выдачи после оплаты.",
                back_keyboard(f"paidadminview:{plan_id}"),
            )
            return

        if data.startswith("paidadmintoggle:"):
            records = paid_plan_records()
            for record in records:
                if record["id"] == plan_id:
                    record["enabled"] = not record.get("enabled", True)
                    plan = record
                    break
            ok, detail = await persist_paid_plans(
                records,
                f"{'Enable' if plan['enabled'] else 'Disable'} paid subscription {plan['name']}",
            )
            if not ok:
                await query.message.reply_text(
                    f"❌ Не удалось сохранить изменение. <code>{html.escape(detail)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            await edit_message_with_banner(query, "main", admin_paid_text(), admin_paid_keyboard())
            return

        if data.startswith("paidadmindeleteask:"):
            await edit_message_with_banner(
                query,
                "main",
                f"<b>Удалить платную подписку?</b>\n\n{html.escape(plan['name'])}",
                InlineKeyboardMarkup([
                    [ui_button("delete", "«Да, удалить»", callback_data=f"paidadmindelete:{plan_id}")],
                    [ui_button("back", "«Отмена»", callback_data=f"paidadminview:{plan_id}")],
                ]),
            )
            return

        if data.startswith("paidadmindelete:"):
            records = [record for record in paid_plan_records() if record["id"] != plan_id]
            ok, detail = await persist_paid_plans(
                records,
                f"Remove paid subscription {plan['name']}",
            )
            if not ok:
                await query.message.reply_text(
                    f"❌ Не удалось удалить подписку. <code>{html.escape(detail)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            if plan.get("file_path"):
                try:
                    paid_file_path(plan_id).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not remove paid file: %s", exc)
            await edit_message_with_banner(query, "main", admin_paid_text(), admin_paid_keyboard())
            return

    if data in {"admin_providers", "provider_add_manual", "admin_discovery"} or data.startswith("provider_"):
        clear_paid_input_state(context)
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
                "Ищу публичные GitHub-репозитории и файлы по VPN, VLESS, proxy/Xray, "
                "config, subscription, white/black и list. Из содержимого беру только валидные VLESS. "
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
                    [ui_button("delete", "«Да, удалить»", callback_data=f"provider_delete:{provider_id}")],
                    [ui_button("back", "«Отмена»", callback_data=f"provider_view:{provider_id}")],
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
    clear_paid_input_state(context)

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
        await edit_message_with_banner(query, "main", admin_panel_text(), admin_keyboard())
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
            lines = ["<b>📊 Статистика</b>"]
            for key, source_data in CACHE.items():
                count = len(source_data.get("configs", []))
                lines.append(f"{key}: <b>{count}</b>")
            lines.append(
                f"\nУникальных VLESS в полном списке: <b>{current_vless_count()}</b>"
            )
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
        except ValueError:
            await query.message.reply_text("Некорректный список конфигов.")
            return
        if proto != "all" or agg_key not in config.AGGREGATED_SUBS:
            await query.message.reply_text("Неизвестный список")
            return
        await show_package_list(query, agg_key, switch_banner=True)
        return

    if data.startswith("pkglist:") or data.startswith("packages:"):
        agg_key = data.split(":", 1)[1]
        if agg_key not in config.AGGREGATED_SUBS:
            await query.message.reply_text("Неизвестный список")
            return
        await show_package_list(query, agg_key)
        return

    if data.startswith("pkgcfg:"):
        try:
            _, filename, page_text = data.split(":", 2)
            page = int(page_text)
        except (TypeError, ValueError):
            await query.message.reply_text("Некорректная страница пакета.")
            return
        if not is_active_package(filename):
            aggregate_key, _ = aggregate_for_filename(filename)
            if aggregate_key:
                await show_package_list(query, aggregate_key)
            else:
                await query.message.reply_text("Пакет больше не существует.")
            return
        await show_config_list(query, filename, page)
        return

    # Compatibility with config-list buttons from the previous aggregate-wide UI.
    if data.startswith("cfglist:"):
        try:
            _, agg_key, page_text = data.split(":", 2)
            old_page = max(0, int(page_text))
        except (TypeError, ValueError):
            await query.message.reply_text("Некорректная страница конфигов.")
            return
        aggregate = config.AGGREGATED_SUBS.get(agg_key)
        if not aggregate:
            await query.message.reply_text("Неизвестный список")
            return
        chunks = AGGREGATED_CHUNKS.get(aggregate["filename"], [])
        global_offset = old_page * CONFIGS_PER_PAGE
        chunk_index = min(global_offset // CHUNK_SIZE, max(0, len(chunks) - 1))
        if not chunks:
            await show_package_list(query, agg_key)
            return
        filename = chunks[chunk_index][0]
        if not is_active_package(filename):
            await show_package_list(query, agg_key)
            return
        local_page = (global_offset - chunk_index * CHUNK_SIZE) // CONFIGS_PER_PAGE
        await show_config_list(query, filename, local_page)
        return

    if data.startswith("cfgdetail:"):
        try:
            _, scope, token, page_text = data.split(":", 3)
            page = int(page_text)
        except (TypeError, ValueError):
            await query.message.reply_text("Некорректный конфиг.")
            return
        filename = package_scope_for_token(scope, token)
        if not filename:
            aggregate_key = scope if scope in config.AGGREGATED_SUBS else None
            if aggregate_key:
                await show_package_list(query, aggregate_key)
            else:
                await query.message.reply_text("Конфиг или пакет больше не найден.")
            return
        await show_config_detail(query, filename, token, page)
        return

    if data.startswith("cfgping:"):
        try:
            _, scope, token, page_text = data.split(":", 3)
            page = int(page_text)
        except (TypeError, ValueError):
            await query.message.reply_text("Некорректный конфиг.")
            return
        filename = package_scope_for_token(scope, token)
        if not filename:
            await query.message.reply_text("Конфиг или пакет больше не найден.")
            return
        link = await show_config_detail(
            query,
            filename,
            token,
            page,
            ping_status="checking",
        )
        if link is None:
            return
        info = parse_vless_info(link)
        try:
            port = int(info.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        latency_ms = await measure_tcp_latency(
            str(info.get("host") or ""),
            port,
            timeout=3.0,
        )
        await show_config_detail(
            query,
            filename,
            token,
            page,
            ping_status=latency_ms if latency_ms is not None else "failed",
        )
        return

    if data.startswith("chunk:"):
        fname = data.split(":", 1)[1]
        await send_chunk_file(
            query,
            fname,
            back_data=file_return_callback(fname),
        )
        return

    # Compatibility for buttons left in old Telegram messages: every old
    # link/base64/QR action now sends the corresponding .txt file instead.
    if data.startswith(("rawcopy:", "b64copy:", "qrfile:", "rawfile:")):
        fname = data.split(":", 1)[1]
        await send_chunk_file(
            query,
            fname,
            back_data=file_return_callback(fname),
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

    username_value = getattr(user, "username", None)
    username = f"@{html.escape(str(username_value))}" if username_value else "не указан"
    nickname_value = (
        getattr(user, "full_name", None)
        or getattr(user, "first_name", None)
        or "не указан"
    )
    nickname = html.escape(str(nickname_value))
    delivered = 0
    for admin_id in sorted(config.ADMIN_IDS):
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=render_html(
                    "💬 <b>Новое обращение в поддержку</b>\n\n"
                    f"🆔 ID пользователя: <code>{user_id}</code>\n\n"
                    f"🔗 Юзернейм: <b>{username}</b>\n\n"
                    f"👤 Ник: <b>{nickname}</b>",
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

    if (
        context.user_data.get("support_mode")
        or context.user_data.get("support_reply_to")
    ) and await relay_support_message(update, context):
        return

    if not await is_user_subscribed(uid, context.bot):
        await update.message.reply_text(f"📢 Подпишись на {config.CHANNEL_USERNAME}, чтобы пользоваться ботом", reply_markup=sub_required_keyboard())
        return

    text = (update.message.text or "").strip()
    if is_main_menu_text(text):
        clear_support_state(context)
        clear_provider_input_state(context)
        clear_paid_input_state(context)
        await send_initial_banner(update, "main", config.WELCOME_TEXT, main_keyboard(uid))
        return

    if await handle_paid_plan_input(update, context):
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
    app.add_handler(CommandHandler("paysupport", paysupport_cmd))
    app.add_handler(PreCheckoutQueryHandler(paid_precheckout_handler))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, paid_successful_payment_handler)
    )
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
