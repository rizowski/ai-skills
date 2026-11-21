#!/usr/bin/env python3
"""Estimate what Claude Code sessions cost, from their transcripts.

Run with `python3 -I spend.py`. Standard library only.

  spend.py [--project PATH] [--days N]   report for one project
  spend.py --current [--project PATH]    where the spend of one session goes; also records it
  spend.py --trend [--weeks N]           one line per week from the recorded sessions

PATH is a repo folder or a folder under ~/.claude/projects. The default is the
current folder. The output holds only numbers, model names, agent types, tool
names and 8-character session ids, never transcript text.

All costs are ESTIMATES in weighted units, not dollars. Change the weights in
the two tables at the top of transcripts.py; nothing else holds a price.

--current writes one summary row per session (figures only) to the `spend_sessions`
table of the score.py ledger, and --trend reads those rows. A row holds the full session
id and the name of the project folder, and is the state at the last --current run. Both take --ledger FILE.
"""

import argparse
import collections
import glob
import os
import statistics
import sqlite3
import sys
import time
from datetime import datetime, timedelta

# -I drops the script folder from sys.path, so add it for the shared module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from score import DEFAULT_LEDGER  # noqa: E402
from transcripts import (  # noqa: E402
    BIG_CONTEXT, PROJECTS, TOKEN_WEIGHTS, Spend, project_dir, read_session, session_cwd,
)

KINDS = list(TOKEN_WEIGHTS)  # input, cache_read, cache_write_5m, cache_write_1h, output


def pct(part, whole):
    return "%5.1f%%" % (100.0 * part / whole) if whole else "  n/a"


def summarize(x, proj, date):
    """The figures of one session, as one flat row. Cost is in weighted units."""
    main = x["main"]
    tokens, kc = x["tokens"], x["kind_cost"]
    prompt = tokens["input"] + tokens["cache_read"] + tokens["cache_write_5m"] + tokens["cache_write_1h"]
    cwd = session_cwd(proj, x["session"])
    row = dict(session_id=x["session"], date=date,
               project=os.path.basename(cwd) if cwd else os.path.basename(proj),
               start_ctx=main[0]["ctx"] if main else 0, end_ctx=main[-1]["ctx"] if main else 0,
               turns=len(main), subagents=len(x["agents"]),
               hit_rate=tokens["cache_read"] / prompt if prompt else 0.0,
               main_cost=x["main_cost"], sub_cost=x["sub_cost"],
               advisor_cost=x["advisor_cost"], advisor_calls=x["advisor_calls"])
    for k in KINDS:
        row["tok_" + k] = tokens[k]
        row["cost_" + k] = kc[k]
    return row


def one_line(r):
    return ("start %dk, now %dk, hit %.0f%%, cost %d (main %d, subagents %d, advisor %d in %d calls)" % (
        r["start_ctx"] // 1000, r["end_ctx"] // 1000, 100 * r["hit_rate"],
        r["main_cost"] + r["sub_cost"], r["main_cost"], r["sub_cost"], r["advisor_cost"], r["advisor_calls"]))


ROW_COLUMNS = (["session_id TEXT PRIMARY KEY", "date TEXT", "project TEXT", "start_ctx INTEGER", "end_ctx INTEGER",
                "turns INTEGER", "subagents INTEGER", "hit_rate REAL", "main_cost REAL", "sub_cost REAL",
                "advisor_cost REAL", "advisor_calls INTEGER"]
               + ["tok_%s INTEGER" % k for k in KINDS] + ["cost_%s REAL" % k for k in KINDS])


def open_ledger(path):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS spend_sessions (%s)" % ", ".join(ROW_COLUMNS))
    return db


def record(db, row):
    names = list(row)
    db.execute("INSERT OR REPLACE INTO spend_sessions (%s) VALUES (%s)" % (
        ", ".join(names), ", ".join("?" * len(names))), [row[n] for n in names])
    db.commit()


def print_current(r):
    total = r["main_cost"] + r["sub_cost"]
    print("starting context: %d tokens   context now: %d tokens" % (r["start_ctx"], r["end_ctx"]))
    print("main turns: %d   subagents: %d   cache hit rate: %.1f%%" % (r["turns"], r["subagents"], 100 * r["hit_rate"]))
    kinds_cost = sum(r["cost_" + k] for k in KINDS)
    print("MAIN THREAD BY KIND (tokens, weighted cost, share; the advisor is not in these rows)")
    for k in KINDS:
        print("  %-15s %12d %12d %s" % (k, r["tok_" + k], r["cost_" + k], pct(r["cost_" + k], kinds_cost)))
    print("cost so far: %d units (main %d, subagents %d)" % (total, r["main_cost"], r["sub_cost"]))
    print("advisor on the main thread: %d units in %d calls (%s of main cost; the advisor reads uncached)" % (
        r["advisor_cost"], r["advisor_calls"], pct(r["advisor_cost"], r["main_cost"]).strip()))
    print("subagents: %d units (%s of cost)" % (r["sub_cost"], pct(r["sub_cost"], total).strip()))
    print("SPEND: " + one_line(r))


def current(project_arg, ledger_path):
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    found = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl")) if sid and not project_arg else []
    how = "this session (CLAUDE_CODE_SESSION_ID)"
    if found:
        path = found[0]
        proj = os.path.dirname(path)
    else:
        proj = project_dir(project_arg)
        mains = glob.glob(os.path.join(proj, "*.jsonl"))
        if not mains:
            sys.exit("No sessions in %s" % proj)
        path = max(mains, key=os.path.getmtime)
        how = "newest session in the project folder; WARNING: no session id, this can be another session"
    x = read_session(Spend(), proj, path)
    main = x["main"]
    print("ESTIMATE. Session %s, %s." % (x["sid"], how))
    print("model of last turn: %s" % (main[-1]["model"] if main else "?"))
    # The transcript records the advisor setting. Claude Code attaches the
    # advisor only when it ranks at or above the main model.
    print("advisor setting: %s" % (x["advisor_model"] or "none recorded"))
    row = summarize(x, proj, time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path))))
    print_current(row)
    if not found:
        print("not recorded in the ledger: the session id is unknown")
    elif main:
        try:
            db = open_ledger(ledger_path)
            record(db, row)
            db.close()
        except (sqlite3.Error, OSError) as e:
            print("ledger not written: %s" % type(e).__name__)


def trend(ledger_path, weeks, out=print):
    """One line per week: the figures that show whether spend improves."""
    db = open_ledger(ledger_path)
    since = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    rows = db.execute("SELECT * FROM spend_sessions WHERE date >= ? ORDER BY date", (since,)).fetchall()
    db.close()
    if not rows:
        out("No recorded sessions in the last %d weeks. Run `spend.py --current` in a session." % weeks)
        return
    by_week = collections.OrderedDict()
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        by_week.setdefault((d - timedelta(days=d.weekday())).strftime("%Y-%m-%d"), []).append(r)
    out("SPEND TREND (weighted units; week starts Monday; each session as of its last --current run)")
    out("week        sessions  median-start-ctx  cache-hit  cost/turn  advisor-share  subagent-share")
    for week, rs in by_week.items():
        prompt = sum(r["tok_input"] + r["tok_cache_read"] + r["tok_cache_write_5m"] + r["tok_cache_write_1h"] for r in rs)
        reads = sum(r["tok_cache_read"] for r in rs)
        main = sum(r["main_cost"] for r in rs)
        total = main + sum(r["sub_cost"] for r in rs)
        turns = sum(r["turns"] for r in rs)
        out("%s  %8d  %16d  %s  %9d  %s  %s" % (
            week, len(rs), statistics.median(r["start_ctx"] for r in rs), pct(reads, prompt),
            main / turns if turns else 0, pct(sum(r["advisor_cost"] for r in rs), main),
            pct(sum(r["sub_cost"] for r in rs), total)))


def report(proj, days):
    cutoff = time.time() - days * 86400
    s = Spend()
    sessions = []
    agents = []
    big_results = []
    for path in sorted(glob.glob(os.path.join(proj, "*.jsonl")), key=os.path.getmtime):
        if os.path.getmtime(path) < cutoff:
            continue
        x = read_session(s, proj, path)
        main = x["main"]
        big_results += x["sizes"]
        agents += x["agents"]
        sess = dict(sid=x["sid"], date=time.strftime("%m-%d", time.localtime(os.path.getmtime(path))),
                    turns=len(main), ctxs=[r["ctx"] for r in main], main=x["main_cost"],
                    big=sum(r["cost"] for r in main if r["ctx"] > BIG_CONTEXT),
                    sub=x["sub_cost"], subs=len(x["agents"]))
        if sess["turns"] or sess["subs"]:
            sessions.append(sess)
    if not sessions:
        sys.exit("No sessions in the last %d days in %s" % (days, proj))

    main_total = sum(x["main"] for x in sessions)
    sub_total = sum(x["sub"] for x in sessions)
    total = main_total + sub_total
    print("ESTIMATE in weighted units (weights at the top of transcripts.py), last %d days." % days)
    print("sessions %d, subagents %d, duplicate responses skipped %d" % (len(sessions), len(agents), s.duplicates))
    print("total %d   main thread %s   subagents %s   (advisor calls, in both: %s)" % (
        total, pct(main_total, total), pct(sub_total, total), pct(s.advisor_cost, total)))

    print("\nBY MODEL")
    for model, c in s.by_model.most_common():
        print("  %-28s %12d %s" % (model, c, pct(c, total)))

    allctx = [c for x in sessions for c in x["ctxs"]]
    print("\nMAIN THREAD CONTEXT PER TURN")
    if allctx:
        allctx.sort()
        print("  turns %d   median %d   p90 %d   max %d" % (
            len(allctx), statistics.median(allctx), allctx[int(0.9 * (len(allctx) - 1))], allctx[-1]))
    print("  main cost from turns over %dk: %s" % (BIG_CONTEXT // 1000, pct(sum(x["big"] for x in sessions), main_total)))
    starts = sorted(x["ctxs"][0] for x in sessions if x["ctxs"])
    if starts:
        print("  first-turn context: median %d" % statistics.median(starts))

    print("\nTOP SESSIONS (id date turns subagents cost subagent-share first-ctx max-ctx)")
    for x in sorted(sessions, key=lambda x: -(x["main"] + x["sub"]))[:8]:
        c = x["ctxs"]
        print("  %s %s %5d %4d %12d %s %8d %8d" % (
            x["sid"], x["date"], x["turns"], x["subs"], x["main"] + x["sub"],
            pct(x["sub"], x["main"] + x["sub"]), c[0] if c else 0, max(c) if c else 0))

    print("\nSUBAGENTS BY TYPE AND MODEL (count, cost share of subagents)")
    groups = collections.defaultdict(lambda: [0, 0.0])
    for a in agents:
        g = groups[(a["type"], a["model"])]
        g[0] += 1
        g[1] += a["cost"]
    for (t, m), (n, c) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        print("  %4d  %-28s %-24s %s" % (n, t, m, pct(c, sub_total)))
    if agents:
        calls = sorted(a["calls"] for a in agents)
        print("  tool calls per subagent: median %d   max %d" % (statistics.median(calls), calls[-1]))

    print("\nCOSTLIEST SUBAGENTS (session type model tool-calls peak-ctx cost top-tools)")
    for a in sorted(agents, key=lambda a: -a["cost"])[:5]:
        top = ",".join("%s:%d" % kv for kv in a["tools"].most_common(3))
        print("  %s %s %s %d %d %d %s" % (a["sid"], a["type"], a["model"], a["calls"], a["peak"], a["cost"], top))

    print("\nLARGEST TOOL RESULTS ON THE MAIN THREAD (chars, tool)")
    for n, name in sorted(big_results, reverse=True)[:5]:
        print("  %9d %s" % (n, name))


def main():
    p = argparse.ArgumentParser(description="Estimate Claude Code session cost from transcripts.")
    p.add_argument("--project", help="repo folder or ~/.claude/projects/<slug> folder (default: current folder)")
    p.add_argument("--days", type=int, default=10, help="days of transcripts to read (default 10)")
    p.add_argument("--current", action="store_true", help="where the spend of the newest session goes; records it")
    p.add_argument("--trend", action="store_true", help="one line per week from the recorded sessions")
    p.add_argument("--weeks", type=int, default=8, help="weeks for --trend (default 8)")
    p.add_argument("--ledger", default=DEFAULT_LEDGER, help="ledger file (default %s)" % DEFAULT_LEDGER)
    args = p.parse_args()
    if args.current and args.trend:
        p.error("use --current or --trend, not both")
    if args.current:
        current(args.project, args.ledger)
    elif args.trend:
        trend(args.ledger, args.weeks)
    else:
        report(project_dir(args.project), args.days)


if __name__ == "__main__":
    main()
