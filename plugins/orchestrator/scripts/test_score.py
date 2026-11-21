"""Tests for score.py. Run with `python3 -m unittest` in this folder.

The fixtures are small synthetic transcripts built here. Their prompt text is a
marker string that must never reach the ledger or the report.
"""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import score  # noqa: E402
import transcripts  # noqa: E402

SECRET = "SECRETPROMPTTEXT"
SID = "11111111-aaaa-bbbb-cccc-222222222222"
USAGE = {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 5}


def dumps(rec):
    return json.dumps(rec, separators=(",", ":"))


class Agent:
    """Builds the lines of one subagent transcript."""

    def __init__(self, agent_id, agent_type="orchestrator:worker", model="claude-sonnet-5-5", ctx=1000):
        self.id, self.type, self.model, self.ctx = agent_id, agent_type, model, ctx
        self.lines = []
        self.n = 0

    def _base(self, **kw):
        self.n += 1
        return dict(isSidechain=True, agentId=self.id, sessionId=SID, gitBranch="feature-x",
                    timestamp="2026-11-20T10:%02d:00.000Z" % self.n, **kw)

    def tool(self, name, inp, result=None, is_error=False, usage=None):
        tid = "toolu_%s_%d" % (self.id, self.n)
        u = dict(USAGE, input_tokens=self.ctx if usage is None else usage)
        self.lines.append(dumps(self._base(type="assistant", message=dict(
            id="msg_%s_%d" % (self.id, self.n), model=self.model, usage=u,
            content=[dict(type="tool_use", id=tid, name=name, input=inp)]))))
        if result is not None:
            self.lines.append(dumps(self._base(type="user", message=dict(
                role="user", content=[dict(type="tool_result", tool_use_id=tid, content=result, is_error=is_error)]))))

    def attachment(self, att):
        self.lines.append(dumps(self._base(type="attachment", attachment=att)))

    def write(self, proj):
        d = os.path.join(proj, SID, "subagents")
        os.makedirs(d, exist_ok=True)
        base = os.path.join(d, "agent-%s" % self.id)
        with open(base + ".jsonl", "w") as fh:
            fh.write("\n".join(self.lines) + "\n")
        with open(base + ".meta.json", "w") as fh:
            json.dump({"agentType": self.type}, fh)


def parent_launches(proj, launches):
    """Write the main transcript. launches: list of (agent id, model given or None)."""
    lines = []
    for i, (aid, model) in enumerate(launches):
        tid = "toolu_launch_%d" % i
        inp = {"description": "d", "prompt": SECRET}
        if model:
            inp["model"] = model
        lines.append(dumps(dict(type="assistant", message=dict(
            id="m%d" % i, model="claude-opus-5-5", usage=USAGE,
            content=[dict(type="tool_use", id=tid, name="Agent", input=inp)]))))
        lines.append(dumps(dict(type="user", message=dict(
            role="user", content=[dict(type="tool_result", tool_use_id=tid, content="started")]),
            toolUseResult=dict(agentId=aid, status="async_launched"))))
    with open(os.path.join(proj, SID + ".jsonl"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def good_worker(aid):
    a = Agent(aid)
    a.tool("Edit", {"file_path": "/x", "new_string": SECRET})
    a.tool("Bash", {"command": "git commit -m '%s'" % SECRET}, "[feature-x abc1234] msg")
    a.tool("Bash", {"command": "gh pr create --title t"}, "https://github.com/owner/repo/pull/42\n")
    return a


class ScoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.projects = os.path.join(self.root, "projects")
        self.proj = os.path.join(self.projects, "-fixture-project")
        os.makedirs(self.proj)
        self.ledger = os.path.join(self.root, "ledger.db")
        old = transcripts.PROJECTS
        transcripts.PROJECTS = self.projects
        self.addCleanup(setattr, transcripts, "PROJECTS", old)

    def scan(self, agents, launches=None):
        for a in agents:
            a.write(self.proj)
        parent_launches(self.proj, launches if launches is not None else [(a.id, "sonnet") for a in agents])
        with io.StringIO() as buf:
            real, sys.stdout = sys.stdout, buf
            try:
                score.scan(self.proj, None, self.ledger)
            finally:
                sys.stdout = real
        db = score.Ledger(self.ledger)
        rows = {r["agent_id"]: r for r in db.runs_since("")}
        db.close()
        return rows

    def failed(self, row):
        return sorted(k for k, v in score.failures(row).items() if v)

    def test_clean_worker_has_no_failure(self):
        rows = self.scan([good_worker("a1")])
        r = rows["a1"]
        self.assertEqual(self.failed(r), [])
        self.assertEqual((r["ended_with_commit"], r["ended_with_pr"], r["pr_repo"], r["pr_number"]),
                         (1, 1, "owner/repo", 42))
        self.assertEqual((r["model_set"], r["branch"], r["code_worker"]), (1, "feature-x", 1))

    def test_no_model(self):
        rows = self.scan([good_worker("a1"), good_worker("a2")], [("a1", None), ("a2", "sonnet")])
        self.assertEqual(self.failed(rows["a1"]), ["no_model"])
        self.assertEqual(self.failed(rows["a2"]), [])

    def test_model_unknown_is_null_not_failure(self):
        rows = self.scan([good_worker("a1")], [])
        self.assertIsNone(rows["a1"]["model_set"])
        self.assertEqual(self.failed(rows["a1"]), [])

    def test_over_calls(self):
        a = good_worker("a1")
        for _ in range(score.MAX_TOOL_CALLS):
            a.tool("Read", {"file_path": "/x"}, "ok")
        rows = self.scan([a])
        self.assertEqual(self.failed(rows["a1"]), ["over_calls"])

    def test_big_context(self):
        a = good_worker("a1")
        a.tool("Read", {"file_path": "/x"}, "ok", usage=score.MAX_CONTEXT + 1)
        self.assertEqual(self.failed(self.scan([a])["a1"]), ["big_context"])

    def test_gate_blocks_and_give_up(self):
        a = good_worker("a1")
        for _ in range(3):
            a.attachment({"type": "hook_blocking_error", "hookEvent": "SubagentStop",
                          "blockingError": {"blockingError": "The finish gate found failing checks", "command": "c"}})
        a.attachment({"type": "hook_system_message", "hookEvent": "SubagentStop", "content":
                      "Finish gate: the work in /w still does not pass after 3 blocked stops, so it was let stop."})
        r = self.scan([a])["a1"]
        self.assertEqual((r["gate_blocks"], r["gate_giveups"]), (3, 1))
        self.assertEqual(self.failed(r), ["gate_gave_up"])

    def test_denial_is_counted_but_not_a_failure(self):
        a = good_worker("a1")
        a.tool("Bash", {"command": "git push"}, "PreToolUse:Bash hook error: Workers may not run or change x", True)
        r = self.scan([a])["a1"]
        self.assertEqual(r["denials"], 1)
        self.assertEqual(self.failed(r), [])

    def test_code_worker_without_commit_or_pr(self):
        a = Agent("a1")
        a.tool("Edit", {"file_path": "/x"})
        self.assertEqual(self.failed(self.scan([a])["a1"]), ["no_commit", "no_pr"])

    def test_general_purpose_counts_as_code_worker_only_with_edits(self):
        edit = Agent("a1", "general-purpose")
        edit.tool("Write", {"file_path": "/x"})
        read = Agent("a2", "general-purpose")
        read.tool("Read", {"file_path": "/x"})
        rows = self.scan([edit, read])
        self.assertEqual(self.failed(rows["a1"]), ["no_commit", "no_pr"])
        self.assertEqual(self.failed(rows["a2"]), [])

    def test_read_only_run_is_not_failed_for_commit_or_pr(self):
        a = Agent("a1", "Explore")
        a.tool("Grep", {"pattern": "x"}, "ok")
        self.assertEqual(self.failed(self.scan([a])["a1"]), [])

    def test_failed_or_empty_commit_does_not_count(self):
        a = Agent("a1")
        a.tool("Edit", {"file_path": "/x"})
        a.tool("Bash", {"command": "git commit -m x"}, "nothing to commit, working tree clean")
        a.tool("Bash", {"command": "git commit -m x"}, "hook failed", True)
        self.assertEqual(self.scan([a])["a1"]["ended_with_commit"], 0)

    def test_second_scan_adds_no_duplicate(self):
        agents = [good_worker("a1"), good_worker("a2")]
        self.scan(agents)
        rows = self.scan(agents)
        db = score.Ledger(self.ledger)
        count = db.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        db.close()
        self.assertEqual((count, len(rows)), (2, 2))

    def test_second_scan_updates_a_run_that_was_still_going(self):
        a = good_worker("a1")
        first = self.scan([a])["a1"]
        a.tool("Read", {"file_path": "/x"}, "ok")
        second = self.scan([a])["a1"]
        self.assertGreater(second["tool_calls"], first["tool_calls"])
        self.assertGreater(second["cost"], first["cost"])
        self.assertGreater(second["ended_at"], first["ended_at"])

    def test_no_prompt_text_reaches_ledger_or_report(self):
        a = good_worker("a1")
        a.tool("Bash", {"command": "echo " + SECRET}, SECRET)
        self.scan([a])
        with open(self.ledger, "rb") as fh:
            self.assertNotIn(SECRET.encode(), fh.read())
        out = []
        score.report(4, self.ledger, out=out.append)
        text = "\n".join(out)
        self.assertNotIn(SECRET, text)
        self.assertIn("owner/repo#42", text)


if __name__ == "__main__":
    unittest.main()
