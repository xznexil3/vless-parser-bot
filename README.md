# 🛰️ VLESS Parser Bot — GitHub-подписки

Telegram-бот собирает и проверяет публичные VLESS-конфигурации из отобранных GitHub-источников. Во вкладке **«Подключить»** пользователь видит raw-ссылку каждого доступного источника, количество его конфигураций и общий итог.

Белые и чёрные вкладки в пользовательском интерфейсе отсутствуют: все подписки показаны одним списком и добавляются в Happ, Hiddify, Streisand, v2rayNG, NekoRay или другой совместимый клиент по raw-ссылке.

## 🔗 Источники

Парсер загружает конфигурации только с GitHub. Широкий `collection`, внешние сайты, GitVerse, Codeberg, Vercel, S3 и индексный `internet_discovery` удалены.

Подключаемые GitHub feed-ы: zieng2, igareck, CID VPN, ByeWhiteLists 2.0, Ghost VPN, AetrisVPN и результаты строгого GitHub discovery. Для каждого источника показываются именно успешно использованные raw-ссылки; недоступные подписки с нулевым результатом пользователю не выводятся.

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
- семантические цвета: синий для навигации, зелёный для подключения, красный для помощи, админки и очистки;
- единая пользовательская вкладка **«Подключить»** вместо белых, чёрных и полного списка;
- рабочие GitHub raw-ссылки отдельно по каждому источнику;
- динамическое количество VLESS по каждой подписке и общий итог по всем источникам;
- автоматическое разбиение длинного списка источников на сообщения в пределах Telegram limit;
- старые кнопки списков и файлов перенаправляются во вкладку «Подключить»;
- автообновление с настраиваемым интервалом;
- внутреннее формирование и атомарная публикация агрегатных snapshots в GitHub;
- ручная строгая проверка одного вставленного VLESS URI.

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

Проект использует `python-telegram-bot 22.8`, поскольку поддержка `style` и `icon_custom_emoji_id` для кнопок появилась в ветке 22.7+.

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
  → «Подключить»: raw URL + source count + total
  → internal aggregate snapshots
```

## 🧪 Тесты

```bash
python -m unittest discover -s tests -v
python -m py_compile src/*.py tests/*.py
```

Тесты покрывают plain/base64/escaped extraction входных данных, VLESS/Reality validation, private host rejection, normalized deduplication, строгие GitHub-фильтры, лимиты discovery, endpoint check deduplication, raw-подписки во вкладке «Подключить», итоговые счётчики, цветовые стили кнопок и очистку устаревших chunks.

## 📂 Структура

```text
vless-parser-bot/
├── src/
│   ├── bot.py          # Telegram-бот и admin cleanup
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
