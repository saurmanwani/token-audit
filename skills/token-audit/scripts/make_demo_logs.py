#!/usr/bin/env python3
"""Write a fake Claude Code config directory with synthetic session logs.

Lets you try analyze_usage.py, or test this skill, without real usage data:

    python3 make_demo_logs.py long-sessions /tmp/demo-config
    python3 analyze_usage.py --config-dir /tmp/demo-config

Scenarios:
  long-sessions    long uncompacted sessions resumed after idle breaks
  thin-data        two days of light use, too little to tune on
  wasteful-agents  tiny sub-agent runs on an expensive model, noisy test
                   output, an unused agent and an oversized CLAUDE.md

Timestamps are relative to now, so the data always falls inside the default
14-day window. Output is deterministic apart from the timestamps.
"""
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

OPUS = "claude-opus-5-5"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class Writer:
    def __init__(self, root):
        self.root = root
        self.project = os.path.join(root, "projects", "-demo-project")
        os.makedirs(self.project, exist_ok=True)
        self.n = 0

    def uid(self, prefix):
        self.n += 1
        return "%s_%06d" % (prefix, self.n)

    def open(self, session, agent=None, agent_type=None):
        if agent is None:
            path = os.path.join(self.project, session + ".jsonl")
        else:
            d = os.path.join(self.project, session, "subagents")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "agent-%s.jsonl" % agent)
            with open(path[:-6] + ".meta.json", "w") as fh:
                json.dump({"agentType": agent_type, "finished": True}, fh)
        return open(path, "a")


def run_context(w, fh, session, start, turns, *, model=OPUS, effort="medium", sub=False,
                base=30000, tool=None, gaps=None, compact_at=None, spawns=None, rng=None):
    """Write one conversation. Returns the time of its last request.

    tool:   function(i) -> (name, input dict, result chars) or None
    gaps:   {turn index: seconds idle before that turn}
    spawns: {turn index: (agent type, model param or None, sub-agent turns)}
    """
    ts = start
    ctx = base
    prev_ctx = 0
    fh.write(json.dumps({"type": "user", "timestamp": ts.isoformat(), "sessionId": session, "isSidechain": sub,
                         "message": {"role": "user", "content": "demo prompt"}}) + "\n")
    for i in range(turns):
        gap = (gaps or {}).get(i, rng.randint(20, 90))
        ts += timedelta(seconds=gap)
        if compact_at and i == compact_at:
            fh.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": ts.isoformat(),
                                 "sessionId": session}) + "\n")
            ctx, prev_ctx = base + 8000, 0
        read = 0 if gap > 3600 else prev_ctx
        usage = {"input_tokens": rng.randint(2, 40), "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": ctx - read,
                 "cache_creation": {"ephemeral_1h_input_tokens": 0 if sub else ctx - read,
                                    "ephemeral_5m_input_tokens": ctx - read if sub else 0},
                 "output_tokens": rng.randint(150, 600)}
        content = [{"type": "text", "text": "ok"}]
        result = None
        spawn = (spawns or {}).get(i)
        if spawn:
            agent_type, model_param, sub_turns = spawn
            tid = w.uid("toolu")
            inp = {"subagent_type": agent_type, "description": "demo task", "prompt": "demo brief"}
            if model_param:
                inp["model"] = model_param
            content.append({"type": "tool_use", "id": tid, "name": "Agent", "input": inp})
            result = (tid, 1200)
            aid = w.uid("a")
            with w.open(session, aid, agent_type) as sfh:
                run_context(w, sfh, session, ts, sub_turns, sub=True, base=18000, rng=rng,
                            model="claude-sonnet-5-5" if model_param == "sonnet" else OPUS,
                            tool=lambda j: ("Grep", {"pattern": "TODO"}, 900) if j < sub_turns - 1 else None)
        elif tool and tool(i):
            name, inp, chars = tool(i)
            tid = w.uid("toolu")
            content.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
            result = (tid, chars)
        fh.write(json.dumps({"type": "assistant", "timestamp": ts.isoformat(), "sessionId": session,
                             "isSidechain": sub, "effort": effort,
                             "message": {"id": w.uid("msg"), "model": model, "role": "assistant",
                                         "usage": usage, "content": content}}) + "\n")
        prev_ctx = ctx
        grow = usage["output_tokens"] + rng.randint(200, 900)
        if result:
            ts += timedelta(seconds=rng.randint(2, 20))
            fh.write(json.dumps({"type": "user", "timestamp": ts.isoformat(), "sessionId": session,
                                 "isSidechain": sub,
                                 "message": {"role": "user", "content": [
                                     {"type": "tool_result", "tool_use_id": result[0],
                                      "content": "x" * result[1]}]}}) + "\n")
            grow += result[1] // 4
        ctx += grow
    return ts


def write_config(root, claude_md_lines, agents, model="opus"):
    lines = ["# Global instructions", ""]
    topics = ["Use TypeScript strict mode.", "Prefer small functions.", "Write tests for new code.",
              "Follow the existing code style.", "Use descriptive variable names."]
    i = 0
    while len(lines) < claude_md_lines:
        if i % 12 == 0:
            lines += ["", "## Section %d" % (i // 12 + 1), ""]
        lines.append("- %s (rule %d)" % (topics[i % len(topics)], i + 1))
        i += 1
    with open(os.path.join(root, "CLAUDE.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(root, "settings.json"), "w") as fh:
        json.dump({"model": model}, fh, indent=2)
    if agents:
        os.makedirs(os.path.join(root, "agents"), exist_ok=True)
    for name, (amodel, desc) in agents.items():
        with open(os.path.join(root, "agents", name + ".md"), "w") as fh:
            fh.write("---\nname: %s\ndescription: %s\nmodel: %s\n---\n\nDo the task you are given.\n"
                     % (name, desc, amodel))


def long_sessions(w, rng):
    write_config(w.root, 40, {})
    mixed = lambda i: [("Read", {"file_path": "/demo/src/app.ts"}, 6000),
                       ("Bash", {"command": "git status"}, 400),
                       ("Edit", {"file_path": "/demo/src/app.ts"}, 300), None][i % 4]
    for d in range(9):
        day = NOW - timedelta(days=12 - d, hours=6)
        sid = "sess-long-%02d" % d
        turns = rng.randint(110, 170)
        gaps = {turns // 3: 2 * 3600 + 600, 2 * turns // 3: 5 * 3600} if d % 3 != 2 else {}
        with w.open(sid) as fh:
            run_context(w, fh, sid, day, turns, tool=mixed, gaps=gaps, rng=rng)


def thin_data(w, rng):
    write_config(w.root, 25, {})
    for d in range(2):
        sid = "sess-thin-%02d" % d
        with w.open(sid) as fh:
            run_context(w, fh, sid, NOW - timedelta(days=2 - d, hours=3), 18, rng=rng,
                        tool=lambda i: ("Read", {"file_path": "/demo/README.md"}, 2000) if i % 3 == 0 else None)


def wasteful_agents(w, rng):
    write_config(w.root, 320, {
        "grunt": ("sonnet", "Helps with tasks."),
        "researcher": ("inherit", "Research agent."),
    })
    for d in range(10):
        day = NOW - timedelta(days=13 - d, hours=5)
        sid = "sess-agents-%02d" % d
        turns = 45
        spawns = {i: ("general-purpose", None, rng.choice([2, 2, 3])) for i in range(4, turns, 5)}
        spawns[20] = ("researcher", None, 14)
        tool = lambda i: ("Bash", {"command": "npm test"}, 38000) if i % 6 == 1 else \
            (("Read", {"file_path": "/demo/src/index.ts"}, 3000) if i % 6 == 3 else None)
        with w.open(sid) as fh:
            run_context(w, fh, sid, day, turns, tool=tool, spawns=spawns, rng=rng,
                        effort="high" if d % 2 else "medium")


SCENARIOS = {"long-sessions": long_sessions, "thin-data": thin_data, "wasteful-agents": wasteful_agents}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in SCENARIOS:
        sys.exit(__doc__)
    root = sys.argv[2]
    if os.path.exists(os.path.join(root, "projects")):
        sys.exit("refusing to write into %s: it already has a projects directory" % root)
    os.makedirs(root, exist_ok=True)
    SCENARIOS[sys.argv[1]](Writer(root), random.Random(7))
    print("wrote %s demo config to %s" % (sys.argv[1], root))


if __name__ == "__main__":
    main()
