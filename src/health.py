"""Railway health check and runtime subscription endpoints."""

import base64
import os
from pathlib import Path

from aiohttp import web

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"


async def health(request):
    return web.Response(
        text="OK - VLESS parser bot is running",
        content_type="text/plain",
    )


async def stats(request):
    files = list(DATA_DIR.glob("*.txt"))
    info = {path.name: path.stat().st_size for path in files if path.is_file()}
    return web.json_response({"status": "ok", "files": info})


def _subscription_path(filename: str):
    if not filename or filename != os.path.basename(filename) or not filename.endswith(".txt"):
        return None
    runtime_path = DATA_DIR / filename
    if runtime_path.is_file():
        return runtime_path
    repository_path = ROOT_DIR / filename
    if repository_path.is_file():
        return repository_path
    return None


async def subscription(request):
    filename = request.match_info.get("filename", "")
    encoding = request.match_info.get("encoding", "")
    if encoding not in {"", "b64"}:
        raise web.HTTPNotFound(text="unknown subscription encoding")

    path = _subscription_path(filename)
    if path is None:
        raise web.HTTPNotFound(text=f"subscription not generated: {filename}")

    content = path.read_bytes()
    if encoding == "b64":
        content = base64.b64encode(content)

    return web.Response(
        body=content,
        content_type="text/plain",
        charset="utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


def create_health_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/stats", stats)
    app.router.add_get("/sub/{filename}", subscription)
    app.router.add_get("/sub/{filename}/{encoding}", subscription)
    return app


async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = create_health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health/subscription server on 0.0.0.0:{port}")
    return runner
