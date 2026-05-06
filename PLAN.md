# Overhaul Plan: Glorified Macro → Fully Autonomous Agent

## Vision

Replace Simon's manual Claude Code terminal session with a **deployed Slack bot** that:
- Proactively DMs Simon on a cron schedule (briefings, crosschecks, EOD)
- Acts autonomously on Tier 1 actions (update Airtable, DM Simon)
- Queues Tier 2 actions (editor/client messages) for Simon's one-tap approval in Slack
- Answers Simon's ad-hoc questions when he DMs the bot
- Never requires Simon to open a terminal

Architecture: **Slack Bolt bot + APScheduler + Anthropic API + Railway deployment**

Claude Code remains the dev/iteration environment (your terminal). The live agent runs on Railway.

---

## Current State Inventory

| File | LOC | Status |
|---|---|---|
| `execution/editor_task_report.py` | 1436 | Monolithic — refactor into skill |
| `execution/client_status_report.py` | 1053 | Monolithic — refactor into skill |
| `execution/slack_airtable_crosscheck.py` | 1305 | Monolithic — refactor into skill |
| `execution/checkout_message.py` | 681 | Monolithic — refactor into skill |
| `execution/airtable_read.py` | 129 | Keep as CLI primitive |
| `execution/slack_read_channel.py` | 228 | Keep as CLI primitive |
| `execution/airtable_update.py` | 95 | Dead (Simon read-only) — unblock for agent |
| `execution/slack_send_message.py` | 143 | Dead (Simon read-only) — unblock for agent |
| `execution/tools/utils.py` | 213 | Good start — expand into full lib |
| `execution/constants.py` | 171 | Keep — move to lib |

---

## Target Directory Structure

```
execution/
  lib/
    airtable.py       # Airtable client: auth, pagination, linked-record resolution
    slack.py          # Slack client: read, send, list, channel cache
    state.py          # Persistent agent state: escalations, approvals, actioned items
  skills/
    editors.py        # Editor workload, deadlines, silent editors
    clients.py        # Client sentiment, unanswered messages, churn risk
    crosscheck.py     # Slack vs Airtable drift detection
    checkout.py       # EOD summary generation
    notify_simon.py   # Slack DM to Simon (Tier 1 — always free)
    update_status.py  # Update Airtable status (Tier 1 — for confirmed moves)
    draft_message.py  # Draft editor/client message, queue for approval (Tier 2)
  orchestrator.py     # Brain: receives trigger, runs skills, triages, acts
  bot.py              # Slack Bolt app + APScheduler cron
  constants.py        # Unchanged
  tools/
    utils.py          # Unchanged (CLI dev use)
    [primitives]      # airtable_read.py, slack_read_channel.py, etc. — unchanged
.tmp/
  agent_state.json    # Runtime state (gitignored)
config/
  simon/
    CLAUDE.md         # Updated for new architecture
```

---

## Action Tiers

| Tier | Actions | Gate |
|---|---|---|
| **1 — Agent acts freely** | DM Simon, update Airtable status when Slack confirms done | None |
| **2 — Simon approves** | Send message to editor channel, send message to client channel | Simon reacts ✅/❌ in Slack |
| **3 — Never autonomous** | Delete records, financial changes, YouTube scheduling | Manual only |

---

## Skill Interface Contract

All skills (Wave 1) must honor this exact interface. Orchestrator (Wave 2) depends on it.

```python
def run(**kwargs) -> dict:
    # Returns:
    # {
    #   "ok": bool,
    #   "data": {...},       # skill-specific structured output
    #   "summary": str,      # 1-2 sentence plain-text summary for orchestrator
    #   "actions_needed": [] # list of recommended actions with priority
    # }
```

---

## Build Waves

M1 is complete. M2 and M3 both depend only on M1 — they are independent of each other. Individual files within each wave are independent. Wave 1 collapses M2+M3+deployment scaffolding into a single parallel pass.

```
Wave 1 (parallel — 9 agents)         Wave 2 (1 agent)      Wave 3 (parallel — 2 agents)
──────────────────────────────        ────────────────       ──────────────────────────────
M2a: skills/editors.py                M4: orchestrator.py   M5-wire: bot.py (full handlers)
M2b: skills/clients.py                                       M6-final: E2E + Railway deploy
M2c: skills/crosscheck.py
M2d: skills/checkout.py
M3a: skills/notify_simon.py
M3b: skills/update_status.py
M3c: skills/draft_message.py
M5-stub: bot.py shell + stubs
M6-pre: Procfile, railway.json,
        .env.template
```

**M4 is the unavoidable bottleneck** — it integrates all skills and cannot begin until Wave 1 is complete. M5-wire and M6-final can run in parallel once M4 exists.

---

## Milestones

### M1 — Shared Library Layer ✅
**Goal:** Eliminate duplicated API code across all scripts. Every skill imports from `lib/` — nothing re-implements auth, pagination, or linked-record resolution.

**Why first:** All subsequent milestones depend on this. The 4 monolithic scripts each reimplement ~200 LOC of Airtable/Slack boilerplate.

**Deliverables:**

`execution/lib/airtable.py`
- AirtableClient class: auth, paginated fetch, rate limiting (5 req/sec)
- `fetch_table(table, fields, filter_formula)` → list of records
- `update_record(table, record_id, fields)` → used by update_status skill
- `build_linked_record_filter(record_id, field)` → FIND formula helper
- Absorbs and extends `execution/tools/utils.py` (get_client_map, get_editor_map, format_video_ref)

`execution/lib/slack.py`
- SlackClient class: read token + bot token (separate — read vs write)
- `read_channel(channel, hours, limit)` → list of messages
- `list_channels(filter_regex)` → channel list
- `send_dm(user_id, text, blocks)` → DM to Simon or others
- `send_channel_message(channel_id, text)` → post to channel
- Channel name→ID cache (avoid repeated list_channels calls)

`execution/lib/state.py`
- `load_state()` / `save_state()` → read/write `.tmp/agent_state.json`
- Schema:
  ```json
  {
    "last_escalated": {"EditorName": "ISO timestamp"},
    "pending_approvals": [{"id": "...", "action": "...", "draft": "...", "expires": "..."}],
    "actioned_slack_ts": ["ts1", "ts2"]
  }
  ```
- `was_recently_escalated(name, hours)` → bool
- `mark_escalated(name)`
- `queue_approval(action_dict)` / `resolve_approval(id, approved)`

**Files created:** `execution/lib/__init__.py`, `airtable.py`, `slack.py`, `state.py`
**Files unchanged:** all existing scripts (lib is additive)

---

### Wave 1 — Parallel (M2 + M3 + scaffolding)

All Wave 1 work is independent. Spawn as parallel agents. All skill files must honor the interface contract above.

---

#### M2a — `execution/skills/editors.py` ⬜
**Goal:** Decompose editor workload logic from `editor_task_report.py` into lean composable skill.

- `run(editor=None, hours=48)` → dict with editor workload, overdue videos, silent editors
- Extracts business logic from `editor_task_report.py`
- Returns structured data (no formatting — orchestrator decides how to present)
- Imports from `lib/airtable.py`, `lib/slack.py`

**Files created:** `execution/skills/__init__.py`, `execution/skills/editors.py`
**Files kept:** `editor_task_report.py` remains as fallback CLI tool until M6 validation

---

#### M2b — `execution/skills/clients.py` ⬜
**Goal:** Decompose client sentiment logic from `client_status_report.py`.

- `run(client=None, hours=48)` → dict with sentiment, unanswered messages, risk flags
- Extracts from `client_status_report.py`
- Imports from `lib/airtable.py`, `lib/slack.py`

**Files created:** `execution/skills/clients.py`
**Files kept:** `client_status_report.py` remains as fallback CLI tool until M6 validation

---

#### M2c — `execution/skills/crosscheck.py` ⬜
**Goal:** Decompose drift detection logic from `slack_airtable_crosscheck.py`.

- `run(checks=None, hours=48)` → dict with Slack/Airtable discrepancies per check type
- `checks` defaults to full suite; can run single check
- Extracts from `slack_airtable_crosscheck.py`
- Imports from `lib/airtable.py`, `lib/slack.py`

**Files created:** `execution/skills/crosscheck.py`
**Files kept:** `slack_airtable_crosscheck.py` remains as fallback CLI tool until M6 validation

---

#### M2d — `execution/skills/checkout.py` ⬜
**Goal:** Decompose EOD summary logic from `checkout_message.py`.

- `run(days=1)` → dict with EOD summary data
- Extracts from `checkout_message.py`
- Imports from `lib/airtable.py`, `lib/slack.py`

**Files created:** `execution/skills/checkout.py`
**Files kept:** `checkout_message.py` remains as fallback CLI tool until M6 validation

---

#### M3a — `execution/skills/notify_simon.py` *(Tier 1)* ⬜
**Goal:** Give the agent ability to DM Simon directly.

- `run(message, blocks=None, urgent=False)` → sends Slack DM to Simon
- Used by orchestrator for briefings, escalations, approval requests
- Formats message with urgency header if `urgent=True`
- Imports from `lib/slack.py`

**Files created:** `execution/skills/notify_simon.py`

---

#### M3b — `execution/skills/update_status.py` *(Tier 1)* ⬜
**Goal:** Give the agent ability to update Airtable status.

- `run(video_record_id, new_status, reason)` → updates Airtable Editing Status
- Only called when orchestrator has high confidence (e.g., editor said "done, submitting for QC" in Slack)
- Logs to state as actioned item to avoid double-updating
- Imports from `lib/airtable.py`, `lib/state.py`

**Files created:** `execution/skills/update_status.py`

---

#### M3c — `execution/skills/draft_message.py` *(Tier 2)* ⬜
**Goal:** Give the agent ability to queue editor/client messages for Simon's approval.

- `run(channel_type, recipient, context, draft_text)` → queues approval request
- Sends Simon a formatted DM: "I want to send this to [editor/client]. React ✅ to send, ❌ to cancel."
- Stores pending approval in `state.json` with 4h expiry
- Bot (M5) listens for Simon's reaction → executes or cancels
- Imports from `lib/slack.py`, `lib/state.py`

**Files created:** `execution/skills/draft_message.py`

---

#### M5-stub — `execution/bot.py` shell ⬜
**Goal:** Scaffold Slack Bolt app with handler stubs and APScheduler wired. Does not need orchestrator yet — stubs return placeholder strings. Wave 3 (M5-wire) fills in real orchestrator calls.

- Slack Bolt app initialized with env vars
- APScheduler set up with correct Singapore-timezone cron schedule (see M6 for schedule table)
- Three handler stubs:
  1. DM handler — receives Simon message, returns `"[stub] received: {text}"`
  2. Reaction handler — detects ✅/❌ on bot messages, returns `"[stub] reaction received"`
  3. Cron handlers — one per trigger type, each returns `"[stub] {trigger} fired"`
- Graceful startup/shutdown
- Env var loading with validation (fail fast if required vars missing)

**Bot token scopes required:**
- `chat:write` — send DMs and channel messages
- `reactions:read` — detect Simon's approval reactions
- `channels:history`, `groups:history` — read channels (existing)
- `im:history` — read DMs from Simon

**New env vars:** `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_SIMON_DM_CHANNEL`

**Files created:** `execution/bot.py`

---

#### M6-pre — Deployment config ⬜
**Goal:** All deployment config files ready before Railway deploy in Wave 3.

- `Procfile`: `web: python execution/bot.py`
- `railway.json`: updated with new env var names
- `.env.template`: updated with all required vars (see full list below)

**Full env var list:**
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_SIMON_DM_CHANNEL=...   # Simon's DM channel ID with the bot
SLACK_SAMU_CHANNEL=...       # Channel where EOD checkout gets posted (after Simon approves)
AGENT_WRITES_ENABLED=true   # Set false to globally disable all writes (read-only mode)
ANTHROPIC_API_KEY=...        # Already exists
AIRTABLE_API_KEY=...         # Already exists
AIRTABLE_BASE_ID=...         # Already exists
```

**Files created/updated:** `Procfile`, `railway.json`, `.env.template`

---

### Wave 2 — M4: Orchestrator

Begins only when all Wave 1 agents complete.

---

#### M4 — `execution/orchestrator.py` ⬜
**Goal:** Build the brain. Receives a trigger (cron type or Simon's message), decides which skills to run, calls Claude API to triage results, executes appropriate actions.

**Depends on:** M2a, M2b, M2c, M2d, M3a, M3b, M3c (all Wave 1 skills)

`execution/orchestrator.py`

Core loop:
```python
def run(trigger: str, context: str = "") -> str:
    state = load_state()
    results = run_relevant_skills(trigger, context)
    decision = call_claude(trigger, results, state)  # Claude decides what to do
    execute_actions(decision, state)
    save_state(state)
    return decision["response_to_simon"]
```

Trigger types:
- `"morning_briefing"` → runs editors + clients skills
- `"midday_crosscheck"` → runs crosscheck skill
- `"eod_checkout"` → runs checkout skill → auto-posts to Samu
- `"urgent_watch"` → runs clients skill focused on unanswered messages
- `"weekly_summary"` → runs all skills with 7-day window
- `"simon_query"` → Simon DM'd the bot; `context` = his message

Claude's role in orchestration:
- Receives skill output + current state (what was already escalated)
- Decides: what's new/urgent, what was already handled, what actions to take
- Returns structured decision: `{response: str, tier1_actions: [], tier2_actions: []}`
- Prevents duplicate escalations (checks `state.last_escalated`)

**Model:** `claude-sonnet-4-6` for orchestration (fast, cost-efficient for frequent cron runs)

**Files created:** `execution/orchestrator.py`

---

### Wave 3 — Parallel (M5-wire + M6-final)

Both begin once M4 exists.

---

#### M5-wire — `execution/bot.py` (full handlers) ⬜
**Goal:** Replace bot.py stubs with real orchestrator calls.

**Depends on:** M4, M5-stub

1. **DM handler** — Simon messages the bot
   ```
   Simon: "what's going on with editors?"
   → orchestrator.run("simon_query", context="what's going on with editors?")
   → response posted back to Simon's DM
   ```

2. **Reaction handler** — Simon reacts to approval requests
   ```
   Simon reacts ✅ on draft message
   → state.resolve_approval(message_ts, approved=True)
   → slack.send_channel_message(channel, draft)
   → confirm to Simon: "Sent to Megh."
   ```

3. **APScheduler cron** — autonomous triggers wired to `orchestrator.run(trigger)`

**Files modified:** `execution/bot.py`

---

#### M6-final — E2E validation + Railway deploy ⬜
**Goal:** Wire up cron schedule, validate end-to-end, deploy to Railway. Agent runs 24/7 without Simon or Joshua touching it.

**Depends on:** M4, M5-wire, M6-pre

**Cron schedule (Singapore time):**

| Time | Trigger | What happens |
|---|---|---|
| 09:00 Mon–Fri | `morning_briefing` | Editor load + client health → DM Simon |
| 12:00 Mon–Fri | `midday_crosscheck` | Slack vs Airtable drift → DM Simon if issues found |
| 17:00 Mon–Fri | `eod_checkout` | EOD summary generated → DM Simon for review → Simon reacts ✅ → posted to Samu channel |
| Every 2h | `urgent_watch` | Check for unanswered client messages → DM Simon only if new urgency |
| 09:00 Monday | `weekly_summary` | 7-day view of all metrics → DM Simon |

**Deliverables:**
- APScheduler schedule verified against above table
- E2E test: each trigger type manually fired, output verified in Slack
- Railway deploy confirmed live

**Files modified:** `execution/bot.py`, `Procfile`, `railway.json`, `.env.template`

---

## Build Order Summary

```
M1 ✅ (lib — done)
  │
  ├── Wave 1 (parallel — all independent, all depend only on M1)
  │     M2a: skills/editors.py
  │     M2b: skills/clients.py
  │     M2c: skills/crosscheck.py
  │     M2d: skills/checkout.py
  │     M3a: skills/notify_simon.py
  │     M3b: skills/update_status.py
  │     M3c: skills/draft_message.py
  │     M5-stub: bot.py shell
  │     M6-pre: Procfile, railway.json, .env.template
  │
  └── Wave 2 (blocks on all Wave 1)
        M4: orchestrator.py
          │
          └── Wave 3 (parallel — both depend on M4)
                M5-wire: bot.py full handlers
                M6-final: E2E + Railway deploy
```

Each wave is independently testable before the next begins.

---

## What Does NOT Change

- `directives/` — all SOP docs, pm_skills_bible, airtable_operations — kept as-is, loaded into orchestrator's Claude context
- `execution/constants.py` — unchanged, imported by lib
- `execution/tools/` primitives — kept for Claude Code dev use
- `config/simon/CLAUDE.md` — updated to reflect new architecture but Simon still has a CLAUDE.md for ad-hoc dev queries if needed

---

## Resolved Questions

1. **Slack app** — create new from scratch
2. **Airtable write access** — confirmed. Safety fallback required — see below.
3. **Railway** — fresh project
4. **Approval UX** — reactions (✅/❌)
5. **EOD checkout** — DM Simon first for review, then Simon reacts ✅ to post to Samu channel

---

## Airtable Safety Fallback

Every write the agent makes must be reversible. Implement a write log + rollback tool.

### Write Log

Every `update_record()` call in `lib/airtable.py` must:
1. Read the current field value **before** writing
2. Append an entry to `.tmp/write_log.jsonl`:

```json
{
  "ts": "2025-05-05T09:00:00Z",
  "table": "Videos",
  "record_id": "recXXX",
  "field": "Editing Status",
  "old_value": "50 - Editor Confirmed",
  "new_value": "60 - Submitted for QC",
  "trigger": "morning_briefing",
  "reason": "Editor said 'done, submitting for QC' in #sakib-editing"
}
```

Log is append-only, never deleted by the agent. Gitignored (lives in `.tmp/`).

### Rollback Tool

`execution/tools/rollback.py` — standalone CLI, never called by the agent autonomously.

```bash
# Preview what would be rolled back (dry run, default)
python execution/tools/rollback.py --last 5

# Roll back last N writes
python execution/tools/rollback.py --last 5 --apply

# Roll back all writes since a timestamp
python execution/tools/rollback.py --since "2025-05-05T09:00:00Z" --apply

# Roll back writes from a specific trigger run
python execution/tools/rollback.py --trigger "morning_briefing_2025-05-05T09:00" --apply
```

Rollback reads the log in reverse order, re-applies old values via the Airtable API, and marks each entry as `"rolled_back": true`. Safe to run multiple times (idempotent on already-rolled-back entries).

### Additional Safeguards in `lib/airtable.py`

- **Dry-run mode** — `update_record(..., dry_run=True)` logs the intended write but does not execute. Orchestrator uses this in dev/staging.
- **Field whitelist** — `update_record()` only allows writes to explicitly whitelisted fields: `["Editing Status", "Assigned Editor", "Thumbnail Status"]`. Any other field raises an exception before the API call.
- **Status transition guard** — before writing a new `Editing Status`, validate it against `VALID_TRANSITIONS` in `constants.py`. Reject invalid moves (e.g., jumping from 41 directly to 80).

### `.env` Flag

```
AGENT_WRITES_ENABLED=true   # set to false to globally disable all writes (read-only mode)
```

Orchestrator checks this flag at startup. If false, all Tier 1 write actions are skipped and Simon is notified the agent is in read-only mode.
