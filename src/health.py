"""Minimal Railway health server; subscriptions are GitHub .txt files only."""

import os
from pathlib import Path

from aiohttp import web

DATA_DIR = Path(__file__).parent.parent / "data"


async def health(request):
    return web.Response(
        text="OK - VLESS parser bot is running",
        content_type="text/plain",
    )


async def stats(request):
    files = list(DATA_DIR.glob("*.txt"))
    info = {path.name: path.stat().st_size for path in files if path.is_file()}
    return web.json_response({"status": "ok", "files": info})


def create_health_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/stats", stats)
    return app


async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = create_health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server on 0.0.0.0:{port}")
    return runner
