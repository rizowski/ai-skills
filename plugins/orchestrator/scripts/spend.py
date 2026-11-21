#!/usr/bin/env python3
"""Estimate what Claude Code sessions cost, from their transcripts.

Run with `python3 -I spend.py`. Standard library only.

  spend.py [--project PATH] [--days N]   report for one project
  spend.py --current [--project PATH]    context size and cost of the newest session

PATH is a repo folder or a folder under ~/.claude/projects. The default is the
current folder. The output holds only numbers, model names, agent types, tool
names and 8-character session ids, never transcript text.

All costs are ESTIMATES in weighted units, not dollars. Change the weights in
the two tables below; nothing else in the script holds a price.
"""

import argparse
import collections
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import time

# Relative price per model family. The first pattern that matches wins.
MODEL_WEIGHTS = [
    (r"fable|mythos", 25),
    (r"opus", 5),
    (r"sonnet", 3),
    (r"haiku", 1),
    (r"", 3),  # unknown model
]
# Relative price per token kind, as a multiple of plain input.
TOKEN_WEIGHTS = {
    "input": 1.0,
    "cache_read": 0.1,
    "cache_write_5m": 1.25,
    "cache_write_1h": 2.0,
    "output": 5.0,
}
BIG_CONTEXT = 150_000  # a turn above this counts as a "big context" turn
HANDOFF_CONTEXT = 120_000  # the orchestrator hands off near this context size

PROJECTS = os.path.expanduser("~/.claude/projects")


def model_weight(model):
    for pattern, weight in MODEL_WEIGHTS:
        if re.search(pattern, model or ""):
            return weight
    return MODEL_WEIGHTS[-1][1]


def call_cost(u, model):
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens", 0) if cc else u.get("cache_creation_input_tokens", 0)
    w1 = cc.get("ephemeral_1h_input_tokens", 0) if cc else 0
    t = TOKEN_WEIGHTS
    units = (
        t["input"] * u.get("input_tokens", 0)
        + t["cache_read"] * u.get("cache_read_input_tokens", 0)
        + t["cache_write_5m"] * w5
        + t["cache_write_1h"] * w1
        + t["output"] * u.get("output_tokens", 0)
    )
    return model_weight(model) * units


def context(u):
    return u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)


def split_usage(usage, model):
    """Return (calls, advisor_calls, context of the last call) for one response.

    A response can hold several API calls in `usage.iterations`. The top-level
    numbers add up the `message` calls only; `advisor_message` calls (the
    advisor model reading the transcript) appear only in the iterations.
    """
    its = usage.get("iterations") or []
    if not its:
        return [(usage, model)], [], context(usage)
    calls = [(i, model) for i in its if i.get("type") != "advisor_message"]
    advisor = [(i, i.get("model") or "?") for i in its if i.get("type") == "advisor_message"]
    last = context(calls[-1][0]) if calls else 0
    return calls, advisor, last


class Spend:
    """Reads transcripts and counts each API response and tool result once."""

    def __init__(self):
        self.seen = set()  # response keys and ("result", tool id) pairs
        self.duplicates = 0
        self.by_model = collections.Counter()

    @property
    def advisor_cost(self):
        return sum(c for m, c in self.by_model.items() if m.endswith(" (advisor)"))

    def responses(self, path, main_thread):
        """Return (responses, tool counts, tool result sizes, advisor setting) for one file.

        For a main-thread file, sidechain lines are skipped (the subagent files
        hold them) and tool result sizes are measured.
        """
        last = {}
        order = []
        tool_ids = collections.defaultdict(set)
        tool_names = {}
        results = []
        advisor_model = None
        with open(path, errors="ignore") as fh:
            for line in fh:
                if main_thread and '"isSidechain":true' in line:
                    continue
                if '"usage"' not in line and not (main_thread and '"tool_result"' in line):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message")
                if not isinstance(m, dict) or (main_thread and d.get("isSidechain")):
                    continue
                if d.get("type") == "assistant" and isinstance(m.get("usage"), dict):
                    key = m.get("id") or d.get("requestId") or d.get("uuid")
                    if key not in last:
                        order.append(key)
                    # Streaming writes one line per content block; the last line
                    # of a response holds the final usage.
                    last[key] = (m.get("model") or "?", m["usage"])
                    advisor_model = d.get("advisorModel") or advisor_model
                    for b in m.get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tool_ids[key].add(b.get("id"))
                            tool_names[b.get("id")] = b.get("name", "?")
                elif main_thread and d.get("type") == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            c = b.get("content")
                            results.append((b.get("tool_use_id"), len(c) if isinstance(c, str) else len(json.dumps(c))))
        out = []
        tools = collections.Counter()
        for key in order:
            if key in self.seen:
                self.duplicates += 1
                continue
            self.seen.add(key)
            tools.update(tool_names[t] for t in tool_ids[key])
            model, usage = last[key]
            if model == "<synthetic>":
                continue
            calls, advisor, ctx = split_usage(usage, model)
            cost = 0.0
            for u, mm in calls:
                c = call_cost(u, mm)
                self.by_model[model] += c
                cost += c
            for u, mm in advisor:
                c = call_cost(u, mm)
                self.by_model[mm + " (advisor)"] += c
                cost += c
            out.append(dict(model=model, cost=cost, ctx=ctx))
        sizes = []
        for tid, n in results:
            if ("result", tid) not in self.seen:
                self.seen.add(("result", tid))
                sizes.append((n, tool_names.get(tid, "?")))
        return out, tools, sizes, advisor_model


def project_dir(arg):
    path = os.path.abspath(os.path.expanduser(arg or os.getcwd()))
    if os.path.dirname(path) == PROJECTS:
        return path
    candidates = [path]
    try:
        top = subprocess.run(
            ["git", "-C", path, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout.splitlines()
        if top and top[0].startswith("worktree "):
            candidates.append(top[0][len("worktree "):])
    except (OSError, subprocess.SubprocessError):
        pass
    for c in candidates:
        d = os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", c))
        if os.path.isdir(d):
            return d
    sys.exit("No transcripts found for %s under %s" % (path, PROJECTS))


def subagent_files(proj, sid):
    return sorted(glob.glob(os.path.join(proj, sid, "subagents", "**", "agent-*.jsonl"), recursive=True))


def agent_meta(path):
    try:
        with open(path[: -len(".jsonl")] + ".meta.json") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def pct(part, whole):
    return "%5.1f%%" % (100.0 * part / whole) if whole else "  n/a"


def read_session(s, proj, path):
    """Read one session: its main thread and each of its subagents."""
    sid = os.path.basename(path)[: -len(".jsonl")]
    main, _, sizes, advisor_model = s.responses(path, main_thread=True)
    agents = []
    for f in subagent_files(proj, sid):
        rs, tools, _, _ = s.responses(f, main_thread=False)
        if rs:
            models = collections.Counter(r["model"] for r in rs)
            agents.append(dict(sid=sid[:8], type=agent_meta(f).get("agentType", "?"),
                               model=models.most_common(1)[0][0], calls=sum(tools.values()),
                               cost=sum(r["cost"] for r in rs), peak=max(r["ctx"] for r in rs), tools=tools))
    return dict(sid=sid[:8], main=main, sizes=sizes, advisor_model=advisor_model, agents=agents,
                main_cost=sum(r["cost"] for r in main), sub_cost=sum(a["cost"] for a in agents))


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
    print("ESTIMATE in weighted units (weights at the top of spend.py), last %d days." % days)
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
