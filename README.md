# 🔑 VLESS Parser Bot — чёрные и белые списки

Telegram-бот собирает публичные VLESS-конфигурации из отобранных GitHub-источников, проверяет их и отправляет пользователям готовые `.txt`-файлы для Happ, Hiddify, Streisand, v2rayNG, NekoRay и других совместимых клиентов.

- **⬜ Белые списки** — конфигурации для сетей с режимом «белых списков».
- **⬛ Чёрные списки** — конфигурации для обычных блокировок.
- **Полный список** — объединение обеих групп.

## 🔗 Источники

Парсер загружает конфигурации только с GitHub. Широкий `collection`, внешние сайты, GitVerse, Codeberg, Vercel, S3 и индексный `internet_discovery` удалены.

Белые GitHub feed-ы: zieng2, igareck, CID VPN, ByeWhiteLists 2.0 и Ghost VPN. Чёрные GitHub feed-ы: igareck, Ghost VPN и AetrisVPN.

Дополнительный `github_discovery` использует GitHub Repository Search и Git Tree API. Поиск проходит отдельными группами по VLESS, VPN, proxy/Xray, subscription, config, white/whitelist, black/blacklist и list, поэтому слово `vless` не обязано присутствовать в названии самого файла. Из каждого репозитория проверяется до 3 подходящих feed-ов; за обновление — до 12 репозиториев, 16 feed-ов и 3000 уникальных конфигураций. Даже для смешанного VPN-feed-а в результат попадают исключительно синтаксически валидные VLESS.

### Управление провайдерами из бота

В админ-панели доступны «Провайдеры» и «Поиск источников»:

- поиск показывает только публичные GitHub-файлы и число валидных VLESS;
- каждый кандидат требует явного подтверждения: «Добавить в белые», «Добавить в черные» или «Пропустить»;
- вручную можно отправить прямую GitHub/raw-ссылку на текстовый feed или ссылку на публичный GitHub-репозиторий;
- для репозитория бот ограниченно проверяет наиболее подходящие `.txt`, `.conf`, `.list`, `.json`, `.yaml` и `.yml` файлы;
- динамический provider можно включить, выключить или удалить; одновременно хранится не более 20 динамических провайдеров;
- реестр хранится в `providers.json` и при наличии токена публикуется в GitHub без force-push, поэтому переживает Railway redeploy;
- для записи реестра `GITHUB_TOKEN` должен иметь доступ на запись Contents к `GITHUB_REPO`; без него изменение применяется локально, а бот явно предупреждает, что оно может исчезнуть после redeploy.

Закрытые, платные, украденные и требующие чужой авторизации subscription-ссылки не ищутся и не принимаются. Смешанные публичные feed-ы дают только прошедшие проверку VLESS.

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
- встроенная поддержка: пользователь пишет в бот, администратор отвечает через защищённый relay без публикации личного username;
- включение и выключение уведомлений в админ-панели;
- замена предыдущего уведомления о списках новым сообщением после каждого обновления;
- поиск публичных GitHub VLESS-feed-ов с ручным подтверждением администратора;
- добавление GitHub-файлов и репозиториев, включение, выключение и удаление динамических провайдеров;
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
| `DISCOVERY_MAX_REPOS` | Максимум GitHub-репозиториев за один поиск | `12` |
| `DISCOVERY_MAX_FEEDS` | Максимум найденных файлов за обновление | `16` |
| `DISCOVERY_MAX_FILES_PER_REPO` | Максимум feed-файлов из одного репозитория | `3` |
| `DISCOVERY_MIN_VALID` | Минимум валидных VLESS для принятия feed-а | `1` |
| `DISCOVERY_MAX_CONFIGS` | Максимум конфигураций из автопоиска | `3000` |
| `GITHUB_TOKEN` | Fine-grained токен с Contents read/write для агрегатов и `providers.json` | — |
| `GITHUB_REPO` | Репозиторий агрегатов | `xznexil3/vless-parser-bot` |
| `GITHUB_BRANCH` | Ветка публикации `.txt`-файлов | `main` |
| `PORT` | Порт Railway health-сервера | `8080` |

## 🧠 Pipeline

```text
selected GitHub feeds + approved providers.json + expanded bounded GitHub discovery
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

Тесты покрывают plain/base64/escaped extraction входных данных, VLESS/Reality validation, private host rejection, normalized deduplication, строгие GitHub-фильтры, лимиты discovery, нормализацию GitHub provider-ссылок, реестр динамических провайдеров, endpoint check deduplication, файловый интерфейс, цветовые стили кнопок и очистку устаревших chunks.

## 📂 Структура

```text
vless-parser-bot/
├── src/
│   ├── bot.py          # Telegram-бот и admin cleanup
│   ├── ui.py           # Unicode-эмодзи и Premium-free кнопки
│   ├── config.py       # источники, зеркала и агрегаты
│   ├── parser.py            # fetch/extract/validate/dedup/TCP
│   ├── provider_registry.py # GitHub-only dynamic provider registry
│   ├── subscription.py      # atomic `.txt`/chunk generation
│   └── health.py            # Railway health-check only
├── providers.json           # persistent providers managed from the bot
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
