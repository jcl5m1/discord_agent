"""Local reverse proxy that exposes agent-run web servers through the
Cloudflare Tunnel path.

One permanent ingress rule on the existing tunnel forwards
https://<tunnel-host>/agents/* to this proxy (127.0.0.1:PROXY_PORT); the proxy
dispatches each request to the agent server by path:

    /agents/<instance-slug>/<token>/<port>/<rest...>
        -> http://127.0.0.1:<port>/<rest...>

The per-instance token (stored in the instance record) keeps random people on
the internet from walking the URL space. HTTP only — websocket upgrades
(vite-style live reload) are not proxied in v1.
"""

from __future__ import annotations

import logging
import re

from aiohttp import ClientSession, ClientTimeout, web

from . import config
from .instances import InstanceManager

log = logging.getLogger("discord_agent.proxy")

_PATH_RE = re.compile(r"/agents/(?P<slug>[\w-]+)/(?P<token>\w+)/(?P<port>\d+)(?P<rest>/.*)?$")

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

_runner: web.AppRunner | None = None


def build_app(store: InstanceManager) -> web.Application:
    app = web.Application()
    app["store"] = store

    async def handle(request: web.Request) -> web.StreamResponse:
        m = _PATH_RE.match(request.path)
        if not m:
            return web.Response(status=404, text="not an agent path")
        slug, token, port, rest = (
            m.group("slug"), m.group("token"), int(m.group("port")), m.group("rest") or "/"
        )
        inst = next((i for i in store.all() if _slug(i.name) == slug), None)
        if inst is None or inst.web_token is None or token != inst.web_token:
            return web.Response(status=403, text="bad token")
        if not (1024 <= port <= 65535):
            return web.Response(status=400, text="bad port")

        url = f"http://127.0.0.1:{port}{rest}"
        if request.query_string:
            url += "?" + request.query_string
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
        }
        body = await request.read()  # buffered; agent demos are small
        session: ClientSession = request.app["session"]
        try:
            async with session.request(
                request.method, url, headers=headers, data=body or None
            ) as upstream:
                resp = web.StreamResponse(
                    status=upstream.status, reason=upstream.reason
                )
                for k, v in upstream.headers.items():
                    if k.lower() not in _HOP_BY_HOP:
                        resp.headers[k] = v
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except Exception:
            log.exception("proxy to %s failed", url)
            return web.Response(status=502, text="agent server not reachable")

    app.router.add_route("*", "/agents/{tail:.*}", handle)

    async def on_startup(app: web.Application) -> None:
        app["session"] = ClientSession(timeout=ClientTimeout(total=None))

    async def on_cleanup(app: web.Application) -> None:
        await app["session"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-_]", "-", name.lower()).strip("-")
    return s or "agent"


async def start_proxy(store: InstanceManager) -> None:
    global _runner
    app = build_app(store)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, "127.0.0.1", config.PROXY_PORT)
    await site.start()
    log.info("agent proxy listening on 127.0.0.1:%s", config.PROXY_PORT)
