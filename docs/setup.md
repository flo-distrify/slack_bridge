# Setup guide

From a fresh install to a working approval button in about fifteen minutes.

**Prerequisite:** your Frappe site must be reachable from the internet over HTTPS. Slack
delivers events, button clicks and slash commands to your site by URL, so `localhost`
installs need a tunnel (ngrok, Cloudflare Tunnel) or a public hostname.

---

## 1. Install the app

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/flo-distrify/slack_bridge --branch version-15
bench --site your-site.com install-app slack_bridge
```

Use `--branch version-16` on Frappe v16. The app has no third-party Python
dependencies and does not require ERPNext.

---

Everything the app adds lives in one place: the **Slack Bridge** workspace in the Desk
sidebar. It groups the doctypes into Setup, Notifications, Interactivity and Logs, with
shortcuts to the four you touch most.

## 2. Create the Slack app from the manifest

You create the Slack app inside your own workspace. Nothing is routed through a
third-party service, your bot token never leaves your site, and because the app is
"internal" to your workspace it is not subject to Slack's rate limits for commercially
distributed non-Marketplace apps.

1. In Frappe, open **Slack Workspace** and create a record. Give it a name (for example
   `Head Office`) and **Save**. Saving generates a secret endpoint token and builds the
   manifest.
2. Click **Copy Manifest**.
3. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** →
   **From a manifest**. Pick your workspace, paste the manifest, and confirm.
4. On the app's **Install App** page, click **Install to Workspace** and approve.

The manifest already contains your site's Request URLs, the bot scopes, the event
subscriptions and your site's domain for link unfurling — there is nothing else to
configure by hand in Slack.

## 3. Connect Frappe to Slack

Back in Slack, collect two values:

| Value | Where to find it |
| --- | --- |
| **Bot User OAuth Token** (`xoxb-…`) | **OAuth & Permissions** → *OAuth Tokens for Your Workspace* |
| **Signing Secret** | **Basic Information** → *App Credentials* |

Paste both into the Slack Workspace record and **Save**. Both are stored encrypted
(Frappe `Password` fields) and are never returned by the API.

Then click, in order:

1. **Test Connection** — calls `auth.test` and stores the team and bot identity. The
   headline should turn into "Connected to *your workspace*".
2. **Sync Users & Channels** — imports the channel list and matches Frappe users to Slack
   accounts by email address.
3. **Create Starter Commands** — creates the built-in `/erp` subcommands.

Because step 3 adds a slash command, **copy the manifest again and paste it into your
Slack app** (App Manifest → paste → Save) so Slack registers `/erp`. This is the only
time you need to update the manifest after the initial creation.

## 4. Invite the bot to a channel

In Slack, run `/invite @ERP Bridge` in each channel you want notifications in. The bot
can post to public channels without being a member, but it must be invited to private
ones, and being a member makes the channel show up correctly in the sync.

## 5. Verify

In Slack, run `/erp help`. You should get a private list of commands. Then run
`/erp whoami` — if it says you are not linked, run `/erp link` and the app will match
your Slack email to your Frappe user.

---

## Your first notification rule

**Slack Notification Rule** → new record:

| Field | Value |
| --- | --- |
| Title | `Large Sales Orders` |
| Workspace | your workspace |
| Document Type | `Sales Order` |
| Send On | `Submit` |
| Condition | `doc.grand_total > 10000` |
| Headline | `New order {{ doc.name }}` |
| Message | `{{ doc.customer }} ordered {{ frappe.utils.fmt_money(doc.grand_total, currency=doc.currency) }}` |
| Recipients | one row: Send To `Channel`, Resolve By `Static`, Channel `#sales` |

Save, then use **Preview Message** to render it against your most recent Sales Order
without sending anything, and **Send Test Message** to deliver it for real.

### Recipients are the flexible part

Each recipient row resolves one of three ways:

- **Static** — a fixed channel, or a fixed Frappe user (the app opens a DM).
- **Document Field** — read the recipient from a field on the document. Use `owner` for
  whoever created it, `modified_by` for whoever last touched it, or any Link-to-User
  field such as `sales_person`. The value may also be a Slack channel id.
- **Jinja** — an expression rendering to a user email or a channel id, for example
  `{{ frappe.db.get_value("Customer", doc.customer, "account_manager") }}`.

Add several rows to notify a channel *and* the document's owner from one rule. Each row
can carry its own condition.

### Daily digests

The **Daily Digest** event turns a rule into a per-user summary DM instead of a
per-document message. Once a day (at the first hourly scheduler tick after **Send
After**, site timezone) the app collects matching documents, groups them by the **Group
By User Field** — any Link-to-User field, or `owner` / `modified_by` — and sends every
Slack-mapped user one DM covering their whole group. Users with nothing to report, and
users without a Slack mapping, get nothing.

A digest rule needs no recipient rows (the grouped user *is* the recipient) and cannot
carry buttons. Templates see a different context: `docs` (the user's documents, up to
50), `count` (the full number), `user`, and `doc_url` (the list view).

Example — every assignee gets their open ToDos at 8:00:

- **Document Type** `ToDo`, **Send On** `Daily Digest`
- **Group By User Field** `allocated_to`, **Send After** `08:00:00`
- **Document Filters** `{"status": "Open"}`
- **Headline** `You have {{ count }} open ToDo(s)`
- **Message**

  ```jinja
  {% for d in docs %}• {{ d.description | striptags | truncate(120) }}
  {% endfor %}
  ```

**Send Test Message** delivers the digest immediately, ignoring the once-a-day and
send-time gates; **Preview Message** renders the first user's digest without sending.

### Threading

Set **Threading** to *Thread Under First Message* and every later event about the same
document lands in the same Slack thread rather than as a new message. *Update Previous
Message* rewrites the original in place instead — useful for a status that changes.

---

## Approval buttons

**Slack Action** → new record:

| Field | Value |
| --- | --- |
| Title | `Approve Purchase Order` |
| Document Type | `Purchase Order` |
| Action Type | `Workflow Action` |
| Workflow Action | `Approve` (a transition name from that doctype's workflow) |
| Required Role | `Purchase Manager` |
| Update Original Message | ✓ |

Then add a button to a notification rule: **Buttons** → Label `Approve`, Type `Action`,
Action `Approve Purchase Order`, Style `Primary`.

When somebody clicks it:

1. The request signature is verified and the click is deduplicated, so a double-click or
   a Slack retry cannot apply the action twice.
2. The Slack user is mapped to their Frappe user, and the action runs **as that person** —
   your document permissions, workflow rules and mandatory fields all apply, and the
   version history names them.
3. The original message is rewritten with the buttons removed and an audit line added
   ("✅ Approve applied — Jane Doe"), so nobody else can act on something already decided.
4. If anything fails — no permission, wrong workflow state, validation error — the person
   who clicked gets a private message. The channel sees nothing.

### Asking for a reason

Create a **Slack Form** with one Long Text field (say `rejection_reason`) targeting the
same doctype, then set it as **Collect Input First** on a "Reject" action. Clicking
Reject opens a modal; the submitted values are available to the action's Jinja as
`input.rejection_reason`.

---

## Slash commands and forms

A **Slack Command Route** maps `/erp <subcommand>` to one of:

- **Open Form** — opens a Slack Form as a modal that creates or updates a document.
- **Run Method** — calls a whitelisted method, executed as the linked user.
- **Run Server Script** — runs a Server Script.
- **Built-in** — `link`, `help`, `whoami`, `subscriptions`, `subscribe`, `unsubscribe`.

`/erp subscribe <rule name>` adds the current channel to that rule's recipients from
inside Slack, the way GitHub's Slack app works. `/erp subscriptions` lists every rule and
marks the ones this channel receives.

Adding a *new* command (not a subcommand) means re-pasting the manifest, because Slack
must register the command itself. New subcommands need no Slack-side change.

---

### Forms in the message menu

Tick **Offer as Message Shortcut** on a Slack Form and it appears in every message's
shortcuts menu (set a label ≤24 chars, a unique callback id, and optionally a
**Prefill Field** — a Text/Long Text field that receives the message's text, with
Slack link markup unwrapped). Right-click a message → the shortcut → adjust the
prefilled form → submit; the document is created as the submitting user and they get
a confirmation with a link. After enabling, regenerate the manifest and re-paste it
into the Slack app config so the menu entry appears — no reinstall, no new scopes.

Example — "Create ToDo" next to "Log as Phone Call": a Slack Form on ToDo with a
title field, `description` (Long Text, the prefill field) and `allocated_to` (User),
shortcut label "Create ToDo".

## Dynamic forms

When the form's choices and fields live in *another* app — start a process, book a
resource, run a parameterised job — a static **Slack Form** cannot express them. A
**Slack Dynamic Form** instead points at three dotted method paths in the providing
app (options / schema / submit), and the modal is built from that app's answers at
runtime: a searchable picker first, then the picked entry's input form. Everything
runs as the mapped Frappe user.

Wire it up with a Command Route (Does: *Open Dynamic Form*) — no manifest change, no
new scopes. The full provider contract, field vocabulary and a worked example live in
[dynamic-forms.md](dynamic-forms.md).

## Logging calls from messages

A **Slack Communication Shortcut** adds a *message shortcut* to Slack's context menu
(the "three dots" on any message). Invoking it opens a modal prefilled with the message
text — or, for a Slack voice clip, with Slack's own transcript — where the user picks
the target document in a type-ahead search and submits. The result is a
**Communication** (medium of your choice, e.g. Phone) on that document's timeline,
created as the linked Frappe user.

To enable it:

1. Create a **Slack Communication Shortcut**: label (max 24 chars), workspace, medium,
   and one **party doctype** row per searchable target — e.g. `Lead` with search fields
   `lead_name, company_name`, and `Customer` with `customer_name, name`.
2. Re-copy the generated manifest from the Slack Workspace document into your Slack
   app's configuration (it now contains the shortcut, the Options Load URL for the
   picker, and the `files:read` / `channels:join` scopes).
3. **Reinstall the app to the workspace** — a scope change always requires a reinstall.

Notes:

- Searching and logging both run as the mapped Frappe user; logging requires **write**
  permission on the target document.
- Voice-clip transcripts are Slack's native transcription. If it is still processing
  when the modal opens, the user is told to type notes or retry in a minute; an empty
  notes field is refetched once at submit time.
- Slack bullet lists (including `◦`/`▪` sub-levels) become nested lists in the logged
  Communication; mrkdwn links unwrap to "label (url)".
- After a successful log the bot reacts to the source message (default
  `:white_check_mark:`, per-shortcut **Synced Reaction** field, clear to disable).
  Reactions require channel membership: the bot joins public channels by itself
  (`channels:join`); in **private channels** it must be `/invite`d once.
- Everything is deduplicated: re-invoking the shortcut later is allowed, Slack's
  redeliveries and double submissions are not.

---

## Link previews

Create a **Slack Unfurl Rule** for a doctype and list the fields to show. Pasting a link
like `https://your-site.com/app/sales-order/SO-0001` into Slack renders a card.

Previews are only produced when the person who shared the link is a linked user with read
permission on that document, so pasting a link cannot leak fields to a channel.

---

## Operating it

- **Slack Message Log** is the delivery record: every message with its status, channel,
  Slack timestamp, retry count and last error. Failed and Dead rows have a **Retry Now**
  button. Slack activity also appears on the referenced document's timeline.
- **Slack Interaction Log** records every inbound click, command and modal submission with
  the mapped user and the outcome — the audit trail for who did what from Slack.
- **Slack Bridge Settings** holds the global kill switch, retry limits, per-channel pacing
  and log retention. Turn on *Verbose Logging* only while troubleshooting: it stores full
  Slack payloads.

### Troubleshooting

| Symptom | Cause |
| --- | --- |
| Slack shows "Your URL didn't respond with the value of the challenge parameter" | The site is not publicly reachable, or the endpoint token in the manifest is stale. Re-copy the manifest. |
| Buttons do nothing, no error | The clicker has no Slack User mapping, or their mapping has *Allow Actions from Slack* off. Ask them to run `/erp link`. |
| Messages stuck in Queued | The scheduler is not running (`bench doctor`), or the channel is being paced. |
| Status Dead with `channel_not_found` | The bot is not in that private channel — `/invite` it. |
| Status Dead with `missing_scope` | The Slack app predates a scope change. Re-paste the manifest and reinstall the app in Slack. |
| `/erp` returns "dispatch_failed" | Slack timed out waiting three seconds. Check that the site responds quickly and that background workers are running. |

---

## Security notes

- Bot tokens and signing secrets are stored in Frappe `Password` fields, encrypted at
  rest with the site's encryption key.
- Every inbound request is verified by HMAC-SHA256 over the raw body using the signing
  secret, and requests older than five minutes are rejected as replays.
- The Request URLs carry a per-workspace secret token, so the endpoints are not guessable
  even before signature verification.
- Actions never run as Administrator. An unmapped Slack user cannot change anything.
