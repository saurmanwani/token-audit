# token-audit

A Claude Code skill that shows where your tokens actually went, then tunes your config based on the numbers.

Most token-saving advice is generic. This skill reads your own Claude Code session logs for the last two weeks, reports what your usage looks like, and proposes specific changes to your global `CLAUDE.md`, sub-agent definitions and settings. It asks before changing anything, and a later run shows whether the changes made a difference.

## What it reports

- Token classes (input, cache writes, cache reads, output), by tokens and by estimated cost
- Models used, and main session versus sub-agents
- Session length, context growth, and sessions that passed 100k tokens without compacting
- Prompt-cache rebuilds and their likely cause (idle time, model switch, compaction)
- Which tools put the most output into context, and the largest single results
- Sub-agent runs: cost per run, model, and runs too small to have been worth spawning
- The size of your always-loaded config
- The change in headline numbers since the previous audit

## Install

```bash
git clone https://github.com/saurmanwani/token-audit.git
```

```bash
cp -r token-audit/skills/token-audit ~/.claude/skills/
```

Requires Python 3.8 or later. No packages to install.

## Use

In Claude Code, type `/token-audit`, or ask "where did my tokens go?" or "audit my token usage and tune my CLAUDE.md".

The analysis script also works by itself:

```bash
python3 ~/.claude/skills/token-audit/scripts/analyze_usage.py --days 14
```

To try it without real usage data, generate a demo config (`long-sessions`, `thin-data` or `wasteful-agents`) and point the script at it:

```bash
python3 ~/.claude/skills/token-audit/scripts/make_demo_logs.py wasteful-agents /tmp/demo-config
```

```bash
python3 ~/.claude/skills/token-audit/scripts/analyze_usage.py --config-dir /tmp/demo-config
```

| Option | Effect |
|---|---|
| `--days N` | Window to analyse (default 14) |
| `--json` | JSON output |
| `--redact` | Leave commands and file names out of the report |
| `--no-save` | Do not record this run in the history file |
| `--config-dir PATH` | Config directory, if not `~/.claude` (`CLAUDE_CONFIG_DIR` is honoured) |

## Privacy

The script runs locally and uses only the Python standard library. It reads `~/.claude/projects/**/*.jsonl` and your config files, and writes one line per run to `~/.claude/token-audit/history.jsonl`. It makes no network calls. The report it prints is read by Claude in your session, like any other tool output; use `--redact` if you do not want commands or file names in it.

## Limits

- Costs are estimates at API list prices, used to rank token classes against each other. Subscription plans do not publish how they weight models against plan limits.
- The session log format is not a documented interface and can change between Claude Code versions. The script skips anything it does not recognise, so a format change shows up as missing numbers rather than a crash.
- It measures where tokens went, not whether the work was worth it.
- Claude Code deletes old logs after its cleanup period (30 days by default), so long windows may come back short.
- A before/after comparison is not a controlled experiment; your work differs from week to week.

## Layout

```
skills/token-audit/
├── SKILL.md                  instructions Claude follows
├── scripts/analyze_usage.py  the log analysis
└── references/playbook.md    how report signals map to config changes
```

## Licence

MIT. See [LICENSE](LICENSE).
