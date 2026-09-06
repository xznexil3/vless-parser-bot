# 🛰️ VLESS Parser Bot — чёрные и белые списки

Telegram-бот собирает публичные VLESS-конфигурации из отобранных GitHub-источников, проверяет их и отправляет пользователям готовые `.txt`-файлы для Happ, Hiddify, Streisand, v2rayNG, NekoRay и других совместимых клиентов.

- **⬜ Белые списки** — конфигурации для сетей с режимом «белых списков».
- **⬛ Чёрные списки** — конфигурации для обычных блокировок.
- **Полный список** — объединение обеих групп.

## 🔗 Источники

Парсер загружает конфигурации только с GitHub. Широкий `collection`, внешние сайты, GitVerse, Codeberg, Vercel, S3 и индексный `internet_discovery` удалены.

Белые GitHub feed-ы: zieng2, igareck, CID VPN, ByeWhiteLists 2.0 и Ghost VPN. Чёрные GitHub feed-ы: igareck, Ghost VPN и AetrisVPN.

Дополнительный строгий `github_discovery` использует GitHub Repository Search и Git Tree API. Репозиторий и путь файла должны одновременно соответствовать VLESS и дополнительным фильтрам `vpn`, `config`, `subscription`, `list` или `blacklist`. Берётся не больше одного feed-а из одного репозитория, максимум 8 feed-ов и 1200 уникальных конфигураций за обновление. Каждый найденный feed принимается только при наличии минимум 10 валидных VLESS.

## ✅ Извлечение и проверка

Парсер работает только с VLESS и извлекает URI из:

- обычного текста;
- стандартного и URL-safe base64 payload;
- base64 по одной строке;
- JSON- и HTML-экранированного текста.

Для каждого найденного URI проверяются:

- схема `vless://`, canonical UUID, один `uuid@host:port` и порт `1..65535`;
- публичный IPv4/IPv6 или корректное доменное имя (локальные и служебные адреса отбрасываются);
- percent-encoding и query-параметры без конфликтующих дубликатов;
- допустимые `encryption`, `security`, transport `type` и boolean-параметры;
- SNI/Host и обязательные Reality-поля `pbk`/`publicKey`, SNI, формат short ID;
- отсутствие лишнего path в самом URI.

Дубликаты удаляются по нормализованной идентичности подключения: порядок query-параметров и remark после `#` не создают отдельный конфиг.

### Режимы `CHECK_MODE`

| Режим | Поведение |
|---|---|
| `none` | Без фильтрации на уровне `validate_configs` (агрегатор всё равно принимает только валидный VLESS). |
| `syntax` | Полная проверка URI, режим по умолчанию для периодического обновления. |
| `tcp` | Сначала строгая проверка URI, затем ограниченная параллельная TCP-проверка каждого уникального `host:port`. |

Кнопка администратора **«Проверка и очистка»** заново загружает источники, включает режим `tcp`, удаляет невалидные/недоступные конфигурации и перестраивает агрегаты. При временной ошибке provider-а старый кэш этого provider-а не стирается вслепую: его endpoints повторно проверяются.

TCP-проверка подтверждает доступность endpoint-а, но не может гарантировать срок жизни публичного UUID или успешность VLESS-аутентификации без полноценного подключения клиентом.

## ✨ Возможности

- интерактивное Telegram-меню с цветными inline-кнопками Bot API 9.4;
- стандартные Unicode-эмодзи на inline/reply-кнопках и в сообщениях — видны всем пользователям без Premium и Fragment;
- семантические цвета: синий для навигации, зелёный для списков/скачивания, красный для помощи, админки и очистки;
- отдельные `WHITE_FULL.txt`, `BLACK_FULL.txt` и `FULL.txt`;
- отправка только обычных `.txt`-файлов, без URL, base64 и QR;
- памятка по импорту файла в разделе помощи и после выбора пакета;
- автообновление с настраиваемым интервалом;
- атомарная публикация всех агрегатов в GitHub одним commit с удалением устаревших chunks;
- синхронизированная смена карты кнопок и файлов без ссылок на отсутствующие пакеты;
- обработка старых кнопок вроде `BLACK_FULL_6.txt` с переходом к актуальным пакетам;
- ручная строгая проверка одного вставленного VLESS URI;
- отсутствие пользовательских subscription-ссылок: конфигурации выдаются файлами.

## 🚀 Запуск

### 1. Настройка

Создайте бота через [@BotFather](https://t.me/BotFather), затем:

```bash
cp .env.example .env
nano .env
```

### 2. Локально

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python src/bot.py
```

Проект использует `python-telegram-bot 22.8` для поддержки `style` кнопок Bot API 9.4.

### Custom Emoji и режим без Premium

По умолчанию используются обычные Unicode-эмодзи (`⬜`, `⬛`, `📚`, `✅` и т. п.) в тексте кнопок и сообщений. Это не требует ни Telegram Premium у владельца, ни дополнительного username бота на Fragment. Цветовые стили `primary`, `success` и `danger` также применяются отдельно.

Для проверки настоящих custom emoji можно передать ID через JSON-переменную `CUSTOM_EMOJI_IDS`:

```dotenv
CUSTOM_EMOJI_IDS='{"profile":"6039422865189638057","white":"5310169226856644648"}'
```

Ключи соответствуют названиям в `src/ui.py`. Бот автоматически использует `icon_custom_emoji_id` на кнопках и `<tg-emoji>` в HTML-сообщениях. Чтобы получить ID из готового сообщения, просто отправь или перешли это сообщение боту: он ответит найденными идентификаторами.

Telegram проверяет право на стороне API: для кнопок нужен дополнительный username бота на Fragment, а для сообщений, отправляемых ботом напрямую, — Telegram Premium у владельца либо соответствующее право бота. Если право отсутствует, Telegram вернёт ошибку; код не пытается это ограничение обходить.

### 3. Docker

```bash
docker-compose up -d --build
docker logs -f vless-parser-bot
```

## ⚙️ Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `BOT_TOKEN` | Токен от @BotFather | — |
| `ADMIN_ID` / `ADMIN_IDS` | Telegram ID администратора / список ID | значение в config / пусто |
| `CHANNEL_ID` | Канал для уведомлений | `@vpncrimson` |
| `REQUIRED_CHANNEL` | Канал для проверки подписки пользователя | `@vpncrimson` |
| `CHECK_MODE` | `none`, `syntax` или `tcp` | `syntax` |
| `UPDATE_INTERVAL` | Интервал автообновления, минут | `60` |
| `AUTO_DISCOVERY` | Автопоиск новых публичных GitHub VLESS feed-ов | `true` |
| `DISCOVERY_MAX_REPOS` | Максимум GitHub-репозиториев за один поиск | `6` |
| `DISCOVERY_MAX_FEEDS` | Максимум найденных файлов за обновление | `8` |
| `DISCOVERY_MIN_VALID` | Минимум валидных VLESS для принятия feed-а | `10` |
| `DISCOVERY_MAX_CONFIGS` | Максимум конфигураций из автопоиска | `1200` |
| `GITHUB_TOKEN` | Токен для публикации агрегатов | — |
| `GITHUB_REPO` | Репозиторий агрегатов | `xznexil3/vless-parser-bot` |
| `GITHUB_BRANCH` | Ветка публикации `.txt`-файлов | `main` |
| `PORT` | Порт Railway health-сервера | `8080` |

## 🧠 Pipeline

```text
selected GitHub feeds + strict filtered GitHub discovery
  → bounded fetch
  → plain / escaped / base64 extraction
  → strict VLESS validation
  → normalized deduplication
  → optional bounded TCP checks
  → source cache
  → WHITE / BLACK / FULL aggregation
  → atomic `.txt` files and chunks
```

## 🧪 Тесты

```bash
python -m unittest discover -s tests -v
python -m py_compile src/*.py tests/*.py
```

Тесты покрывают plain/base64/escaped extraction входных данных, VLESS/Reality validation, private host rejection, normalized deduplication, строгие GitHub-фильтры, лимиты discovery, endpoint check deduplication, файловый интерфейс, цветовые стили кнопок и очистку устаревших chunks.

## 📂 Структура

```text
vless-parser-bot/
├── src/
│   ├── bot.py          # Telegram-бот и admin cleanup
│   ├── ui.py           # Unicode-эмодзи и Premium-free кнопки
│   ├── config.py       # источники, зеркала и агрегаты
│   ├── parser.py       # fetch/extract/validate/dedup/TCP
│   ├── subscription.py # atomic `.txt`/chunk generation
│   └── health.py       # Railway health-check only
├── tests/
│   └── test_parser.py
├── data/               # runtime-файлы
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## ⚠️ Важно

- Это публичные конфигурации: они могут перестать работать в любой момент.
- Обновляйте подписку перед использованием.
- Не используйте недоверенные VPN endpoints для передачи чувствительных данных.
- Проект не гарантирует доступность сторонних provider-ов.

## 📄 Лицензия

MIT, без гарантий.
