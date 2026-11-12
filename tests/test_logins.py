"""/login: driving CLI sign-ins and reading their status, with fake CLIs.

    .venv/bin/python -m unittest tests.test_logins
"""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from harness import config, logins
from harness.logins import LoginError, LoginSession

PASTE_CLI = """
import sys
print("Opening browser to sign in…")
print("If the browser didn't open, visit: https://example.com/oauth/authorize?state=abc")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
code = sys.stdin.readline().strip()
if code == "good":
    print("Login successful."); sys.exit(0)
print("Login failed: That code wasn't accepted."); sys.exit(1)
"""

DEVICE_CLI = """
import sys, time
print("Open this page to sign in:")
print("  https://example.com/device?code=xyz")
print("confirm this code matches:")
print("  BORP-4PDR", flush=True)
print("Waiting for approval…", flush=True)
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 0.3)
"""

BROKEN_CLI = "import sys; print('error: network unreachable'); sys.exit(2)"


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def script(self, name, body):
        path = Path(self.tmp.name) / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)


class SessionTests(Base):
    async def test_paste_code_accepted(self):
        s = LoginSession("claude", [sys.executable, self.script("c.py", PASTE_CLI)])
        await s.start()
        self.assertEqual(s.url, "https://example.com/oauth/authorize?state=abc")
        self.assertTrue(s.needs_code)
        await s.submit_code("  good  ")
        self.assertTrue(await s.wait(5))

    async def test_paste_code_rejected_reports_why(self):
        s = LoginSession("claude", [sys.executable, self.script("c.py", PASTE_CLI)])
        await s.start()
        await s.submit_code("bad")
        self.assertFalse(await s.wait(5))
        self.assertEqual(s.result_text(), "Login failed: That code wasn't accepted.")

    async def test_device_code(self):
        s = LoginSession("muse", [sys.executable, self.script("d.py", DEVICE_CLI)])
        await s.start()
        self.assertEqual((s.url, s.confirm_code, s.needs_code),
                         ("https://example.com/device?code=xyz", "BORP-4PDR", False))
        self.assertTrue(await s.wait(5))

    async def test_no_link_is_an_error(self):
        s = LoginSession("kimi", [sys.executable, self.script("b.py", BROKEN_CLI)])
        with self.assertRaises(LoginError) as e:
            await s.start(timeout=5)
        self.assertIn("network unreachable", str(e.exception))

    async def test_timeout_kills_the_login(self):
        s = LoginSession("muse", [sys.executable, self.script("d.py", DEVICE_CLI), "30"])
        await s.start()
        self.assertFalse(await s.wait(0.5))
        self.assertTrue(s.done)

    async def test_code_after_exit_refused(self):
        s = LoginSession("muse", [sys.executable, self.script("d.py", DEVICE_CLI), "0"])
        await s.start()
        await s.wait(5)
        with self.assertRaises(LoginError):
            await s.submit_code("x")


class StatusTests(Base):
    def fake(self, attr, output, code=0):
        path = self.script(attr, f"import sys; print({output!r}); sys.exit({code})")
        return mock.patch.object(config, attr, path)

    async def test_claude(self):
        sub = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"})
        with self.fake("CLAUDE_BIN", sub):
            st = await logins.status("claude")
        self.assertEqual((st.logged_in, st.subscription, st.detail), (True, True, "subscription (max)"))
        with self.fake("CLAUDE_BIN", json.dumps({"loggedIn": True, "authMethod": "apiKey"})):
            st = await logins.status("claude")
        self.assertEqual((st.logged_in, st.subscription), (True, False))
        self.assertIn("API billing", st.detail)
        with self.fake("CLAUDE_BIN", json.dumps({"loggedIn": False}), code=1):
            self.assertFalse((await logins.status("claude")).logged_in)

    async def test_kimi(self):
        with self.fake("KIMI_BIN", "managed:kimi-code  type=kimi  models=4  source=oauth"):
            self.assertTrue((await logins.status("kimi")).subscription)
        with self.fake("KIMI_BIN", "moonshot  type=openai  models=2  source=api_key"):
            st = await logins.status("kimi")
        self.assertEqual((st.logged_in, st.subscription), (True, False))

    async def test_muse(self):
        auth = Path(self.tmp.name) / "auth.json"
        with self.fake("MUSE_BIN", ""), mock.patch.dict(os.environ, {"MUSE_AUTH_PATH": str(auth)}):
            self.assertFalse((await logins.status("muse")).logged_in)
            auth.write_text(json.dumps({"providers": {"meta": {"mechanism": "oauth"}}}))
            self.assertTrue((await logins.status("muse")).subscription)

    async def test_not_installed(self):
        with mock.patch.object(config, "KIMI_BIN", "/nonexistent/kimi"):
            st = await logins.status("kimi")
        self.assertEqual((st.installed, st.detail), (False, "not installed"))


if __name__ == "__main__":
    unittest.main()
