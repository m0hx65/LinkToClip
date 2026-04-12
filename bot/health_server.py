from __future__ import annotations

import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_if_configured() -> web.AppRunner | None:
    """
    Render Web Services require a process listening on $PORT.
    When PORT is unset (local dev), skip and use Telegram polling only.
    """
    raw = os.environ.get("PORT")
    if not raw:
        return None
    port = int(raw)
    app = web.Application()
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server listening on 0.0.0.0:%s", port)
    return runner
