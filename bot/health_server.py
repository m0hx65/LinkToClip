from __future__ import annotations

"""
Public HTTP for Render Web Services: one listener on 0.0.0.0:$PORT (see Render docs).
Telegram traffic still uses long polling; this only satisfies the platform health check.
"""

import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)

# Render default when PORT is not overridden in the dashboard
_RENDER_DEFAULT_PORT = 10000


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_if_configured() -> web.AppRunner | None:
    """
    Bind when PORT is set (any host) or RENDER is set (deploy on Render).
    Local dev: omit both to run Telegram polling only with no HTTP server.
    """
    if not (os.environ.get("PORT") or os.environ.get("RENDER")):
        return None
    port = int(os.environ.get("PORT", str(_RENDER_DEFAULT_PORT)))
    app = web.Application()
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server listening on 0.0.0.0:%s (PORT)", port)
    return runner
