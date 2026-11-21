#!/usr/bin/env python3
"""Score Claude Code subagent runs ("workers") from the saved transcripts.

Run with `python3 -I score.py`. Standard library only.

  score.py scan [--project PATH] [--days N] [--ledger FILE]
      One row per subagent run in a SQLite ledger. Safe to run again: the row key
      is (session id, agent id). Without --days it reads every transcript.
  score.py report [--weeks 4] [--ledger FILE]
      Failure kinds by total cost, denials by rule, cost by model, cost per pull
      request, and the top runs.
  score.py pr-cost --repo OWNER/NAME [--ledger FILE]
      One line per pull request of that repository: number, runs, cost (tab separated).

Hook events come from the hook event log (~/.claude/hook-events/events.jsonl, or
$CLAUDE_HOOKS_LOG_DIR/events.jsonl) for a run that started at or after the first
line of the log. Older runs, and every run when the log is missing or empty, use
the fixed texts in the transcript. The two are never added.

The ledger holds ids, model names, agent types, counts, branch names, PR numbers
and times. It never holds transcript text. No network call is made.
"""

import argparse
import collections
import glob
import json
import os
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# -I drops the script folder from sys.path, so add it for the shared module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transcripts import BIG_CONTEXT, Spend, project_dir, read_session, subagent_files  # noqa: E402

# Limits for the failure kinds.
MAX_TOOL_CALLS = 60
MAX_CONTEXT = BIG_CONTEXT
CODE_WORKER_TYPE = "orchestrator:worker"
GENERAL_TYPE = "general-purpose"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
FAILURE_KINDS = ["no_model", "over_calls", "gate_gave_up", "no_commit", "no_pr", "big_context"]

# Fixed texts that mark a hook event in a transcript. They serve runs from before
# the hook event log; only detect_hook_event() reads this table.
HOOK_TEXTS = {
    "denial": "Workers may not run or change",
    "gate_event": "SubagentStop",  # hook event of a finish gate block
    "giveup_start": "Finish gate: the work in",
    "giveup_contains": "still does not pass after 3 blocked stops",
}

DEFAULT_LEDGER = os.path.expanduser("~/.claude/orchestrator-ledger/ledger.db")
AGENT_TOOLS = ("Agent", "Task")
UNKNOWN_RULE = "unknown"  # a text-matched denial does not name its rule
# A command starts a line or follows ; & | or (. A commit counts when the Bash result is not an
# error and does not say that nothing was committed (commit -q prints nothing on success).
COMMIT_CMD = re.compile(r"(?:^|[\n;&|(])\s*git\s+(?:-C\s+\S+\s+)?commit\b")
COMMIT_EMPTY = re.compile(r"nothing (?:added )?to commit|no changes added to commit")
PR_CMD = re.compile(r"(?:^|[\n;&|(])\s*gh\s+pr\s+create\b")
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")

COLUMNS = [
    ("session_id", "TEXT NOT NULL"), ("agent_id", "TEXT NOT NULL"), ("agent_type", "TEXT"),
    ("model", "TEXT"), ("model_set", "INTEGER"), ("tool_calls", "INTEGER"),
    ("gate_blocks", "INTEGER"), ("gate_giveups", "INTEGER"), ("denials", "INTEGER"),
    ("code_worker", "INTEGER"), ("ended_with_commit", "INTEGER"), ("ended_with_pr", "INTEGER"),
    ("pr_repo", "TEXT"), ("pr_number", "INTEGER"), ("branch", "TEXT"), ("peak_context", "INTEGER"),
    ("cost", "REAL"), ("machine", "TEXT"), ("project", "TEXT"), ("started_at", "TEXT"), ("ended_at", "TEXT"),
]
NAMES = [c for c, _ in COLUMNS]


class Ledger:
    """The only place that holds SQL. A later slice points it at a libSQL server."""

    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        cols = ", ".join("%s %s" % c for c in COLUMNS)
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (%s, PRIMARY KEY (session_id, agent_id))" % cols)
        self.db.execute("CREATE TABLE IF NOT EXISTS run_denials (session_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
                        "rule TEXT NOT NULL, count INTEGER, PRIMARY KEY (session_id, agent_id, rule))")

    def upsert(self, row):
        self.db.execute("INSERT OR REPLACE INTO runs (%s) VALUES (%s)" % (
            ", ".join(NAMES), ", ".join("?" * len(NAMES))), [row.get(n) for n in NAMES])
        self.db.commit()

    def set_denials(self, session_id, agent_id, by_rule):
        """Replace the denial counts of one run. by_rule: rule name -> count."""
        self.db.execute("DELETE FROM run_denials WHERE session_id = ? AND agent_id = ?", (session_id, agent_id))
        self.db.executemany("INSERT INTO run_denials VALUES (?, ?, ?, ?)",
                            [(session_id, agent_id, rule, n) for rule, n in by_rule.items() if n])
        self.db.commit()

    def denials_since(self, iso):
        """Rule name -> total count, for runs that ended at or after iso."""
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT d.rule, SUM(d.count) FROM run_denials d JOIN runs r USING (session_id, agent_id) "
            "WHERE r.ended_at >= ? GROUP BY d.rule ORDER BY 2 DESC, 1", (iso,))}

    def pr_costs(self, repo):
        """(number, runs, cost) per pull request of one repository."""
        return [tuple(r) for r in self.db.execute(
            "SELECT pr_number, COUNT(*), SUM(cost) FROM runs WHERE pr_repo = ? AND pr_number IS NOT NULL "
            "GROUP BY pr_number ORDER BY pr_number", (repo,))]

    def runs_since(self, iso):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM runs WHERE ended_at >= ? ORDER BY ended_at", (iso,))]

    def close(self):
        self.db.close()


def text_of(content):
    """Flatten a tool result's content to one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def detect_hook_event(rec):
    """Return how many ("denial" | "gate_block" | "gate_giveup") events one record holds."""
    out = collections.Counter()
    att = rec.get("attachment")
    if isinstance(att, dict):
        if att.get("type") == "hook_blocking_error" and att.get("hookEvent") == HOOK_TEXTS["gate_event"]:
            out["gate_block"] += 1
        elif att.get("type") == "hook_system_message":
            c = att.get("content")
            if isinstance(c, str) and c.startswith(HOOK_TEXTS["giveup_start"]) and HOOK_TEXTS["giveup_contains"] in c:
                out["gate_giveup"] += 1
    m = rec.get("message")
    if rec.get("type") == "user" and isinstance(m, dict) and isinstance(m.get("content"), list):
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result" and HOOK_TEXTS["denial"] in text_of(b.get("content")):
                out["denial"] += 1
    return out


def parse_time(text):
    """An ISO 8601 time as an aware datetime, or None. A time without a zone counts as UTC."""
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def hook_log_path():
    folder = os.environ.get("CLAUDE_HOOKS_LOG_DIR") or os.path.expanduser("~/.claude/hook-events")
    return os.path.join(folder, "events.jsonl")


def load_hook_log(path=None):
    """Read the hook event log. Returns (time of its first line or None, events).

    events maps (session id, agent id) to a Counter of "deny:<rule>", "gate_block", "gate_giveup".
    A missing file, an unreadable file and a broken line all give less data, never an error.
    """
    first, events = None, collections.defaultdict(collections.Counter)
    try:
        lines = list(read_jsonl(path or hook_log_path()))
    except OSError:
        return None, events
    for rec in lines:
        if not isinstance(rec, dict):
            continue
        when = parse_time(rec.get("time"))
        if when is None:
            continue
        first = first or when
        hook, kind = rec.get("hook"), rec.get("kind")
        if hook == "policy-guard" and kind == "deny":
            what = "deny:%s" % (str(rec.get("rule") or UNKNOWN_RULE)[:40])
        elif hook == "finish-gate" and kind == "block":
            what = "gate_block"
        elif hook == "finish-gate" and kind == "give_up":
            what = "gate_giveup"
        else:
            continue
        sid, aid = rec.get("session_id"), rec.get("agent_id")
        if isinstance(sid, str) and isinstance(aid, str):
            events[(sid, aid)][what] += 1
    return first, events


def read_jsonl(path):
    with open(path, errors="ignore") as fh:
        for line in fh:
            if line.startswith('{"type":"api-request-blob"'):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def blocks(rec, kind):
    m = rec.get("message")
    if isinstance(m, dict) and isinstance(m.get("content"), list):
        return [b for b in m["content"] if isinstance(b, dict) and b.get("type") == kind]
    return []


def run_events(path):
    """Hook events, commit, PR, branch and times of one subagent file."""
    ev = dict(denials=0, gate_blocks=0, gate_giveups=0, commit=False, pr_repo=None, pr_number=None,
              branch=None, start=None, end=None)
    bash = {}  # tool id -> "commit" | "pr"
    for rec in read_jsonl(path):
        ts = rec.get("timestamp")
        if ts:
            ev["start"] = ev["start"] or ts
            ev["end"] = ts
        ev["branch"] = rec.get("gitBranch") or ev["branch"]
        found = detect_hook_event(rec)
        ev["denials"] += found["denial"]
        ev["gate_blocks"] += found["gate_block"]
        ev["gate_giveups"] += found["gate_giveup"]
        for b in blocks(rec, "tool_use"):
            cmd = (b.get("input") or {}).get("command") if b.get("name") == "Bash" else None
            if isinstance(cmd, str):
                if COMMIT_CMD.search(cmd):
                    bash[b.get("id")] = "commit"
                elif PR_CMD.search(cmd):
                    bash[b.get("id")] = "pr"
        for b in blocks(rec, "tool_result"):
            kind = bash.get(b.get("tool_use_id"))
            if b.get("is_error") or not kind:
                continue
            text = text_of(b.get("content"))
            if kind == "commit" and not COMMIT_EMPTY.search(text):
                ev["commit"] = True
            elif kind == "pr":
                urls = PR_URL.findall(text)
                if urls:
                    ev["pr_repo"], ev["pr_number"] = urls[-1][0], int(urls[-1][1])
    return ev


def launch_models(proj, sid):
    """Map agent id -> was `model` given on the Agent call that started it, for one session."""
    given = {}  # tool use id -> bool
    started = {}  # tool use id -> agent id
    files = [os.path.join(proj, sid + ".jsonl")] + subagent_files(proj, sid)
    for f in files:
        with open(f, errors="ignore") as fh:
            for line in fh:
                is_call = '"name":"Agent"' in line or '"name":"Task"' in line
                is_result = '"toolUseResult"' in line and '"agentId"' in line
                if not (is_call or is_result):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for b in blocks(rec, "tool_use"):
                    if b.get("name") in AGENT_TOOLS:
                        given[b.get("id")] = bool((b.get("input") or {}).get("model"))
                res = rec.get("toolUseResult")
                if isinstance(res, dict) and res.get("agentId"):
                    for b in blocks(rec, "tool_result"):
                        started[b.get("tool_use_id")] = res["agentId"]
    return {aid: given[tid] for tid, aid in started.items() if tid in given}


def apply_hook_log(ev, session_id, agent_id, log):
    """Set the hook counts of one run. A run takes its counts from the log only when the log
    holds an event for it, or when it started at or after the first line of the log. Any
    other run keeps its text-match counts."""
    log_start, events = log
    start = parse_time(ev["start"])
    by_rule = {UNKNOWN_RULE: ev["denials"]} if ev["denials"] else {}
    in_log = (session_id, agent_id) in events
    if in_log or (log_start is not None and start is not None and start >= log_start):
        c = events.get((session_id, agent_id), collections.Counter())
        by_rule = {k[len("deny:"):]: n for k, n in c.items() if k.startswith("deny:")}
        ev = dict(ev, gate_blocks=c["gate_block"], gate_giveups=c["gate_giveup"])
    ev["denials"] = sum(by_rule.values())
    return ev, by_rule


def build_row(agent, ev, model_set, project, machine):
    tools = agent["tools"]
    code = agent["type"] == CODE_WORKER_TYPE or (agent["type"] == GENERAL_TYPE and any(t in EDIT_TOOLS for t in tools))
    return dict(
        session_id=agent["session"], agent_id=agent["agent_id"], agent_type=agent["type"], model=agent["model"],
        model_set=None if model_set is None else int(model_set), tool_calls=agent["calls"],
        gate_blocks=ev["gate_blocks"], gate_giveups=ev["gate_giveups"], denials=ev["denials"],
        code_worker=int(code), ended_with_commit=int(ev["commit"]), ended_with_pr=int(ev["pr_number"] is not None),
        pr_repo=ev["pr_repo"], pr_number=ev["pr_number"], branch=ev["branch"], peak_context=agent["peak"],
        cost=agent["cost"], machine=machine, project=project, started_at=ev["start"], ended_at=ev["end"],
    )


def failures(row):
    """The failure kinds of one row. A policy guard denial is never one."""
    code = bool(row["code_worker"])
    return {
        "no_model": row["model_set"] == 0,
        "over_calls": (row["tool_calls"] or 0) > MAX_TOOL_CALLS,
        "gate_gave_up": (row["gate_giveups"] or 0) > 0,
        "no_commit": code and not row["ended_with_commit"],
        "no_pr": code and not row["ended_with_pr"],
        "big_context": (row["peak_context"] or 0) > MAX_CONTEXT,
    }


def scan(project_arg, days, ledger_path):
    proj = project_dir(project_arg)
    cutoff = time.time() - days * 86400 if days else 0
    machine, project = socket.gethostname(), os.path.basename(proj)
    s = Spend()
    ledger = Ledger(ledger_path)
    log = load_hook_log()
    total = unknown = 0
    for path in sorted(glob.glob(os.path.join(proj, "*.jsonl")), key=os.path.getmtime):
        if os.path.getmtime(path) < cutoff:
            continue
        x = read_session(s, proj, path)
        if not x["agents"]:
            continue
        sid = os.path.basename(path)[: -len(".jsonl")]
        launched = launch_models(proj, sid)
        for a in x["agents"]:
            model_set = launched.get(a["agent_id"])
            unknown += model_set is None
            ev, by_rule = apply_hook_log(run_events(a["path"]), a["session"], a["agent_id"], log)
            ledger.upsert(build_row(a, ev, model_set, project, machine))
            ledger.set_denials(a["session"], a["agent_id"], by_rule)
            total += 1
    ledger.close()
    print("scanned %d runs, model_set unknown for %d, ledger %s" % (total, unknown, ledger_path))


def report(weeks, ledger_path, out=print):
    ledger = Ledger(ledger_path)
    since = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = ledger.runs_since(since)
    denials = ledger.denials_since(since)
    ledger.close()
    out("Last %d weeks: %d runs, cost %d weighted units (estimate)" % (weeks, len(rows), sum(r["cost"] for r in rows)))
    kinds = {k: [0, 0.0] for k in FAILURE_KINDS}
    for r in rows:
        for k, bad in failures(r).items():
            if bad:
                kinds[k][0] += 1
                kinds[k][1] += r["cost"]
    out("\nFAILURE KINDS (count, total cost)")
    for k, (n, c) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
        out("  %-13s %5d %12d" % (k, n, c))
    out("\nDENIALS BY RULE (count)")
    for rule, n in denials.items():
        out("  %-24s %5d" % (rule, n))
    if not denials:
        out("  none")
    models = collections.defaultdict(lambda: [0, 0.0, 0])
    for r in rows:
        m = models[r["model"] or "unknown"]
        m[0] += 1
        m[1] += r["cost"]
        m[2] += any(failures(r).values())
    out("\nBY MODEL (runs, total cost, share of runs with a failure)")
    for k, (n, c, bad) in sorted(models.items(), key=lambda kv: -kv[1][1]):
        out("  %-28s %5d %12d %5d%%" % (k, n, c, round(100.0 * bad / n)))
    out("\nCOST PER PULL REQUEST (repository#number, runs, cost)")
    prs = collections.defaultdict(lambda: [0, 0.0])
    for r in rows:
        if r["pr_number"] is not None:
            p = prs["%s#%d" % (r["pr_repo"], r["pr_number"])]
            p[0] += 1
            p[1] += r["cost"]
    for k, (n, c) in sorted(prs.items(), key=lambda kv: -kv[1][1])[:10]:
        out("  %-40s %3d %12d" % (k, n, c))
    out("\nTOP RUNS (session agent type model calls peak-ctx cost branch failures)")
    for r in sorted(rows, key=lambda r: -r["cost"])[:10]:
        bad = ",".join(k for k, v in failures(r).items() if v) or "-"
        out("  %s %s %s %s %d %d %d %s %s" % (r["session_id"][:8], r["agent_id"], r["agent_type"], r["model"],
                                              r["tool_calls"], r["peak_context"], r["cost"], r["branch"] or "-", bad))


def main():
    p = argparse.ArgumentParser(description="Score subagent runs from Claude Code transcripts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("scan", help="write or update one ledger row per subagent run")
    a.add_argument("--project", help="repo folder or ~/.claude/projects/<slug> folder (default: current folder)")
    a.add_argument("--days", type=int, help="days of transcripts to read (default: all)")
    a.add_argument("--ledger", default=DEFAULT_LEDGER)
    b = sub.add_parser("report", help="failure kinds, cost per pull request, top runs")
    b.add_argument("--weeks", type=int, default=4)
    b.add_argument("--ledger", default=DEFAULT_LEDGER)
    c = sub.add_parser("pr-cost", help="number, runs and cost per pull request of one repository")
    c.add_argument("--repo", required=True, help="OWNER/NAME")
    c.add_argument("--ledger", default=DEFAULT_LEDGER)
    args = p.parse_args()
    if args.cmd == "scan":
        scan(args.project, args.days, args.ledger)
        return
    if not os.path.exists(args.ledger):
        sys.exit("No ledger at %s. Run scan first." % args.ledger)
    if args.cmd == "report":
        report(args.weeks, args.ledger)
    else:
        ledger = Ledger(args.ledger)
        for number, runs, cost in ledger.pr_costs(args.repo):
            print("%d\t%d\t%d" % (number, runs, cost))
        ledger.close()


if __name__ == "__main__":
    main()
