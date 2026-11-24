"""The running cap's view of a waiting thread (bin/t3-mcp thread_from_list, read by `slot_counts`).

T3 0.1.11 lists a PR watch as a pending background task of its own: kind `monitor`, taskId
`pull-request-watch:<repo>#<n>`. A thread waiting on nothing else holds no slot; a thread that
also waits on a command, a subagent or another background task still does.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("t3_mcp", str(ROOT / "bin" / "t3-mcp"))
SPEC = importlib.util.spec_from_loader("t3_mcp", LOADER)
t3_mcp = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(t3_mcp)

PR_WATCH = {"taskId": "pull-request-watch:github.com/ai-kosmo/aikosmo-monorepo#9527", "kind": "monitor",
            "awaited": False}
TESTS = {"taskId": "command:bun-test-7f3a", "kind": "command", "awaited": True}


def listed(*tasks, watching=False, blocked=False, status="waiting", title="[kosmi] work"):
    """A t3_thread_list row the way T3 0.1.11 returns it."""
    return {"threadId": "t", "title": title, "status": status, "latestRunId": "r",
            "pullRequests": [{"number": 9527, "state": "open", "watching": watching}],
            "pendingBackgroundTasks": list(tasks), "blockedOnThreadWait": blocked}


def slots(rows, limit=3):
    """`slot_counts`' jq over normalised rows, run as t3ctl runs it."""
    script = (ROOT / "bin" / "t3ctl").read_text()
    start = script.index("slot_counts() {")
    jq = script[script.index("jq -c", start):script.index("<<<\"$rows\"", start)]
    program = jq.split("'", 1)[1].rsplit("'", 1)[0]
    out = subprocess.run(["jq", "-c", "--arg", "tag", "[kosmi]", "--argjson", "limit", str(limit), program],
                         input=json.dumps([t3_mcp.thread_from_list(r) for r in rows]), capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)


class SlotTest(unittest.TestCase):
    def test_a_pr_watch_task_is_the_watch_not_other_background_work(self):
        row = t3_mcp.thread_from_list(listed(PR_WATCH))
        self.assertTrue(row["watchingPullRequest"])
        self.assertEqual(row["pendingBackgroundTasks"], 0)

    def test_a_pr_watch_plus_a_test_run_still_waits_on_work(self):
        row = t3_mcp.thread_from_list(listed(PR_WATCH, TESTS))
        self.assertTrue(row["watchingPullRequest"])
        self.assertEqual(row["pendingBackgroundTasks"], 1)

    def test_the_cap_skips_a_thread_parked_only_on_a_pr_watch(self):
        counted = slots([listed(PR_WATCH), listed(PR_WATCH, TESTS), listed(PR_WATCH, blocked=True),
                         listed(status="running"), listed(PR_WATCH, title="Giulio's own thread")])
        self.assertEqual(counted, {"running": 3, "limit": 3, "free": 0, "parkedOnPullRequestWatch": 1})

    def test_the_vm_case_of_2026_11_24(self):
        """Three [kosmi] threads parked on PR watches (T3 lists each watch as a pending task): 0 of 3 used."""
        counted = slots([listed(PR_WATCH, watching=True) for _ in range(3)])
        self.assertEqual(counted, {"running": 0, "limit": 3, "free": 3, "parkedOnPullRequestWatch": 3})

    def test_an_id_shaped_task_and_odd_entries(self):
        self.assertTrue(t3_mcp.is_pr_watch({"id": "pull-request-watch:x#1"}))
        self.assertTrue(t3_mcp.is_pr_watch("pull-request-watch:x#1"))
        self.assertFalse(t3_mcp.is_pr_watch({"taskId": "monitor:pull-request-watch:x"}))
        self.assertFalse(t3_mcp.is_pr_watch(None))


if __name__ == "__main__":
    unittest.main()
