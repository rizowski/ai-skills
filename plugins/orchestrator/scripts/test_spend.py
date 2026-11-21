"""Tests for spend.py. Run with `python3 -m unittest` in this folder.

The fixtures are small synthetic transcripts. Their prompt text is a marker
string that must never reach the output or the ledger.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spend  # noqa: E402
import transcripts  # noqa: E402

SECRET = "SECRETPROMPTTEXT"
SID = "33333333-aaaa-bbbb-cccc-444444444444"


def dumps(rec):
    return json.dumps(rec, separators=(",", ":"))


def turn(n, usage, text=SECRET, model="claude-sonnet-5-5"):
    return dumps(dict(type="assistant", message=dict(
        id="m%d" % n, model=model, usage=usage, content=[dict(type="text", text=text)])))


class SpendTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.proj = os.path.join(self.root, "projects", "-fixture-project")
        os.makedirs(self.proj)
        self.ledger = os.path.join(self.root, "ledger.db")
        old = transcripts.PROJECTS
        transcripts.PROJECTS = os.path.join(self.root, "projects")
        spend.PROJECTS = transcripts.PROJECTS
        self.addCleanup(setattr, transcripts, "PROJECTS", old)
        self.addCleanup(setattr, spend, "PROJECTS", old)
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""})
        env.start()
        self.addCleanup(env.stop)

    def write_main(self, lines):
        with open(os.path.join(self.proj, SID + ".jsonl"), "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def two_turns(self):
        # Sonnet weight 3. Turn 1: 1000 input-side tokens, 100 written (5m) and 50 output.
        # Turn 2: 900 read, 100 new input, 20 output, plus one advisor call.
        self.write_main([
            turn(1, dict(input_tokens=900, cache_read_input_tokens=0, cache_creation_input_tokens=100,
                         cache_creation=dict(ephemeral_5m_input_tokens=100, ephemeral_1h_input_tokens=0),
                         output_tokens=50)),
            turn(2, dict(input_tokens=100, cache_read_input_tokens=900, cache_creation_input_tokens=0,
                         output_tokens=20,
                         iterations=[
                             dict(type="message", input_tokens=100, cache_read_input_tokens=900,
                                  cache_creation_input_tokens=0, output_tokens=20),
                             dict(type="advisor_message", model="claude-opus-5-5", input_tokens=1000,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=10)]))])

    def run_current(self, sid=SID):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}), contextlib.redirect_stdout(out):
            spend.current(None if sid else self.proj, self.ledger)
        return out.getvalue()

    def summary(self):
        x = transcripts.read_session(transcripts.Spend(), self.proj, os.path.join(self.proj, SID + ".jsonl"))
        return spend.summarize(x, self.proj, "2026-11-16")

    def test_figures(self):
        self.two_turns()
        r = self.summary()
        self.assertEqual((r["start_ctx"], r["end_ctx"], r["turns"]), (1000, 1000, 2))
        self.assertEqual(r["tok_input"], 1000)
        self.assertEqual(r["tok_cache_read"], 900)
        self.assertEqual(r["tok_cache_write_5m"], 100)
        self.assertEqual(r["tok_output"], 70)
        # 900 read of 2000 prompt-side tokens
        self.assertAlmostEqual(r["hit_rate"], 900 / 2000)
        self.assertAlmostEqual(r["cost_cache_read"], 3 * 0.1 * 900)
        self.assertAlmostEqual(r["cost_cache_write_5m"], 3 * 1.25 * 100)
        self.assertAlmostEqual(r["cost_output"], 3 * 5.0 * 70)
        # the advisor call is opus (weight 5): 1000 input + 10 output
        self.assertEqual(r["advisor_calls"], 1)
        self.assertAlmostEqual(r["advisor_cost"], 5 * (1000 + 5.0 * 10))
        self.assertEqual(r["sub_cost"], 0)
        # kinds cover the main cost without the advisor
        self.assertAlmostEqual(sum(r["cost_" + k] for k in spend.KINDS), r["main_cost"] - r["advisor_cost"])

    def test_subagent_cost(self):
        self.two_turns()
        d = os.path.join(self.proj, SID, "subagents")
        os.makedirs(d)
        sub = dict(type="assistant", isSidechain=True, message=dict(
            id="s1", model="claude-haiku-5", content=[],
            usage=dict(input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=0)))
        with open(os.path.join(d, "agent-a1.jsonl"), "w") as fh:
            fh.write(dumps(sub) + "\n")
        r = self.summary()
        self.assertEqual(r["subagents"], 1)
        self.assertAlmostEqual(r["sub_cost"], 100.0)

    def test_current_prints_figures_and_no_handoff(self):
        self.two_turns()
        out = self.run_current()
        self.assertIn("starting context: 1000 tokens", out)
        self.assertIn("cache hit rate: 45.0%", out)
        self.assertIn("SPEND: start 1k", out)
        self.assertNotIn("HANDOFF", out.upper())
        self.assertNotIn(SECRET, out)

    def test_current_records_one_row_and_runs_again(self):
        self.two_turns()
        self.run_current()
        self.run_current()
        db = spend.open_ledger(self.ledger)
        rows = db.execute("SELECT * FROM spend_sessions").fetchall()
        db.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], SID)
        self.assertEqual(rows[0]["project"], "-fixture-project")
        self.assertNotIn(SECRET, json.dumps([tuple(r) for r in rows], default=str))

    def test_current_does_not_record_when_session_is_unknown(self):
        self.two_turns()
        out = self.run_current(sid="")
        self.assertIn("not recorded", out)
        self.assertFalse(os.path.exists(self.ledger))

    def trend_rows(self, rows):
        db = spend.open_ledger(self.ledger)
        for i, (date, start, hit_read, cost) in enumerate(rows):
            r = dict(session_id="s%d" % i, date=date, project="p", start_ctx=start, end_ctx=start, turns=10,
                     subagents=0, hit_rate=0.0, main_cost=cost, sub_cost=0.0, advisor_cost=0.0, advisor_calls=0)
            for k in spend.KINDS:
                r["tok_" + k] = 0
                r["cost_" + k] = 0.0
            r["tok_input"] = 100
            r["tok_cache_read"] = hit_read
            spend.record(db, r)
        db.close()

    def test_trend_groups_by_week(self):
        today = spend.datetime.now()
        monday = today - spend.timedelta(days=today.weekday())
        last = monday - spend.timedelta(days=7)
        fmt = "%Y-%m-%d"
        self.trend_rows([(last.strftime(fmt), 80000, 100, 1000.0), (monday.strftime(fmt), 60000, 300, 500.0),
                         (monday.strftime(fmt), 40000, 300, 500.0)])
        lines = []
        spend.trend(self.ledger, 4, out=lines.append)
        body = [l for l in lines if l.startswith("20")]
        self.assertEqual(len(body), 2)
        self.assertTrue(body[0].startswith(last.strftime(fmt)))
        self.assertIn(" 80000 ", body[0])
        self.assertIn("  50.0%", body[0])  # 100 read of 200 prompt tokens
        self.assertIn("     2 ", body[1])  # two sessions
        self.assertIn(" 50000 ", body[1])  # median of 60000 and 40000
        self.assertIn("  75.0%", body[1])
        self.assertIn("       50 ", body[1])  # 1000 cost over 20 turns

    def test_trend_without_rows(self):
        lines = []
        spend.trend(self.ledger, 4, out=lines.append)
        self.assertIn("No recorded sessions", lines[0])


if __name__ == "__main__":
    unittest.main()
