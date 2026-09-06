# 🛰️ VLESS Parser Bot — чёрные и белые списки

Telegram-бот собирает публичные VLESS-конфигурации, проверяет их и публикует plain/base64-подписки для Happ, Hiddify, Streisand, v2rayNG, NekoRay и других совместимых клиентов.

- **⬜ Белые списки** — конфигурации для сетей с режимом «белых списков».
- **⬛ Чёрные списки** — конфигурации для обычных блокировок.
- **Полный список** — объединение обеих групп.

## 🔗 Источники

В белый агрегат входят все 11 провайдеров:

| № | Провайдер | Основной feed |
|---:|---|---|
| 1 | Сборник подписок против БС | VALCHIK / Codeberg `obhod_WL` |
| 2 | zieng2 | `zieng2/wl` |
| 3 | EtoNeYa | `etoneya.su/whitelist` |
| 4 | igareck | `WHITE-CIDR-RU-all.txt` |
| 5 | CID VPN | `CidVpn/cid-vpn-config` + CID White |
| 6 | wrtrmmu | nowmeow whitelist API |
| 7 | wlrus.lol | wlrus.lol, GitVerse и S3-зеркало |
| 8 | ByeWhiteLists 2.0 | `ByeWhiteLists/ByeWhiteLists2` |
| 9 | Vercel | `white-lists.vercel.app/api/filter?code=RU` |
| 10 | Ghost-vpn.ru | две WhiteListVpn-подписки |
| 11 | VPN bolt | `RUVIPIEN/russian-white-bolt_fix` |

Точные URL и порядок зеркал находятся в [`src/config.py`](src/config.py). Для зеркал используется стратегия `first_available`; независимые части одного источника загружаются стратегией `all`.

> На момент последней проверки endpoint Vercel возвращает HTTP 404. Он сохранён как канонический источник и автоматически снова начнёт участвовать в агрегате, если deployment восстановят. Ошибка одного провайдера не останавливает остальные источники.

Чёрные feed-ы igareck, EtoNeYa и Ghost VPN зарегистрированы отдельно и не смешиваются с белым агрегатом.

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
- семантические цвета: синий для навигации, зелёный для списков/скачивания, красный для помощи, админки и очистки;
- отдельные `WHITE_FULL.txt`, `BLACK_FULL.txt` и `FULL.txt`;
- plain-файлы, base64-представление, chunks и QR-коды;
- автообновление с настраиваемым интервалом;
- атомарная публикация всех агрегатов в GitHub одним commit с удалением устаревших chunks;
- синхронизированная смена карты кнопок и файлов без ссылок на отсутствующие пакеты;
- обработка старых кнопок вроде `BLACK_FULL_6.txt` с переходом к актуальным пакетам;
- ручная строгая проверка одного вставленного VLESS URI;
- Railway HTTP endpoint `/sub/<file>.txt` и `/sub/<file>.txt/b64` для runtime-подписок.

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
| `GITHUB_TOKEN` | Токен для публикации агрегатов | — |
| `GITHUB_REPO` | Репозиторий агрегатов | `xznexil3/vless-parser-bot` |
| `GITHUB_BRANCH` | Ветка публикации | `main` |
| `PUBLIC_URL` | Публичный адрес runtime-подписок без завершающего `/` | определяется через Railway |
| `RAILWAY_PUBLIC_DOMAIN` | Railway domain; автоматически превращается в `PUBLIC_URL` | Railway variable |
| `PORT` | Порт health/subscription HTTP-сервера | `8080` |

## 🧠 Pipeline

```text
provider/mirror
  → bounded fetch
  → plain / escaped / base64 extraction
  → strict VLESS validation
  → normalized deduplication
  → optional bounded TCP checks
  → source cache
  → WHITE / BLACK / FULL aggregation
  → plain + base64 + chunks
```

## 🧪 Тесты

```bash
python -m unittest discover -s tests -v
python -m py_compile src/*.py tests/*.py
```

Тесты покрывают plain/base64/escaped extraction, VLESS/Reality validation, private host rejection, normalized deduplication, mirror fallback, endpoint check deduplication, 11 обязательных провайдеров, цветовые стили кнопок, runtime HTTP-подписки и очистку устаревших chunks.

## 📂 Структура

```text
vless-parser-bot/
├── src/
│   ├── bot.py          # Telegram-бот и admin cleanup
│   ├── config.py       # источники, зеркала и агрегаты
│   ├── parser.py       # fetch/extract/validate/dedup/TCP
│   ├── subscription.py # atomic plain/base64/chunk generation
│   ├── health.py       # Railway health + runtime subscriptions
│   └── server.py       # standalone HTTP endpoint
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
