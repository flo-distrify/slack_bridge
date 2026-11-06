# Document threads — the contract for other apps

A **document thread** keeps one document (a ticket, an order) visible in a Slack channel:
one message for the document, a thread under it that works in both directions, and bot
reactions that show its state. Another Frappe app drives it through four functions in
`slack_bridge.threads` and one hook. The coupling is by name only: the other app calls the
functions by dotted path when slack_bridge is installed, and slack_bridge calls the other
app through the hook. Neither imports the other.

## Calling in

```python
def bridge(name):
    if "slack_bridge" not in frappe.get_installed_apps():
        return None
    return frappe.get_attr(f"slack_bridge.threads.{name}")

bridge("post")(
    reference_doctype="Ticket",
    reference_name=doc.name,
    channel="customer_acme",          # a channel NAME; created (public) on first use
    text="*<https://erp.example/…|TCK-12 · Export fails>*",   # Slack mrkdwn
    invite=["jane@example.com"],      # Frappe users, joined to a channel the bot creates
    purpose="Tickets of Acme",        # set on a channel the bot creates
    reactions=["raised_hand"],        # optional, see set_reactions
)
```

| Function | What it does |
| --- | --- |
| `post(reference_doctype, reference_name, channel, text, blocks=None, workspace=None, create_channel=True, invite=None, purpose=None, reactions=None)` | The document's message. The first call queues it; every later call rewrites the same message in place. Returns the log name, or `None` when no workspace is configured. |
| `reply(reference_doctype, reference_name, text=None, html=None, author=None, key=None, blocks=None)` | A message in the document's thread. Pass mrkdwn as `text` or editor HTML as `html`. `author` (a user id or a name) is put in front in bold. The same `key` is never posted twice. `None` when the document has no thread. |
| `set_reactions(reference_doctype, reference_name, reactions)` | The bot reactions the document's message carries, as the full wanted list. What is missing is added, what is no longer listed is removed. |
| `has_thread(reference_doctype, reference_name)` | Whether the document has a message. |

None of them talks to Slack inline, so they are safe inside a document event. Messages go
through the outbox (retries, pacing, the delivery log). The channel is resolved, joined or
created when the message is delivered. Reactions are applied after delivery and retried
by the outbox cron until Slack shows what is wanted.

A reaction is always the bot's. Slack adds a reaction as the owner of the token, so a
reaction "from the person who took the ticket" would need a user token per person; say
who it was in the message text instead.

## Hearing replies

```python
# hooks.py
slack_thread_reply = {"Ticket": "my_app.slack.on_thread_reply"}

# my_app/slack.py
def on_thread_reply(doctype, name, message):
    ...
```

`message` holds `user` (the linked Frappe user, or `None`), `sender_name`, `slack_user_id`,
`text` (plain), `html` (escaped, Slack bullets as lists), `ts`, `thread_ts`, `channel_id`
and `files` (`[{name, url}]`). The handler runs as the linked user when the Slack account
is linked. If it raises, the reply gets a ⚠️ reaction in Slack so its author sees that it
did not arrive.

Only replies a person typed into a document's thread are handed over. Top-level channel
messages, edits, bot messages and threads that belong to no document are dropped at the
events endpoint without a log row.

## What the Slack app needs

Document threads use three things beyond the base install; the generated manifest carries
them:

- scope `channels:manage` — create the channel and invite people
- scope `channels:history` and the event `message.channels` — receive the replies
- scope `reactions:write` — set and remove the reactions

After updating slack_bridge on a site that is already connected, re-paste the manifest in
Slack (Slack Workspace → Copy Manifest) and reinstall the app to the workspace, otherwise
the first message fails with `missing_scope`. A message that was given up on that way is
sent the next time `post` is called for its document.

Channels are public. The bot only receives replies from channels it is a member of; it
joins a public channel on first use and is a member of every channel it creates.
