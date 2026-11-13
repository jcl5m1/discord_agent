"""Stopping a running turn: process-tree kill and per-channel queue dropping.

    .venv/bin/python -m unittest tests.test_interrupt
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness.pending import PendingTurns
from harness.providers.base import SPAWN_KWARGS, kill_process_tree


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); check it's really running
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


# A fake CLI with: a plain child; a tool command in its own process group (how
# Claude Code runs Bash); and a server detached the way the brief says (setsid).
CLI = """
import os, subprocess, sys, time
sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
child = subprocess.Popen(sleeper)
tool = subprocess.Popen(sleeper, preexec_fn=os.setpgrp)
launcher = subprocess.Popen(["sh", "-c", "setsid " + " ".join(sleeper[:1]) + " -c 'import time; time.sleep(60)' & echo $!"],
                            stdout=subprocess.PIPE, text=True)
server = int(launcher.stdout.readline())
launcher.wait()
print(child.pid, tool.pid, server, flush=True)
time.sleep(60)
"""


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class KillTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_kills_cli_and_its_children_but_not_detached_servers(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(CLI)
        self.addCleanup(os.unlink, f.name)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, f.name, stdout=asyncio.subprocess.PIPE, **SPAWN_KWARGS
        )
        child, tool, server = map(int, (await proc.stdout.readline()).split())
        self.addCleanup(lambda: alive(server) and os.kill(server, 9))
        await kill_process_tree(proc, grace=1)
        await asyncio.sleep(0.2)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(alive(child), "the CLI's child should be killed with it")
        self.assertFalse(alive(tool), "a tool command in its own process group should be killed too")
        self.assertTrue(alive(server), "a server started with setsid nohup ... & should survive")

    async def test_noop_after_exit(self):
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", **SPAWN_KWARGS)
        await proc.wait()
        await kill_process_tree(proc)  # must not raise


class DropChannelTests(unittest.TestCase):
    def test_drops_only_waiting_turns_in_that_channel(self):
        p = PendingTurns()
        running = p.add(1, "a", channel_id=10)
        running.started = True
        waiting = [p.add(2, "b", channel_id=10), p.add(3, "c", channel_id=10)]
        other = p.add(4, "d", channel_id=20)
        self.assertEqual(p.drop_channel(10), 2)
        self.assertTrue(all(t.deleted for t in waiting))
        self.assertFalse(running.deleted or other.deleted)
        self.assertEqual(p.drop_channel(10), 0)

    def test_followup_edit_keeps_channel(self):
        p = PendingTurns()
        t = p.add(1, "old", channel_id=10)
        t.started = True
        _, follow = p.edit(1, "new", channel_id=10)
        self.assertEqual(p.drop_channel(10), 1)
        self.assertTrue(follow.deleted)


if __name__ == "__main__":
    unittest.main()
