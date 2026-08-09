# Slack Bridge

Connect Frappe/ERPNext with Slack — in both directions, entirely by configuration.

Frappe ships a Slack option on Notifications, but it is a legacy incoming webhook: plain
text, one fixed channel, no buttons, no threads, no retries. Slack Bridge replaces that
with a real connector: rich Block Kit messages driven by document events, buttons that
change documents, slash commands that open modal forms, and link previews for your ERP
URLs — all configured in the Desk, no code required.

## What it does

**Notifications out.** A *Slack Notification Rule* watches any doctype and any event
(New, Save, Submit, Cancel, Delete, Value Change, Days/Minutes Before or After, Method,
Custom). Conditions are Python expressions evaluated in Frappe's sandbox; messages are
Jinja templates in either simple markdown or full Block Kit. Recipients resolve three
ways — a fixed channel, a **field on the document** (`owner`, `sales_manager`, …), or a
Jinja expression — so one rule covers "tell the owner and the #sales channel".

**Daily digests.** A *Daily Digest* rule DMs each user one morning summary of the
documents grouped on them — "your 4 open ToDos", "your overdue invoices" — grouped by
any Link-to-User field (e.g. `allocated_to`), filtered by JSON filters plus a
per-document condition, sent once a day after a configurable time.

**Actions in.** Attach buttons to any message. A *Slack Action* applies a workflow
transition, sets a field, runs a Server Script, or calls a whitelisted method. Every
action runs **as the Frappe user the Slack account maps to**, so your existing
permissions and workflow rules apply unchanged, and the version history names the real
person. After an action the original message updates in place — buttons removed, an
audit line added — so nobody can approve the same thing twice.

**Commands and forms.** Route `/erp <subcommand>` to a modal form that creates or updates
a document, to a Server Script, or to a whitelisted method. Built-in subcommands cover
account linking, help, and GitHub-style channel subscriptions (`/erp subscribe <rule>`).

**Link previews.** Paste a document URL into Slack and it unfurls into a card with the
fields you chose — gated on the sharer's read permission, so nothing leaks into a channel.

**Log calls from messages.** A *Slack Communication Shortcut* adds a message shortcut
("Log as Phone Call") that turns any Slack message — typed notes or a **voice clip, using
Slack's own transcript** — into a timeline Communication on a document of your choosing.
A type-ahead picker searches the doctypes you configure (Leads, Customers, …), permission-
aware as the linked user. Slack bullet lists survive as nested lists, and the bot marks
the source message with a configurable ✅ reaction once it is in the ERP — the channel
sees at a glance what has been logged.

**Reliability.** Every outbound message is a durable outbox row: per-channel pacing to
respect Slack's one-message-per-second limit, exponential backoff, `Retry-After`
handling, permanent-error detection, and a full delivery log on the document's timeline.
Inbound requests are signature-verified and deduplicated, because Slack redelivers events
and people double-click buttons.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/flo-distrify/slack_bridge --branch version-15
bench --site your-site install-app slack_bridge
```

Use `--branch version-16` on Frappe v16.

## Setup

Everything is configured from the **Slack Bridge** workspace in the Desk sidebar.

**[Full setup guide →](docs/setup.md)** — the short version:

1. Open **Slack Workspace** → new record. Give it a name and save.
2. Click **Copy Manifest**, then open [api.slack.com/apps](https://api.slack.com/apps) →
   **Create New App** → **From a manifest**, pick your workspace, and paste it.
3. Install the app to your workspace, then copy the **Bot User OAuth Token** (`xoxb-…`)
   and the **Signing Secret** into the Frappe record and save.
4. Click **Test Connection**, then **Sync Users & Channels**, then
   **Create Starter Commands**.
5. Re-paste the manifest in Slack once so it registers your slash command.
6. In Slack, `/invite @ERP Bridge` into the channels you want to use, then run
   `/erp help` to check it all works.

Your site must be reachable from the internet over HTTPS — Slack delivers events to it.

You create the Slack app inside your own workspace, so it counts as an internal app: no
third-party service ever holds your token, and you are not subject to Slack's rate limits
for commercially distributed non-Marketplace apps.

## Documentation

- [Setup guide](docs/setup.md) — installation, Slack app creation, first rule, approval
  buttons, slash commands, unfurls, operations and troubleshooting
- [Marketplace checklist](docs/marketplace-checklist.md) — Frappe Cloud submission status

## Example: a purchase order approval

Create a *Slack Action* "Approve" (Workflow Action → `Approve`, required role Purchase
Manager), then a *Slack Notification Rule*:

- Document Type: Purchase Order, Send On: Submit
- Condition: `doc.grand_total > 10000`
- Recipients: channel `#purchasing`, plus a Document Field row pointing at `owner`
- Buttons: Approve (Primary) and Reject (Danger, with a "reason" form attached)

Submitting a large PO posts a card to `#purchasing`. A Purchase Manager clicks Approve,
the workflow advances in Frappe, and the message rewrites itself to
"✅ Approve applied — Jane Doe".

## Requirements

- Frappe v15 or v16 (ERPNext optional — the app works against any doctype)
- No third-party Python packages

## Testing

```bash
bench --site your-site run-tests --app slack_bridge
```

## Contributing

This app uses `pre-commit` for formatting and linting:

```bash
cd apps/slack_bridge
pre-commit install
```

## License

AGPL-3.0. See `license.txt`.
