"""Tests for judge.py. Run with `python3 -m unittest` in this folder.

No test makes a model call: the call is replaced. The marker strings in the fixtures must
never reach the ledger or the report.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import judge  # noqa: E402
import score  # noqa: E402
import transcripts  # noqa: E402
from test_score import SID, USAGE, Agent, dumps, good_worker  # noqa: E402

BRIEF = "BRIEFMARKER"
REPORT = "REPORTMARKER"
REASON = "REASONMARKER"


def row(i, cost=10.0, code=1, **kw):
    r = dict(session_id="s%d" % i, agent_id="a%d" % i, agent_type="orchestrator:worker", model="m", model_set=1,
             tool_calls=5, gate_blocks=0, gate_giveups=0, denials=0, code_worker=code, ended_with_commit=1,
             ended_with_pr=1, pr_repo=None, pr_number=None, branch="b", peak_context=1000, cost=cost,
             machine="x", project="p", started_at="2026-11-20T10:00:00Z", ended_at="2026-11-20T11:00:00Z")
    r.update(kw)
    return r


def good_answer(answer="no"):
    return {k: {"answer": answer, "reason": REASON} for k in judge.QUESTIONS}


def ids(sample):
    return [(r["agent_id"], why) for r, why in sample]


class SampleTest(unittest.TestCase):
    def rows(self):
        costly = [row(i, cost=100 - i) for i in range(12)]  # 12 code runs, clean
        failed = [row(100 + i, cost=1, code=0, tool_calls=99) for i in range(12)]  # over_calls
        clean = [row(200 + i, cost=1, code=0) for i in range(20)]
        return costly + failed + clean

    def test_sizes(self):
        sample = judge.choose_sample(self.rows(), set(), "2026-W47")
        why = [w for _, w in sample]
        self.assertEqual(why.count("costly code run"), judge.COSTLIEST_CODE_RUNS)
        self.assertEqual(why.count("script failure"), judge.FAILED_RUNS)
        self.assertEqual(why.count("control"), judge.CONTROL_RUNS)
        self.assertEqual(len({r["agent_id"] for r, _ in sample}), 25)
        self.assertEqual([r["agent_id"] for r, w in sample if w == "costly code run"], ["a%d" % i for i in range(10)])

    def test_control_is_seeded_by_week(self):
        a = judge.choose_sample(self.rows(), set(), "2026-W47")
        b = judge.choose_sample(self.rows(), set(), "2026-W47")
        c = judge.choose_sample(self.rows(), set(), "2026-W48")
        self.assertEqual(ids(a), ids(b))
        self.assertNotEqual(ids(a), ids(c))

    def test_judged_run_is_skipped_and_others_stay(self):
        full = judge.choose_sample(self.rows(), set(), "2026-W47")
        done = {("s0", "a0"), (full[-1][0]["session_id"], full[-1][0]["agent_id"])}
        rest = judge.choose_sample(self.rows(), done, "2026-W47")
        self.assertEqual(ids(rest), ids(full)[1:-1])

    def test_small_ledger(self):
        self.assertEqual(judge.choose_sample([], set(), "x"), [])
        self.assertEqual(len(judge.choose_sample([row(1)], set(), "x")), 1)


class InputTest(unittest.TestCase):
    def test_caps_and_notice(self):
        text = judge.build_input(row(1), "b" * (judge.BRIEF_CAP + 50), "r" * (judge.REPORT_CAP + 50), {"/f": 2})
        self.assertIn("BRIEF (cut to the first %d characters)" % judge.BRIEF_CAP, text)
        self.assertIn("REPORT (cut to the first %d characters)" % judge.REPORT_CAP, text)
        self.assertIn("b" * judge.BRIEF_CAP, text)
        self.assertNotIn("b" * (judge.BRIEF_CAP + 1), text)
        self.assertLess(len(text), judge.BRIEF_CAP + judge.REPORT_CAP + 1000)
        self.assertIn("/f (2 calls)", text)

    def test_short_text_is_not_cut(self):
        text = judge.build_input(row(1, tool_calls=99), BRIEF, REPORT, {})
        self.assertNotIn("cut to", text)
        self.assertIn("over_calls", text)
        self.assertIn("  none", text)

    def test_file_list_is_capped(self):
        files = {"/f%03d" % i: 1 for i in range(judge.MAX_FILES + 10)}
        text = judge.build_input(row(1), BRIEF, REPORT, files)
        self.assertEqual(text.count(" (1 calls)"), judge.MAX_FILES)
        self.assertIn("first %d shown" % judge.MAX_FILES, text)


class RulesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.put(".claude/skills/alpha/SKILL.md", "---\nname: alpha\ndescription: Does the alpha thing\n---\n# body head\n")
        self.put(".claude/skills/beta/SKILL.md", "---\nname: beta\ndescription: \"Does beta\"\n---\n")
        self.put("CLAUDE.md", "# Root rules\ntext\n## Tools\n")
        self.put("core/CLAUDE.md", "# Core rules\n")
        self.put("node_modules/CLAUDE.md", "# Skipped\n")
        self.put(".git/CLAUDE.md", "# Skipped git\n")
        self.put("a/b/c/CLAUDE.md", "# Too deep\n")

    def put(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def test_summary(self):
        text = judge.rules_summary(self.repo)
        self.assertIn("skill alpha: Does the alpha thing", text)
        self.assertIn("skill beta: Does beta", text)
        self.assertIn("CLAUDE.md: # Root rules | ## Tools", text)
        self.assertIn(os.path.join("core", "CLAUDE.md") + ": # Core rules", text)
        for gone in ("Skipped", "Too deep", "body head"):
            self.assertNotIn(gone, text)
        self.assertNotIn("cut to", text)

    def test_cap(self):
        self.put("CLAUDE.md", "# " + "h" * (judge.RULES_CAP + 100) + "\n")
        text = judge.rules_summary(self.repo)
        self.assertIn("summary cut to the first %d characters" % judge.RULES_CAP, text)
        self.assertLess(len(text), judge.RULES_CAP + 100)

    def test_missing_folder(self):
        self.assertEqual(judge.rules_summary(os.path.join(self.repo, "nope")), "")
        self.assertEqual(judge.rules_summary(None), "")
        text = judge.build_input(row(1), BRIEF, REPORT, {})
        self.assertIn("repository rules: not available", text)

    def test_input_label_and_question(self):
        text = judge.build_input(row(1), BRIEF, REPORT, {}, judge.rules_summary(self.repo))
        self.assertIn("REPOSITORY RULES (summary):", text)
        self.assertIn("skill alpha", text)
        self.assertNotIn("not available", text)
        self.assertIn("unknown when it says the rules are not available", judge.SYSTEM_PROMPT)


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proj = os.path.join(tmp.name, "projects", "-p")
        os.makedirs(self.proj)

    def make(self):
        a = good_worker("a1")
        a.tool("Edit", {"file_path": "/x", "new_string": "SECRETCODE"})
        a.tool("Write", {"file_path": "/y", "content": "SECRETCODE"})
        a.lines.append(dumps(a._base(type="assistant", message=dict(
            id="mfinal", model=a.model, usage=USAGE, content=[dict(type="text", text=REPORT)]))))
        a.write(self.proj)
        lines = [dumps(dict(type="assistant", message=dict(id="m", model="o", usage=USAGE, content=[
            dict(type="tool_use", id="t1", name="Agent", input={"prompt": BRIEF})]))),
            dumps(dict(type="user", message=dict(role="user", content=[dict(type="tool_result", tool_use_id="t1", content="ok")]),
                       toolUseResult=dict(agentId="a1")))]
        with open(os.path.join(self.proj, SID + ".jsonl"), "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_input_holds_brief_report_and_file_counts_only(self):
        self.make()
        text = judge.run_input(self.proj, row(1, session_id=SID, agent_id="a1"))
        self.assertIn(BRIEF, text)
        self.assertIn(REPORT, text)
        self.assertIn("/x (2 calls)", text)
        self.assertIn("/y (1 calls)", text)
        self.assertNotIn("SECRETCODE", text)
        self.assertNotIn("git commit", text)

    def test_missing_transcript(self):
        self.make()
        self.assertIsNone(judge.run_input(self.proj, row(1, session_id=SID, agent_id="nope")))


class ValidateTest(unittest.TestCase):
    def test_good_shape(self):
        got = judge.validate(good_answer("yes"))
        self.assertEqual(set(got), set(judge.QUESTIONS))
        self.assertEqual(got["off_brief"], ("yes", REASON))

    def test_bad_shapes_give_none(self):
        bad = good_answer()
        missing = {k: v for k, v in bad.items() if k != "scope_grew"}
        extra = dict(bad, other={"answer": "no", "reason": ""})
        wrong_answer = dict(bad, off_brief={"answer": "maybe", "reason": "x"})
        extra_key = dict(bad, off_brief={"answer": "no", "reason": "x", "more": 1})
        no_reason = dict(bad, off_brief={"answer": "no"})
        for case in (None, [], "yes", {}, missing, extra, wrong_answer, extra_key, no_reason):
            self.assertIsNone(judge.validate(case), case)

    def test_bad_shape_stores_unknown_for_all_six(self):
        result, ok = judge.judge_one("x", lambda t: {"brief_unclear": "yes"})
        self.assertFalse(ok)
        self.assertEqual({a for a, _ in result.values()}, {"unknown"})
        self.assertEqual(len(result), 6)

    def test_failed_call_gives_unknown(self):
        with mock.patch.object(judge.subprocess, "run", side_effect=judge.subprocess.TimeoutExpired("claude", 1)):
            self.assertIsNone(judge.call_model("x"))
        with mock.patch.object(judge.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(judge.call_model("x"))
        with mock.patch.object(judge.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="not json")):
            self.assertIsNone(judge.call_model("x"))

    def test_call_flags(self):
        out = json.dumps({"structured_output": good_answer()})
        with mock.patch.object(judge.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=out)) as m:
            self.assertEqual(judge.call_model("x"), good_answer())
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--model") + 1], judge.JUDGE_MODEL)
        self.assertNotEqual(m.call_args[1]["cwd"], os.getcwd())
        self.assertEqual(m.call_args[1]["timeout"], judge.CALL_TIMEOUT)


class FlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.projects = os.path.join(self.root, "projects")
        self.proj = os.path.join(self.projects, "-p")
        os.makedirs(self.proj)
        self.ledger = os.path.join(self.root, "ledger", "ledger.db")
        old = transcripts.PROJECTS
        transcripts.PROJECTS = self.projects
        self.addCleanup(setattr, transcripts, "PROJECTS", old)
        # Rows that end now, so the period holds them. One has a transcript.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        db = score.Ledger(self.ledger)
        db.upsert(row(1, session_id=SID, agent_id="a1", ended_at=now))
        db.upsert(row(2, session_id=SID, agent_id="a2", ended_at=now))
        db.close()
        TranscriptTest.make(mock.Mock(proj=self.proj))
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.repo, ".claude", "skills", "zeta"))
        with open(os.path.join(self.repo, ".claude", "skills", "zeta", "SKILL.md"), "w") as fh:
            fh.write("---\nname: zeta\ndescription: ZETASUMMARY\n---\n")
        with open(os.path.join(self.proj, SID + ".jsonl")) as fh:
            old = fh.read()
        with open(os.path.join(self.proj, SID + ".jsonl"), "w") as fh:
            fh.write(dumps(dict(type="user", cwd=self.repo)) + "\n" + old)

    def run_judge(self, call, **kw):
        lines = []
        judge.run(self.proj, 1, self.ledger, kw.get("limit"), kw.get("dry_run", False), call, lines.append)
        return lines

    def test_dry_run_writes_nothing_and_calls_nothing(self):
        call = mock.Mock()
        lines = self.run_judge(call, dry_run=True)
        call.assert_not_called()
        self.assertEqual(len(lines), 3)
        self.assertIn("dry run", lines[-1])
        db = score.Ledger(self.ledger)
        self.assertEqual(db.judged_ids(), set())
        db.close()
        self.assertFalse(os.path.exists(judge.reasons_path(self.ledger)))

    def test_run_stores_labels_and_keeps_reasons_out_of_ledger(self):
        lines = self.run_judge(lambda t: good_answer("yes"))
        self.assertIn("skipped", " ".join(lines))  # a2 has no transcript
        db = score.Ledger(self.ledger)
        self.assertEqual(db.labels_of(SID, "a1"), {k: "yes" for k in judge.QUESTIONS})
        db.close()
        with open(self.ledger, "rb") as fh:
            data = fh.read()
        for marker in (BRIEF, REPORT, REASON, "ZETASUMMARY"):
            self.assertNotIn(marker.encode(), data)
        with open(judge.reasons_path(self.ledger)) as fh:
            self.assertNotIn("ZETASUMMARY", fh.read())
        with open(judge.reasons_path(self.ledger)) as fh:
            self.assertIn(REASON, fh.read())

    def test_summary_reaches_the_call_only(self):
        seen = []
        self.run_judge(lambda t: seen.append(t) or good_answer())
        self.assertIn("ZETASUMMARY", seen[0])

    def test_judged_run_is_not_judged_again(self):
        self.run_judge(lambda t: good_answer())
        call = mock.Mock()
        self.run_judge(call)
        call.assert_not_called()

    def test_failed_call_is_counted_and_stored_unknown(self):
        lines = self.run_judge(lambda t: None)
        self.assertIn("1 failed calls", lines[-1])
        db = score.Ledger(self.ledger)
        self.assertEqual(set(db.labels_of(SID, "a1").values()), {"unknown"})
        db.close()

    def test_limit(self):
        self.assertIn("1 runs in the sample", self.run_judge(None, dry_run=True, limit=1)[-1])

    def test_mark_review_and_report(self):
        self.run_judge(lambda t: good_answer("yes"))
        with io.StringIO() as buf, mock.patch.object(sys, "stdout", buf):
            judge.mark(SID, "a1", "right", self.ledger)
        with self.assertRaises(SystemExit):
            judge.mark(SID, "a2", "wrong", self.ledger)  # never judged
        lines = []
        judge.review(5, self.ledger, lines.append)
        self.assertIn("checked: right", lines[0])
        self.assertTrue(any(REASON in line for line in lines))
        rep = []
        score.report(1, self.ledger, rep.append)
        text = "\n".join(rep)
        self.assertIn("JUDGE LABELS", text)
        self.assertRegex(text, r"off_brief\s+1\s+0\s+0")
        self.assertIn("marked right: 100%", text)
        self.assertNotIn(REASON, text)

    def test_report_on_ledger_without_judge_data(self):
        old = os.path.join(self.root, "old.db")
        import sqlite3
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE runs (%s, PRIMARY KEY (session_id, agent_id))" % ", ".join("%s %s" % c for c in score.COLUMNS))
        db.close()
        rep = []
        score.report(1, old, rep.append)
        self.assertIn("marked right: -", "\n".join(rep))


if __name__ == "__main__":
    unittest.main()
