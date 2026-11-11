#!/usr/bin/env python3
"""Summarise where Claude Code tokens went, from the local session logs.

Reads the JSONL session logs under <config dir>/projects (default ~/.claude),
covering the last N days, and prints a compact report: token classes, models,
main session vs sub-agents, context growth, cache rebuilds, tool output volume,
sub-agent usage and the size of always-loaded config.

Standard library only. Nothing is sent anywhere; the only file written is a
one-line history record in <config dir>/token-audit/history.jsonl (skip with
--no-save) so that a later run can show what changed.

The log format is not a documented interface and changes between Claude Code
versions, so every field is read defensively and unknown shapes are skipped.
"""
import argparse
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# API list prices in USD per million tokens:
# (input, cache write 5m, cache write 1h, cache read, output).
# Used only to weight token classes against each other. Subscription plans do
# not publish how they weight models, so treat "cost" as a relative estimate.
# Edit these if prices change.
PRICES = {
    "fable": (8.0, 10.0, 16.0, 0.20, 40.0),
    "opus": (4.0, 5.0, 8.0, 0.20, 20.0),
    "sonnet": (2.0, 2.5, 4.0, 0.10, 10.0),
    "haiku": (0.1, 0.125, 0.2, 0.01, 0.5),
}
FALLBACK_FAMILY = "opus"
TINY_RUN_TOOL_CALLS = 2      # a sub-agent run with this few tool calls was probably not worth a spawn
REBUILD_MIN_CONTEXT = 20000  # ignore cache misses on small contexts
BIG_CONTEXT = 100000


def family(model):
    m = (model or "").lower()
    for name in PRICES:
        if name in m:
            return name
    return None


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def usage_parts(usage):
    """Return (input, write5m, write1h, read, output) token counts."""
    g = lambda k: usage.get(k) or 0
    write = g("cache_creation_input_tokens")
    detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w1h = detail.get("ephemeral_1h_input_tokens") or 0
    w5m = detail.get("ephemeral_5m_input_tokens") or 0
    if w1h + w5m != write:
        w5m, w1h = write, 0
    return (g("input_tokens"), w5m, w1h, g("cache_read_input_tokens"), g("output_tokens"))


def cost_of(parts, model):
    p = PRICES[family(model) or FALLBACK_FAMILY]
    return sum(t * price for t, price in zip(parts, p)) / 1e6


def result_chars(content):
    if isinstance(content, str):
        return len(content)
    total = 0
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                total += len(b.get("text") or "")
    return total


def tool_label(name, inp, redact):
    if redact or not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "url", "pattern", "query", "description"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            v = " ".join(v.split())
            return v if len(v) <= 70 else v[:67] + "..."
    return ""


def load(config_dir, days, redact):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_epoch = cutoff.timestamp()
    requests = {}      # message id -> record
    tool_uses = {}     # tool_use id -> record
    results = {}       # tool_use id -> chars
    compactions = Counter()
    agent_meta = {}    # run key -> meta dict
    files_read = 0
    pattern = os.path.join(config_dir, "projects", "**", "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_epoch:
                continue
        except OSError:
            continue
        files_read += 1
        is_sub_file = os.sep + "subagents" + os.sep in path
        run_key = None
        if is_sub_file:
            run_key = path
            meta_path = path[: -len(".jsonl")] + ".meta.json"
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    agent_meta[run_key] = json.load(fh)
            except (OSError, ValueError):
                agent_meta[run_key] = {}
        file_session = os.path.basename(os.path.dirname(os.path.dirname(path))) if is_sub_file \
            else os.path.splitext(os.path.basename(path))[0]
        try:
            fh = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                ts = parse_ts(d.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                kind = d.get("type")
                session = file_session
                sidechain = bool(d.get("isSidechain")) or is_sub_file
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                content = msg.get("content") if isinstance(msg.get("content"), list) else []
                if kind == "system" and d.get("subtype") == "compact_boundary":
                    compactions[session] += 1
                elif kind == "assistant":
                    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                    mid = msg.get("id") or d.get("uuid")
                    model = msg.get("model") or ""
                    if usage and mid and not model.startswith("<"):
                        parts = usage_parts(usage)
                        prev = requests.get(mid)
                        # one API response is logged once per content block; keep one copy
                        if prev is None or parts[4] >= prev["parts"][4]:
                            requests[mid] = {
                                "ts": prev["ts"] if prev else ts, "model": model, "parts": parts,
                                "session": session, "sub": sidechain, "run": run_key,
                                "effort": d.get("effort"),
                            }
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            tool_uses[b["id"]] = {
                                "name": b.get("name") or "?", "session": session, "sub": sidechain,
                                "run": run_key, "label": tool_label(b.get("name"), inp, redact),
                                "file": inp.get("file_path") if b.get("name") == "Read" else None,
                                "agent_type": inp.get("subagent_type"), "agent_model": inp.get("model"),
                            }
                elif kind == "user":
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                            results[b["tool_use_id"]] = result_chars(b.get("content"))
    return requests, tool_uses, results, compactions, agent_meta, files_read


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def median(values):
    return int(statistics.median(values)) if values else 0


def p90(values):
    if not values:
        return 0
    s = sorted(values)
    return int(s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))])


def analyse(config_dir, days, redact):
    requests, tool_uses, results, compactions, agent_meta, files_read = load(config_dir, days, redact)
    out = {"days": days, "files_read": files_read, "requests": len(requests)}
    if not requests:
        return out
    reqs = sorted(requests.values(), key=lambda r: r["ts"])
    classes = ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]
    tok = Counter()
    cost = Counter()
    by_model = defaultdict(lambda: {"requests": 0, "tokens": 0, "cost": 0.0})
    scope_cost = Counter()
    effort = Counter()
    days_active = set()
    unknown_models = Counter()
    for r in reqs:
        p = PRICES[family(r["model"]) or FALLBACK_FAMILY]
        if family(r["model"]) is None:
            unknown_models[r["model"]] += 1
        c = 0.0
        for name, t, price in zip(classes, r["parts"], p):
            tok[name] += t
            cost[name] += t * price / 1e6
            c += t * price / 1e6
        r["cost"] = c
        m = by_model[r["model"]]
        m["requests"] += 1
        m["tokens"] += sum(r["parts"])
        m["cost"] += c
        scope_cost["subagents" if r["sub"] else "main"] += c
        effort[str(r["effort"] or "unset")] += 1
        days_active.add(r["ts"].astimezone().date().isoformat())
    total_cost = sum(cost.values())
    total_tok = sum(tok.values())
    out.update({
        "active_days": len(days_active),
        "first": reqs[0]["ts"].astimezone().date().isoformat(),
        "last": reqs[-1]["ts"].astimezone().date().isoformat(),
        "total_tokens": total_tok,
        "est_cost_usd": round(total_cost, 2),
        "est_cost_per_active_day": round(total_cost / max(1, len(days_active)), 2),
        "token_classes": {k: {"tokens": tok[k], "token_pct": pct(tok[k], total_tok),
                              "cost_pct": pct(cost[k], total_cost)} for k in classes},
        "models": {k: {"requests": v["requests"], "tokens": v["tokens"],
                       "cost_pct": pct(v["cost"], total_cost)}
                   for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])},
        "main_vs_subagents_cost_pct": {k: pct(v, total_cost) for k, v in scope_cost.items()},
        "effort_requests": dict(effort),
        "unknown_models_priced_as_" + FALLBACK_FAMILY: dict(unknown_models),
    })

    # --- main sessions: context growth, rebuilds, model switches
    sessions = defaultdict(list)
    for r in reqs:
        if not r["sub"]:
            sessions[r["session"]].append(r)
    peaks, starts, lengths = [], [], []
    rebuilds = Counter()
    rebuild_tokens = 0
    switches = 0
    big_cost = 0.0
    long_uncompacted = 0
    for sid, rs in sessions.items():
        ctx = [r["parts"][0] + r["parts"][1] + r["parts"][2] + r["parts"][3] for r in rs]
        peaks.append(max(ctx))
        starts.append(ctx[0])
        lengths.append(len(rs))
        if max(ctx) >= BIG_CONTEXT:
            big_cost += sum(r["cost"] for r in rs)
            if not compactions.get(sid):
                long_uncompacted += 1
        for i in range(1, len(rs)):
            if rs[i]["model"] != rs[i - 1]["model"]:
                switches += 1
            written = rs[i]["parts"][0] + rs[i]["parts"][1] + rs[i]["parts"][2]
            if ctx[i] >= REBUILD_MIN_CONTEXT and rs[i]["parts"][3] < 0.2 * ctx[i]:
                gap = (rs[i]["ts"] - rs[i - 1]["ts"]).total_seconds()
                if rs[i]["model"] != rs[i - 1]["model"]:
                    cause = "model_switch"
                elif gap > 3600:
                    cause = "idle_over_1h"
                elif gap > 300:
                    cause = "idle_5m_to_1h"
                else:
                    cause = "other_compaction_or_edit"
                rebuilds[cause] += 1
                rebuild_tokens += written
    main_cost = scope_cost["main"]
    out["sessions"] = {
        "count": len(sessions),
        "requests_median": median(lengths), "requests_p90": p90(lengths),
        "startup_context_median": median(starts),
        "peak_context_median": median(peaks), "peak_context_p90": p90(peaks),
        "sessions_over_100k": sum(1 for p in peaks if p >= BIG_CONTEXT),
        "over_100k_never_compacted": long_uncompacted,
        "cost_pct_in_sessions_over_100k": pct(big_cost, main_cost),
        "compactions": sum(compactions.values()),
        "model_switches_mid_session": switches,
        "cache_rebuilds": dict(rebuilds),
        "cache_rebuild_tokens_rewritten": rebuild_tokens,
    }

    # --- tools: how much output each tool put into context
    by_tool = defaultdict(lambda: {"calls": 0, "chars": 0})
    biggest = []
    reads = defaultdict(Counter)
    for tid, tu in tool_uses.items():
        chars = results.get(tid, 0)
        t = by_tool[tu["name"]]
        t["calls"] += 1
        t["chars"] += chars
        biggest.append((chars, tu["name"], tu["label"], "subagent" if tu["sub"] else "main"))
        if tu["file"]:
            reads[(tu["session"], tu["run"])][tu["file"]] += 1
    total_chars = sum(t["chars"] for t in by_tool.values())
    out["tools"] = [
        {"tool": name, "calls": t["calls"], "est_tokens": t["chars"] // 4,
         "pct_of_tool_output": pct(t["chars"], total_chars),
         "avg_tokens": (t["chars"] // 4) // max(1, t["calls"])}
        for name, t in sorted(by_tool.items(), key=lambda kv: -kv[1]["chars"])[:12]
    ]
    out["largest_results"] = [
        {"est_tokens": c // 4, "tool": n, "what": l, "where": w}
        for c, n, l, w in sorted(biggest, reverse=True)[:10] if c
    ]
    rereads = [(n, os.path.basename(f) if not redact else "") for per in reads.values()
               for f, n in per.items() if n >= 3]
    out["files_read_3plus_times_in_one_context"] = {
        "count": len(rereads),
        "top": [] if redact else [{"reads": n, "file": f} for n, f in sorted(rereads, reverse=True)[:5]],
    }

    # --- sub-agent runs
    runs = defaultdict(lambda: {"requests": 0, "cost": 0.0, "tools": 0, "models": Counter()})
    for r in reqs:
        if r["run"]:
            x = runs[r["run"]]
            x["requests"] += 1
            x["cost"] += r["cost"]
            x["models"][r["model"]] += 1
    for tu in tool_uses.values():
        if tu["run"] in runs:
            runs[tu["run"]]["tools"] += 1
    by_type = defaultdict(lambda: {"runs": 0, "cost": 0.0, "tiny_runs": 0, "tool_calls": [], "models": Counter()})
    for key, x in runs.items():
        a = by_type[(agent_meta.get(key) or {}).get("agentType") or "unknown"]
        a["runs"] += 1
        a["cost"] += x["cost"]
        a["tool_calls"].append(x["tools"])
        a["models"].update(x["models"])
        if x["tools"] <= TINY_RUN_TOOL_CALLS:
            a["tiny_runs"] += 1
    out["subagents"] = [
        {"agent": name, "runs": a["runs"], "cost_pct": pct(a["cost"], total_cost),
         "est_cost_per_run": round(a["cost"] / a["runs"], 2),
         "tool_calls_median": median(a["tool_calls"]), "tiny_runs": a["tiny_runs"],
         "models": dict(a["models"])}
        for name, a in sorted(by_type.items(), key=lambda kv: -kv[1]["cost"])
    ]
    spawn = Counter()
    for tu in tool_uses.values():
        if tu["name"] in ("Agent", "Task") and not tu["sub"]:
            spawn[(tu["agent_type"] or "default", tu["agent_model"] or "not set")] += 1
    out["spawns_from_main"] = [{"agent": a, "model_param": m, "count": n}
                               for (a, m), n in spawn.most_common(12)]
    return out


def config_summary(config_dir):
    info = {}
    def size(path):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            return {"lines": text.count("\n") + 1, "est_tokens": len(text) // 4}
        except OSError:
            return None
    info["global_claude_md"] = size(os.path.join(config_dir, "CLAUDE.md"))
    try:
        with open(os.path.join(config_dir, "settings.json"), encoding="utf-8") as fh:
            s = json.load(fh)
        info["settings"] = {k: s.get(k) for k in ("model", "effortLevel", "outputStyle") if k in s}
    except (OSError, ValueError):
        info["settings"] = None
    agents = []
    for path in sorted(glob.glob(os.path.join(config_dir, "agents", "**", "*.md"), recursive=True)):
        fields = {}
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if ":" in line and not line.startswith(" "):
                    k, v = line.split(":", 1)
                    fields[k.strip()] = v.strip()
        agents.append({"file": os.path.basename(path), "name": fields.get("name"),
                       "model": fields.get("model", "inherit"), "effort": fields.get("effort"),
                       "description_chars": len(fields.get("description", ""))})
    info["agents"] = agents
    info["skills_installed"] = len(glob.glob(os.path.join(config_dir, "skills", "*", "SKILL.md")))
    return info


HEADLINE = ("est_cost_per_active_day", "cache_read_cost_pct", "cache_write_cost_pct", "output_cost_pct",
            "subagent_cost_pct", "peak_context_median", "startup_context_median", "cache_rebuilds",
            "tiny_subagent_runs")


def headline(out):
    tc = out.get("token_classes", {})
    s = out.get("sessions", {})
    return {
        "est_cost_per_active_day": out.get("est_cost_per_active_day"),
        "cache_read_cost_pct": tc.get("cache_read", {}).get("cost_pct"),
        "cache_write_cost_pct": round(tc.get("cache_write_5m", {}).get("cost_pct", 0)
                                      + tc.get("cache_write_1h", {}).get("cost_pct", 0), 1),
        "output_cost_pct": tc.get("output", {}).get("cost_pct"),
        "subagent_cost_pct": out.get("main_vs_subagents_cost_pct", {}).get("subagents", 0),
        "peak_context_median": s.get("peak_context_median"),
        "startup_context_median": s.get("startup_context_median"),
        "cache_rebuilds": sum(s.get("cache_rebuilds", {}).values()),
        "tiny_subagent_runs": sum(a["tiny_runs"] for a in out.get("subagents", [])),
    }


def history(config_dir, out, save):
    path = os.path.join(config_dir, "token-audit", "history.jsonl")
    previous = None
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        previous = rows[-1] if rows else None
    except (OSError, ValueError):
        pass
    now = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "days": out["days"]}
    now.update(headline(out))
    if save:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(now) + "\n")
        except OSError as exc:
            print("note: could not save history: %s" % exc, file=sys.stderr)
    return now, previous


def n(x):
    return "{:,}".format(x) if isinstance(x, int) else str(x)


def render(out):
    L = []
    add = L.append
    add("# Claude Code token audit: last %d days" % out["days"])
    if not out.get("requests"):
        add("\nNo usage found in %d log files. Check --config-dir, or widen --days." % out["files_read"])
        return "\n".join(L)
    add("\n%s to %s, %d active days, %s API requests, %s tokens, about $%s at API list prices ($%s per active day)."
        % (out["first"], out["last"], out["active_days"], n(out["requests"]), n(out["total_tokens"]),
           out["est_cost_usd"], out["est_cost_per_active_day"]))
    add("Costs are API-price estimates used to weight token classes; plan quota weighting is not published.")
    add("\n## Where the tokens went\n| Class | Tokens | % of tokens | % of est. cost |\n|---|---|---|---|")
    for k, v in out["token_classes"].items():
        add("| %s | %s | %s | %s |" % (k, n(v["tokens"]), v["token_pct"], v["cost_pct"]))
    add("\n## Models\n| Model | Requests | Tokens | % of est. cost |\n|---|---|---|---|")
    for k, v in out["models"].items():
        add("| %s | %s | %s | %s |" % (k, n(v["requests"]), n(v["tokens"]), v["cost_pct"]))
    mv = out["main_vs_subagents_cost_pct"]
    add("\nMain session %s%% of cost, sub-agents %s%%. Effort by request: %s."
        % (mv.get("main", 0), mv.get("subagents", 0), out["effort_requests"]))
    s = out["sessions"]
    add("\n## Sessions (main conversations)")
    for k in ("count", "requests_median", "requests_p90", "startup_context_median", "peak_context_median",
              "peak_context_p90", "sessions_over_100k", "over_100k_never_compacted",
              "cost_pct_in_sessions_over_100k", "compactions", "model_switches_mid_session",
              "cache_rebuilds", "cache_rebuild_tokens_rewritten"):
        add("- %s: %s" % (k, n(s[k]) if isinstance(s[k], int) else s[k]))
    add("\n## Tool output entering context\n| Tool | Calls | Est. tokens | % of tool output | Avg tokens/call |\n|---|---|---|---|---|")
    for t in out["tools"]:
        add("| %s | %s | %s | %s | %s |" % (t["tool"], n(t["calls"]), n(t["est_tokens"]),
                                            t["pct_of_tool_output"], n(t["avg_tokens"])))
    if out["largest_results"]:
        add("\nLargest single results:")
        for r in out["largest_results"]:
            add("- %s tokens, %s (%s) %s" % (n(r["est_tokens"]), r["tool"], r["where"], r["what"]))
    rr = out["files_read_3plus_times_in_one_context"]
    add("\nFiles read 3+ times in one context: %d %s" % (rr["count"], rr["top"] if rr["count"] else ""))
    add("\n## Sub-agents")
    if out["subagents"]:
        add("| Agent | Runs | % of est. cost | Est. $/run | Median tool calls | Tiny runs | Models |\n|---|---|---|---|---|---|---|")
        for a in out["subagents"]:
            add("| %s | %d | %s | %s | %d | %d | %s |" % (a["agent"], a["runs"], a["cost_pct"],
                a["est_cost_per_run"], a["tool_calls_median"], a["tiny_runs"], ", ".join(a["models"])))
    else:
        add("No sub-agent runs in this window.")
    if out["spawns_from_main"]:
        add("\nSpawns from the main session (agent, model parameter, count): "
            + "; ".join("%s / %s / %d" % (x["agent"], x["model_param"], x["count"]) for x in out["spawns_from_main"]))
    c = out["config"]
    add("\n## Always-loaded config")
    add("- global CLAUDE.md: %s" % (c["global_claude_md"] or "none"))
    add("- settings: %s" % c["settings"])
    add("- skills installed: %s" % c["skills_installed"])
    for a in c["agents"]:
        add("- agent %s: model %s, effort %s, description %d chars" % (a["name"] or a["file"], a["model"],
                                                                     a["effort"], a["description_chars"]))
    prev = out.get("previous_run")
    if prev:
        add("\n## Change since the previous audit (%s, %s-day window)" % (prev.get("at"), prev.get("days")))
        now = out["headline"]
        for k in HEADLINE:
            add("- %s: %s -> %s" % (k, prev.get(k), now.get(k)))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=14, help="how many days back to read (default 14)")
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))
    ap.add_argument("--json", action="store_true", help="print JSON instead of the text report")
    ap.add_argument("--redact", action="store_true", help="leave commands and file names out of the report")
    ap.add_argument("--no-save", action="store_true", help="do not append this run to the history file")
    args = ap.parse_args()
    out = analyse(args.config_dir, args.days, args.redact)
    out["config"] = config_summary(args.config_dir)
    if out.get("requests"):
        out["headline"], out["previous_run"] = history(args.config_dir, out, not args.no_save)
    print(json.dumps(out, indent=1, default=str) if args.json else render(out))


if __name__ == "__main__":
    main()
