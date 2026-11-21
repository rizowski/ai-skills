#!/usr/bin/env python3
"""Estimate what Claude Code sessions cost, from their transcripts.

Run with `python3 -I spend.py`. Standard library only.

  spend.py [--project PATH] [--days N]   report for one project
  spend.py --current [--project PATH]    context size and cost of the newest session

PATH is a repo folder or a folder under ~/.claude/projects. The default is the
current folder. The output holds only numbers, model names, agent types, tool
names and 8-character session ids, never transcript text.

All costs are ESTIMATES in weighted units, not dollars. Change the weights in
the two tables at the top of transcripts.py; nothing else holds a price.
"""

import argparse
import collections
import glob
import os
import statistics
import sys
import time

# -I drops the script folder from sys.path, so add it for the shared module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transcripts import (  # noqa: E402
    BIG_CONTEXT, HANDOFF_CONTEXT, PROJECTS, Spend, project_dir, read_session,
)


def pct(part, whole):
    return "%5.1f%%" % (100.0 * part / whole) if whole else "  n/a"


def current(project_arg):
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
    s = Spend()
    x = read_session(s, proj, path)
    main = x["main"]
    ctx = main[-1]["ctx"] if main else 0
    print("ESTIMATE. Session %s, %s." % (x["sid"], how))
    print("model of last turn: %s" % (main[-1]["model"] if main else "?"))
    # The transcript records the advisor setting. Claude Code attaches the
    # advisor only when it ranks at or above the main model.
    print("advisor setting: %s" % (x["advisor_model"] or "none recorded"))
    print("context now: %d tokens (handoff near %d)" % (ctx, HANDOFF_CONTEXT))
    print("main turns: %d   subagents: %d" % (len(main), len(x["agents"])))
    print("cost so far: %d units (main %d, subagents %d, advisor %d)" % (
        x["main_cost"] + x["sub_cost"], x["main_cost"], x["sub_cost"], s.advisor_cost))
    print("HANDOFF: yes" if ctx >= HANDOFF_CONTEXT else "HANDOFF: not yet")


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
    p.add_argument("--current", action="store_true", help="context size and cost of the newest session")
    args = p.parse_args()
    if args.current:
        current(args.project)
    else:
        report(project_dir(args.project), args.days)


if __name__ == "__main__":
    main()
