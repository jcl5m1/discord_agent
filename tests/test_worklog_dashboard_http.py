"""Worklog dashboard proxy: fixed upstream, token forwarding, and allowed routes."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from harness import bot, config, proxy
from harness.instances import InstanceManager


class WorklogProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.tmp.cleanup)
        self.requests = []
        self.replies = []

        async def upstream(request):
            token = request.headers.get("X-Worklog-Token", "")
            self.requests.append((request.path, token, request.query_string))
            if token != "good-token":
                return web.Response(status=403, text="bad token")
            if request.path == "/":
                return web.Response(text="<html>worklog</html>", content_type="text/html")
            if request.path in ("/reply", "/drop"):
                self.replies.append((request.headers.get("Content-Type"), await request.json()))
                return web.json_response({"ok": True, "text": "sent"})
            return web.json_response({"tasks": [], "echo": request.query.get("echo")})

        self.upstream = TestServer(web.Application())
        self.upstream.app.router.add_get("/", upstream)
        self.upstream.app.router.add_get("/state", upstream)
        self.upstream.app.router.add_post("/reply", upstream)
        self.upstream.app.router.add_post("/drop", upstream)
        await self.upstream.start_server()
        self.addAsyncCleanup(self.upstream.close)
        self.port_patch = mock.patch.object(config, "WORKLOG_PORT", self.upstream.port)
        self.port_patch.start()
        self.addCleanup(self.port_patch.stop)
        store = InstanceManager(Path(self.tmp.name) / "instances.json")
        self.client = TestClient(TestServer(proxy.build_app(store)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_root_and_state_forward_only_to_the_fixed_worklog_port(self):
        response = await self.client.get("/agents/_worklog/good-token/")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "<html>worklog</html>")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        response = await self.client.get("/agents/_worklog/good-token/state?echo=ok")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"tasks": [], "echo": "ok"})
        self.assertEqual(self.requests[:2], [
            ("/", "good-token", ""),
            ("/state", "good-token", "echo=ok"),
        ])

    async def test_reply_forwards_json_body_content_type_and_token(self):
        response = await self.client.post(
            "/agents/_worklog/good-token/reply", json={"task": 42, "text": "Please review"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True, "text": "sent"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.replies, [("application/json", {"task": 42, "text": "Please review"})])
        self.assertEqual(self.requests, [("/reply", "good-token", "")])

    async def test_drop_forwards_json_body_and_token(self):
        response = await self.client.post("/agents/_worklog/good-token/drop", json={"task": 7, "reason": "stale"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.replies, [("application/json", {"task": 7, "reason": "stale"})])
        self.assertEqual(self.requests, [("/drop", "good-token", "")])
        response = await self.client.get("/agents/_worklog/good-token/drop")
        self.assertEqual(response.status, 405)

    async def test_rejects_bad_tokens_methods_and_arbitrary_paths(self):
        response = await self.client.get("/agents/_worklog/wrong/state")
        self.assertEqual(response.status, 403)
        response = await self.client.post("/agents/_worklog/good-token/state")
        self.assertEqual(response.status, 405)
        response = await self.client.get("/agents/_worklog/good-token/other")
        self.assertEqual(response.status, 404)
        self.assertEqual([path for path, token, query in self.requests], ["/state"])

    async def test_slash_worklog_without_command_returns_dashboard_link(self):
        response = mock.Mock()
        response.send_message = mock.AsyncMock()
        response.defer = mock.AsyncMock()
        interaction = mock.Mock(response=response, user=mock.Mock(name="tester"))
        worklog = mock.Mock()
        worklog.ready.is_set.return_value = True
        worklog.dashboard_url.return_value = "https://agents.example.com/agents/_worklog/token/"
        worklog.run = mock.AsyncMock()
        with mock.patch.object(bot, "worklog", worklog):
            await bot.cmd_worklog.callback(interaction)

        response.send_message.assert_awaited_once_with(
            "[Open the worklog dashboard](https://agents.example.com/agents/_worklog/token/)"
        )
        worklog.run.assert_not_called()
        response.defer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
