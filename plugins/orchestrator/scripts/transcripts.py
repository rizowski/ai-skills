"""Shared transcript parsing for spend.py and score.py.

Standard library only. It reads ~/.claude/projects/<project>/<session>.jsonl and
<session>/subagents/**/agent-*.jsonl with their .meta.json. All weights live in
the two tables below; nothing else holds a price.
"""

import collections
import glob
import json
import os
import re
import subprocess
import sys

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


def read_session(s, proj, path):
    """Read one session: its main thread and each of its subagents."""
    sid = os.path.basename(path)[: -len(".jsonl")]
    main, _, sizes, advisor_model = s.responses(path, main_thread=True)
    agents = []
    for f in subagent_files(proj, sid):
        rs, tools, _, _ = s.responses(f, main_thread=False)
        if rs:
            models = collections.Counter(r["model"] for r in rs)
            agents.append(dict(sid=sid[:8], session=sid, path=f, agent_id=os.path.basename(f)[len("agent-"):-len(".jsonl")],
                               type=agent_meta(f).get("agentType", "?"),
                               model=models.most_common(1)[0][0], calls=sum(tools.values()),
                               cost=sum(r["cost"] for r in rs), peak=max(r["ctx"] for r in rs), tools=tools))
    return dict(sid=sid[:8], main=main, sizes=sizes, advisor_model=advisor_model, agents=agents,
                main_cost=sum(r["cost"] for r in main), sub_cost=sum(a["cost"] for a in agents))
