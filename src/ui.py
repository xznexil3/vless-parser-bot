"""UI helpers for Bot API 9.4 emoji and styled buttons.

With no configured IDs, the module uses standard Unicode emoji.  If
``CUSTOM_EMOJI_IDS`` is filled in, it sends ``icon_custom_emoji_id`` on buttons
and renders ``<tg-emoji>`` tags in HTML messages.  Telegram entitlement is
checked server-side; this module does not attempt to bypass it.
"""

from telegram import InlineKeyboardButton


ICONS = {
    "home": "🏠",
    "profile": "👤",
    "white": "⬜",
    "black": "⬛",
    "full": "📚",
    "help": "❔",
    "admin": "⚙️",
    "subscribe": "📢",
    "check": "✅",
    "back": "◀️",
    "download": "⬇️",
    "vless": "🔗",
    "stats": "📊",
    "refresh": "🔄",
    "clean": "🧹",
    "sources": "🗂️",
    "chunk": "📦",
    "success": "✅",
    "warning": "⚠️",
    "info": "ℹ️",
    "network": "🛰️",
    "lock": "🔒",
    "shield": "🔐",
    "file": "📄",
    "clipboard": "📋",
    "category": "🎛️",
    "puzzle": "🧩",
    "chat": "💬",
    "search": "🔎",
    "calendar": "🗓️",
    "id": "🆔",
}


def icon_text(icon: str, text: str) -> str:
    """Prefix visible text with a regular Unicode emoji."""
    prefix = ICONS.get(icon, "")
    return f"{prefix} {text}" if prefix else text


def button(
    icon: str,
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
    custom_emoji_id: str | None = None,
) -> InlineKeyboardButton:
    """Build a Bot API 9.4 button with optional custom emoji.

    When ``custom_emoji_id`` is configured, Telegram renders it in the icon
    slot and the visible label contains no Unicode fallback.  With no ID, the
    helper uses a regular Unicode emoji so development and fallback mode remain
    usable.  Telegram may reject a custom ID when the bot is not entitled to
    use custom emoji; this helper deliberately does not try to bypass that
    server-side permission.
    """
    kwargs = {"text": text if custom_emoji_id else icon_text(icon, text)}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style is not None:
        kwargs["style"] = style
    if custom_emoji_id:
        kwargs["icon_custom_emoji_id"] = custom_emoji_id
    return InlineKeyboardButton(**kwargs)


def render_html(text: str, custom_emoji_ids: dict[str, str] | None = None) -> str:
    """Replace configured Unicode markers with Bot API custom-emoji tags.

    Existing messages keep their readable Unicode form until an ID is added
    to configuration.  The returned value is intended for ``parse_mode=HTML``.
    """
    if not text or not custom_emoji_ids:
        return text
    rendered = text
    seen_emojis = set()
    # Longer glyphs first, and never wrap the same fallback twice when two
    # semantic keys happen to share one Unicode character.
    configured = sorted(custom_emoji_ids.items(), key=lambda item: len(ICONS.get(item[0], "")), reverse=True)
    for icon, custom_id in configured:
        emoji = ICONS.get(icon)
        if not emoji or not custom_id or emoji in seen_emojis:
            continue
        seen_emojis.add(emoji)
        rendered = rendered.replace(
            emoji,
            f'<tg-emoji emoji-id="{custom_id}">{emoji}</tg-emoji>',
        )
    return rendered


def is_main_menu_text(text: str | None) -> bool:
    """Accept both the old and the emoji-decorated reply-button text."""
    if not text:
        return False
    return text.strip().removeprefix(ICONS["home"]).strip() == "Главное меню"


__all__ = ["ICONS", "button", "icon_text", "is_main_menu_text", "render_html"]
