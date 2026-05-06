# Samu — Autonomous PM Agent

Slack bot that runs Simon's video production ops without a terminal. Proactively briefs him on editor workloads and client health, queues messages for one-tap approval, and answers ad-hoc questions on demand.

**Stack:** Slack Bolt · APScheduler · Anthropic API · Airtable · Railway

---

## How It Works

```
APScheduler cron  ──┐
Simon DMs bot     ──┤──▶  orchestrator.py  ──▶  skills/  ──▶  Claude decides
                  ──┘                                      ──▶  act / queue / notify Simon
```

**Action tiers:**

| Tier | What | Gate |
|---|---|---|
| 1 | DM Simon, update Airtable status | Autonomous |
| 2 | Message editor/client channels | Simon reacts ✅/❌ in Slack |
| 3 | Delete records, financial changes, scheduling | Manual only — never autonomous |

**Cron schedule (Singapore time):**

| Time | Trigger | What happens |
|---|---|---|
| 09:00 Mon–Fri | `morning_briefing` | Editor load + client health → DM Simon |
| 12:00 Mon–Fri | `midday_crosscheck` | Slack vs Airtable drift → DM Simon if issues |
| 17:00 Mon–Fri | `eod_checkout` | EOD summary → DM Simon → Simon ✅ → post to Samu channel |
| Every 2h | `urgent_watch` | Unanswered client messages → DM Simon if new urgency |
| 09:00 Monday | `weekly_summary` | 7-day view of all metrics → DM Simon |

---

## Directory Structure

```
execution/
  lib/
    airtable.py        # Airtable client: auth, pagination, linked-record resolution, write log
    slack.py           # Slack client: read, send, DM, channel cache
    state.py           # Persistent state: escalations, pending approvals, actioned items
  skills/
    editors.py         # Editor workload, deadlines, silent editors
    clients.py         # Client sentiment, unanswered messages, churn risk
    crosscheck.py      # Slack vs Airtable drift detection
    checkout.py        # EOD summary generation
    notify_simon.py    # DM Simon (Tier 1)
    update_status.py   # Update Airtable status (Tier 1)
    draft_message.py   # Queue editor/client message for Simon approval (Tier 2)
  orchestrator.py      # Brain: receives trigger, runs skills, calls Claude, executes actions
  bot.py               # Slack Bolt app + APScheduler + Flask adapter
  constants.py         # IDs, field names, status transitions
  tools/
    test_e2e.py        # Manual trigger runner (dev use)
    rollback.py        # Airtable write rollback CLI (dev use)
    utils.py           # CLI dev utilities
directives/            # SOPs, PM skills bible, Airtable operations docs
config/
  simon/
    CLAUDE.md          # Agent config for Simon's ops context
.tmp/
  agent_state.json     # Runtime state (gitignored)
  write_log.jsonl      # Airtable write audit log (gitignored)
```

---

## Local Development

### Install

```bash
pip install -r requirements.txt
```

### Configure

Copy `.env.template` to `.env` and fill in all values.

### Test a trigger (dry run — no writes, no Slack sends)

```bash
AGENT_WRITES_ENABLED=false python execution/tools/test_e2e.py morning_briefing --dry-run
```

### Test a trigger (live — writes to real Airtable/Slack)

```bash
python execution/tools/test_e2e.py morning_briefing
```

Available triggers: `morning_briefing`, `midday_crosscheck`, `eod_checkout`, `urgent_watch`, `weekly_summary`

---

## Railway Deploy

### Step 1: Create Slack App

Go to **api.slack.com/apps** → "Create New App" → "From scratch" → name it → select your workspace.

**OAuth & Permissions → Bot Token Scopes — add:**
```
chat:write
reactions:read
channels:history
groups:history
im:history
im:write
channels:read
groups:read
```

**Event Subscriptions** — enable, then subscribe to bot events:
```
message.im
reaction_added
```

Leave the Request URL blank for now (fill in after deploy).

**Install to workspace** → copy:
- `SLACK_BOT_TOKEN` (`xoxb-...`) from OAuth & Permissions
- `SLACK_SIGNING_SECRET` from Basic Information

### Step 2: Get Channel IDs

**Simon's DM channel:** Have Simon DM the bot once. Open that DM in Slack web app — the URL contains `D...` (e.g. `app.slack.com/client/T.../D...`). That `D...` is `SLACK_SIMON_DM_CHANNEL`.

**Samu channel:** Open the channel where EOD posts should go. Grab the `C...` from the URL. That's `SLACK_SAMU_CHANNEL`.

### Step 3: Create Railway Project

Go to **railway.app** → "New Project" → "Deploy from GitHub repo" → select this repo.

Railway detects `Procfile` and `railway.json` automatically.

### Step 4: Set Environment Variables

In Railway → project → "Variables" tab, add:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_SIMON_DM_CHANNEL=D...
SLACK_SAMU_CHANNEL=C...
SLACK_USER_TOKEN=xoxp-...
AGENT_WRITES_ENABLED=false
ANTHROPIC_API_KEY=sk-ant-...
AIRTABLE_API_KEY=pat...
AIRTABLE_BASE_ID=app...
```

Keep `AGENT_WRITES_ENABLED=false` until smoke tests pass.

### Step 5: Deploy + Wire Slack Events

Railway auto-deploys on push. Once live, grab the public URL (e.g. `https://samu-pm-agent.up.railway.app`).

Back in Slack app settings → **Event Subscriptions** → paste:
```
https://<your-railway-url>/slack/events
```

Slack sends a challenge — bot handles it automatically, shows ✅ verified. Save. Re-install app to workspace if prompted.

### Step 6: Smoke Test

Fire each trigger and verify DMs land in Simon's Slack:

```bash
python execution/tools/test_e2e.py morning_briefing
python execution/tools/test_e2e.py midday_crosscheck
python execution/tools/test_e2e.py eod_checkout
```

Test approval flow: bot DMs Simon a draft with ✅/❌. React and verify it sends or cancels.

Once validated, flip `AGENT_WRITES_ENABLED=true` in Railway variables.

---

## Environment Variables Reference

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-...`) — write permissions |
| `SLACK_SIGNING_SECRET` | Slack app signing secret — verifies incoming events |
| `SLACK_USER_TOKEN` | User token (`xoxp-...`) — read private channels |
| `SLACK_SIMON_DM_CHANNEL` | Simon's DM channel ID with bot (`D...`) |
| `SLACK_SAMU_CHANNEL` | Channel ID for EOD posts (`C...`) |
| `AGENT_WRITES_ENABLED` | `true`/`false` — global write kill switch |
| `ANTHROPIC_API_KEY` | Claude API key |
| `AIRTABLE_API_KEY` | Airtable personal access token (`pat...`) |
| `AIRTABLE_BASE_ID` | Airtable base ID (`app...`) |
| `PORT` | Set automatically by Railway (default `3000`) |

---

## Airtable Safety

Every write is logged and reversible.

**Write log** — `lib/airtable.py` records before/after values for every `update_record()` call to `.tmp/write_log.jsonl`. Append-only, never deleted by the agent.

**Rollback CLI** — standalone tool, never called autonomously:

```bash
# Preview last 5 writes (dry run)
python execution/tools/rollback.py --last 5

# Roll back last 5 writes
python execution/tools/rollback.py --last 5 --apply

# Roll back all writes since a timestamp
python execution/tools/rollback.py --since "2025-05-05T09:00:00Z" --apply
```

**Additional safeguards:**
- Field whitelist — `update_record()` only allows `["Editing Status", "Assigned Editor", "Thumbnail Status"]`
- Status transition guard — validates against `VALID_TRANSITIONS` before writing
- `AGENT_WRITES_ENABLED=false` — disables all writes globally, Simon is notified

---

## Skill Interface

All skills return this shape — orchestrator depends on it:

```python
def run(**kwargs) -> dict:
    return {
        "ok": bool,
        "data": {...},          # skill-specific structured output
        "summary": str,         # 1-2 sentence plain-text summary
        "actions_needed": [],   # recommended actions with priority
    }
```
