"""
Мини HTTP-сервер для отдачи подписок по ссылке.
Поддерживает как стандартные категории, так и кастомные файлы типа CUSTOM_100_*.txt
GET /sub/BLACK_FULL       -> plain агрегированная подписка
GET /sub/BLACK_FULL/b64   -> base64
GET /sub/CUSTOM_100_123.txt -> кастомная подписка (прямо по имени файла)
GET /                    -> список всех подписок
"""
import os
from pathlib import Path
from aiohttp import web
import config

DATA_DIR = Path(__file__).parent.parent / "data"

async def handle_list(request):
    html = ["<h1>Free VPN • Crimson — Подписки</h1><ul>"]
    # Стандартные
    for key, cfg in config.SOURCES.items():
        html.append(f"<li><b>{cfg['name']}</b> — {cfg['description']}<br>")
        html.append(f"Plain: <a href='/sub/{key}'>/sub/{key}</a> | ")
        html.append(f"Base64: <a href='/sub/{key}/b64'>/sub/{key}/b64</a></li>")
    # Агрегированные
    html.append("<hr><h3>Агрегированные</h3><ul>")
    for agg_key, agg in config.AGGREGATED_SUBS.items():
        fname = agg["filename"]
        html.append(f"<li><b>{agg['profile_title']}</b> — {fname} <a href='/sub/{fname}'>/sub/{fname}</a></li>")
    html.append("</ul>")
    # Кастомные файлы из data/
    html.append("<hr><h3>Кастомные (CUSTOM_100)</h3><ul>")
    try:
        for p in sorted(DATA_DIR.glob("CUSTOM_100*.txt")):
            if "_base64" in p.name:
                continue
            html.append(f"<li><a href='/sub/{p.name}'>{p.name}</a> — <a href='/sub/{p.name}/b64'>base64</a></li>")
    except:
        pass
    html.append(f"</ul><p>Добавь ссылку в клиент как подписку. Обновляется каждые {config.UPDATE_INTERVAL} мин.</p>")
    return web.Response(text="".join(html), content_type="text/html")

async def handle_sub(request):
    key = request.match_info.get("key")
    b64 = request.match_info.get("b64", "")
    # Определяем имя файла
    filename = None
    # 1) Если key — это точное имя файла типа CUSTOM_100_123.txt или FULL.txt
    if key.endswith(".txt"):
        filename = key
        # если запрошен b64 через /sub/file.txt/b64 — отдаем base64 версию
        if b64:
            if filename.endswith("_base64.txt"):
                filename = filename  # уже base64
            else:
                filename = filename.replace(".txt", "_base64.txt")
    else:
        # 2) Если key — это ключ из SOURCES (collection, zieng2 и т.д.)
        if key in config.SOURCES:
            filename = f"{key}_base64.txt" if b64 else f"{key}.txt"
        else:
            # 3) Проверяем AGGREGATED_SUBS по ключу
            if key in config.AGGREGATED_SUBS:
                base = config.AGGREGATED_SUBS[key]["filename"]
                if b64:
                    filename = base.replace(".txt", "_base64.txt")
                else:
                    filename = base
            else:
                # 4) Пытаемся найти файл напрямую: key.txt или key
                possible = [f"{key}.txt", f"{key}_base64.txt", key]
                for cand in possible:
                    if (DATA_DIR / cand).exists():
                        filename = cand if not b64 else cand.replace(".txt", "_base64.txt") if not cand.endswith("_base64.txt") else cand
                        break
                if not filename:
                    # Если b64 суффикс передан как второй параметр
                    if b64 == "b64":
                        # key может быть CUSTOM_100_123.txt, а b64 = b64
                        if key.endswith(".txt"):
                            filename = key.replace(".txt", "_base64.txt")
                        else:
                            filename = f"{key}_base64.txt"
                    else:
                        filename = f"{key}.txt"

    if not filename:
        return web.Response(text="unknown key", status=404)

    # Защита от path traversal
    filename = os.path.basename(filename)
    path = DATA_DIR / filename
    # Если файл base64 не найден, пробуем plain и на лету кодируем? Лучше 404
    if not path.exists():
        # Пробуем найти без учета регистра? Нет
        # Если запрошен plain, но есть только base64 — отдадим base64 decoded? Лучше 404
        return web.Response(text=f"not generated yet: {filename}, try later", status=404)

    content = path.read_text(encoding="utf-8")
    ctype = "text/plain; charset=utf-8"
    return web.Response(text=content, content_type=ctype, headers={
        "Content-Disposition": f"inline; filename={filename}",
        "Cache-Control": "no-cache",
    })

def create_app():
    app = web.Application()
    app.router.add_get("/", handle_list)
    app.router.add_get("/sub/{key}", handle_sub)
    app.router.add_get("/sub/{key}/{b64}", handle_sub)
    return app

if __name__ == "__main__":
    app = create_app()
    port = config.PORT
    print(f"HTTP подписок на http://0.0.0.0:{port}/sub/BLACK_FULL")
    web.run_app(app, host="0.0.0.0", port=port)
