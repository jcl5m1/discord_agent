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

It also hosts the external-trigger endpoint (see harness/triggers.py), under
the same tunnel path:

    POST /agents/_triggers/<name>   (Authorization: Bearer <trigger token>)

and each instance's shared files folder (see harness/files.py), read-only:

    GET /agents/<instance-slug>/<token>/files/<path>
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable

from aiohttp import ClientSession, ClientTimeout, web

from . import config, files, triggers
from .instances import InstanceManager

# bot callback: (trigger, event) -> result dict for the HTTP response
OnTrigger = Callable[[triggers.Trigger, dict], Awaitable[dict]]
_MAX_EVENT_BYTES = 64 * 1024

log = logging.getLogger("discord_agent.proxy")

_PATH_RE = re.compile(r"/agents/(?P<slug>[\w-]+)/(?P<token>\w+)/(?P<port>\d+)(?P<rest>/.*)?$")

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

# --- reviewjs annotate integration -------------------------------------------------
# Every HTML page served through the proxy gets the reviewjs annotate toolbar
# injected, plus a shim that seeds comments from the server (so they follow the
# user across devices) and autosaves the localStorage blob after any change
# (replacing the library's download/copy/import round-trip). The blob is stored
# opaquely — it is exactly the library's own localStorage payload.
_BLOB_PATH = "/__annotate_blob"
_MAX_BLOB = 1 * 1024 * 1024

_ANNOTATE_SHIM = """<script src="https://cdn.jsdelivr.net/npm/@reviewjs/annotate@1.5.3/annotate.js" data-project="{slug}" defer></script>
<script>(function(){{
var K="annotate:{slug}",E="/agents/{slug}/{token}/__annotate_blob",L=null;
fetch(E).then(function(r){{return r.ok?r.text():null}}).then(function(t){{
if(t&&!localStorage.getItem(K)){{localStorage.setItem(K,t);location.reload();}}
}}).catch(function(){{}});
setInterval(function(){{
var b=localStorage.getItem(K);
if(b&&b!==L){{L=b;fetch(E,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:b}}).catch(function(){{}});}}
}},3000);
}})();</script>"""


def _annotate_file(slug: str):
    return config.DATA_DIR / "annotations" / f"{slug}.json"


def _validate(request: web.Request, store: InstanceManager):
    m = re.match(r"/agents/(?P<slug>[\w-]+)/(?P<token>\w+)" + re.escape(_BLOB_PATH) + r"$", request.path)
    if not m:
        return None
    slug, token = m.group("slug"), m.group("token")
    inst = next((i for i in store.all() if _slug(i.name) == slug), None)
    if inst is None or inst.web_token is None or token != inst.web_token:
        return None
    return slug


async def _annotate_blob(request: web.Request) -> web.Response:
    slug = _validate(request, request.app["store"])
    if slug is None:
        return web.Response(status=403, text="bad token")
    path = _annotate_file(slug)
    if request.method == "GET":
        if not path.exists():
            return web.Response(status=404, text="no annotations yet")
        return web.Response(body=path.read_bytes(), content_type="application/json")
    if request.method == "POST":
        body = await request.read()
        if len(body) > _MAX_BLOB:
            return web.Response(status=413, text="annotation payload too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        log.info("annotations saved for %s (%d bytes)", slug, len(body))
        return web.Response(status=200, text="ok")
    return web.Response(status=405, text="method not allowed")


def _inject_shim(html: bytes, slug: str, token: str) -> bytes:
    shim = _ANNOTATE_SHIM.format(slug=slug, token=token).encode()
    lower = html.lower()
    idx = lower.rfind(b"</body>")
    if idx == -1:
        return html + shim
    return html[:idx] + shim + html[idx:]

# ------------------------------------------------------------------------------------

_runner: web.AppRunner | None = None


async def _trigger(request: web.Request) -> web.Response:
    """Fire a trigger: authenticate, rate-limit/dedupe, hand the event to the bot."""
    on_trigger: OnTrigger | None = request.app.get("on_trigger")
    if request.method != "POST":
        return web.json_response({"error": "POST only"}, status=405)
    tstore = triggers.TriggerStore()  # re-read: CLI edits apply without a restart
    trig = tstore.get(request.match_info["name"])
    # Same answer for unknown trigger and bad token (no name probing).
    if trig is None or not triggers.check_token(trig, request.headers.get("Authorization")):
        return web.json_response({"error": "unauthorized"}, status=401)
    if on_trigger is None:
        return web.json_response({"error": "triggers not available"}, status=503)
    if request.content_length and request.content_length > _MAX_EVENT_BYTES:
        return web.json_response({"error": "event too large"}, status=413)
    raw = await request.read()
    if len(raw) > _MAX_EVENT_BYTES:
        return web.json_response({"error": "event too large"}, status=413)
    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(event, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    reason = triggers.admit(trig, event)
    tstore.put(trig)  # persist rate-limit / dedupe bookkeeping
    if reason:
        log.info("trigger %s dropped: %s", trig.name, reason)
        return web.json_response({"status": "dropped", "reason": reason}, status=200)
    try:
        result = await on_trigger(trig, event)
    except Exception:
        log.exception("trigger %s failed", trig.name)
        return web.json_response({"error": "harness error"}, status=500)
    log.info("trigger %s fired: %s", trig.name, result)
    return web.json_response(result, status=202 if result.get("status") == "started" else 200)


async def _files(request: web.Request) -> web.StreamResponse:
    if request.method not in ("GET", "HEAD"):
        return web.Response(status=405, text="read-only")
    slug, token = request.match_info["slug"], request.match_info["token"]
    inst = next((i for i in request.app["store"].all() if _slug(i.name) == slug), None)
    if inst is None or inst.web_token is None or token != inst.web_token:
        return web.Response(status=403, text="bad token")
    rel = request.match_info["path"]
    target = files.resolve(slug, rel)
    if target is None or any(part.startswith(".") for part in rel.split("/") if part):
        return web.Response(status=404, text="not found")
    if target.is_file():
        return web.FileResponse(target, headers={"Cache-Control": "no-cache"})
    if target.is_dir() or rel == "":
        if rel and not rel.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        base = f"/agents/{slug}/{token}/files/"
        return web.Response(text=files.index_html(slug, rel.strip("/"), base), content_type="text/html")
    return web.Response(status=404, text="not found")


async def _files_root_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound(request.path + "/")


def build_app(store: InstanceManager, on_trigger: OnTrigger | None = None) -> web.Application:
    app = web.Application()
    app["store"] = store
    app["on_trigger"] = on_trigger

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
                ctype = upstream.headers.get("Content-Type", "")
                if (
                    request.method == "GET"
                    and upstream.status == 200
                    and "text/html" in ctype.lower()
                ):
                    # Buffer HTML pages so the annotate shim can be injected.
                    raw = await upstream.read()
                    raw = _inject_shim(raw, slug, token)
                    return web.Response(body=raw, content_type=ctype)

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

    app.router.add_route("*", "/agents/_triggers/{name}", _trigger)
    app.router.add_route("*", "/agents/{slug}/{token}/__annotate_blob", _annotate_blob)
    app.router.add_route("*", "/agents/{slug}/{token}/files/{path:.*}", _files)
    app.router.add_route("*", "/agents/{slug}/{token}/files", _files_root_redirect)
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


async def start_proxy(store: InstanceManager, on_trigger: OnTrigger | None = None) -> None:
    global _runner
    app = build_app(store, on_trigger)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, "127.0.0.1", config.PROXY_PORT)
    await site.start()
    log.info("agent proxy listening on 127.0.0.1:%s", config.PROXY_PORT)
