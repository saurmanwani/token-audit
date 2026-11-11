# Playbook: from report signals to changes

Each entry gives the signal in the report, what it means, and what to do. Thresholds are rules of thumb; judge them against the rest of the report.

Contents: context size and session length, cache rebuilds, startup context, tool output, sub-agents, model and effort mix, output share, limits of this audit.

## Context size and session length

**Signal:** `cache_read` is most of the tokens; `peak_context_median` or `peak_context_p90` is high (over about 100k); `over_100k_never_compacted` is above zero; `cost_pct_in_sessions_over_100k` is large.

**Meaning:** Every request re-reads the whole conversation. A session that grows to 300k tokens pays for 300k on each later turn, so a few long sessions can account for most of the bill.

**Change:**
- Habit: `/clear` when moving to an unrelated task; `/compact` at a natural break in a long one.
- CLAUDE.md: a line asking Claude to point out, once, when a finished task makes this a good moment for `/clear`; a "Compact instructions" section saying what to keep and what to drop.
- If long sessions are long because of noisy work (test runs, web research, log reading), move that work to a sub-agent so only the result enters the main context.

## Cache rebuilds

**Signal:** `cache_rebuilds` has entries; `cache_rebuild_tokens_rewritten` is a visible share of the cache-write tokens.

**Meaning:** The prompt cache expired or was invalidated and the whole context was written again at the cache-write price, which is the most expensive way to send input.
- `idle_over_1h` / `idle_5m_to_1h`: the user came back to a session after the cache lifetime had passed.
- `model_switch`: the model changed mid-session; each model has its own cache.
- `other_compaction_or_edit`: usually a compaction (expected, and it shrinks the context) or something that changed the start of the prompt.

**Change:**
- Idle rebuilds: a habit, not config. Before stepping away from a large session, `/compact` it, or start a fresh session on return instead of resuming.
- Model switches: choose the model at session start. `opusplan` switches models on every plan-mode toggle; if `model_switches_mid_session` is high and settings show `opusplan`, suggest a single model.

## Startup context

**Signal:** `startup_context_median` is high, or `global_claude_md` is over about 200 lines or 2,000 tokens.

**Meaning:** This is the fixed overhead paid before any work: system prompt, tool definitions, MCP servers, CLAUDE.md, skill descriptions, and the first message. The script cannot split it; `/context` in a session can.

**Change:**
- Ask the user to run `/context` once and look at the MCP and memory lines.
- Disable MCP servers that are not used (`/mcp`).
- Shrink CLAUDE.md: delete what Claude does anyway, move task-specific workflows into skills (only a skill's description is always loaded).

## Tool output

**Signal:** One tool dominates `Tool output entering context`; `avg_tokens` per call is high; `Largest single results` shows multi-thousand-token results in the main session; `files_read_3plus_times_in_one_context` is above zero.

**Meaning:** A tool result is paid for once when it arrives and again on every later turn of that session. Results marked `subagent` are less harmful, since that context is thrown away.

**Change:**
- Bash heavy: a CLAUDE.md rule to use quiet flags and pipe long output through `tail`, `head` or `grep`; for one recurring noisy command, name it in the rule. A PreToolUse hook can enforce it if rules are ignored.
- WebFetch or WebSearch heavy in the main session: route web lookups to a cheap sub-agent that returns an extract.
- Read heavy or repeated reads: a rule to search first, read line ranges of large files, and not re-read unchanged files.
- MCP tool heavy: check whether that server returns more than is needed, or whether a CLI would be leaner.

## Sub-agents

**Signal and change, case by case:**
- **`tiny_runs` is a large share of an agent's runs.** Spawning cost more than doing the job inline. Tighten that agent's `description` with an explicit "not for ..." clause, and add a CLAUDE.md line to do small jobs directly.
- **A mechanical agent runs on an expensive model** (check `Models` per agent). Set `model: sonnet` or `haiku` and `effort: low` in its frontmatter. Built-in `general-purpose` and `Explore` agents inherit or use the main model, so define named agents for cheap work and tell CLAUDE.md to prefer them.
- **`spawns_from_main` shows `default` or `general-purpose` with model `not set`.** Those inherit the main model. Same fix as above.
- **A defined agent never appears in the report.** Its description is not triggering. Make the description say concretely when to use it, or delete the agent.
- **Sub-agents are a large share of cost (over about a third) without a matching drop in main-session context.** Probably over-delegation. Narrow the delegation rules.
- **No sub-agents at all, with large tool output in the main session.** Under-delegation. Add one or two narrowly scoped agents for the noisy work.

When editing descriptions, say what the agent is for, what it is not for, and what it returns. The main model picks agents from the description alone.

## Model and effort mix

**Signal:** `Models` shows a top-tier model taking a large cost share; `effort_requests` shows many `high`, `xhigh` or `max` requests.

**Meaning:** Higher tiers and higher effort cost more per request. Whether that was worth it depends on the work, which the report cannot judge, so ask before changing.

**Change:** A default model and effort in settings that fit everyday work, with the top tier reserved for a named sub-agent or for sessions started on it deliberately.

## Output share

**Signal:** `output` cost share.

**Meaning and change:**
- Under about 10%: terse-reply tricks will not move the bill. Say so, and do not add output-style rules.
- Over about 25%: output matters here. Useful rules are "lead with the answer, no recap of visible diffs", "do not write summary or plan files unless asked", and writing less code (reuse before writing new).

## Limits of this audit

Say these when they bear on a finding.
- Costs are API-price estimates. Plan quota weighting between models is not published.
- The report shows where tokens went, not whether the work was worth it. It cannot tell whether a delegation or a high-effort turn produced a better result.
- Startup context is a single number; only `/context` breaks it down.
- The log format is undocumented and changes between versions. If the report looks empty or wrong on a new version, say that rather than guessing.
- Old logs are deleted after the cleanup period, so the window may be shorter than requested.
- A before/after comparison is not a controlled test: the work itself differs from week to week.
