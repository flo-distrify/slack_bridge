# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Document threads: one Slack message per document, with a two-way thread under it.

Another app keeps a document (a ticket, an order) visible in a Slack channel:

* :func:`post` — the document's message, the root of its thread. Created once; a later
  call rewrites it in place. The channel is addressed by NAME and created on first use.
* :func:`reply` — a message in that thread.
* :func:`set_reactions` — the bot reactions the root carries, as a desired state
  (``["raised_hand"]`` now, ``["raised_hand", "white_check_mark"]`` later, ``[]`` to clear).

and hears what people answer in the thread through a hook::

        # hooks.py of the other app
        slack_thread_reply = {"My DocType": "my_app.slack.on_thread_reply"}


        def on_thread_reply(doctype, name, message):
            # message: user (the linked Frappe user or None), sender_name, slack_user_id,
            # text, html, ts, thread_ts, channel_id, files [{name, url}]
            ...

The handler runs as the linked Frappe user when the Slack account is linked. The other
app calls these functions by dotted path (``frappe.get_attr("slack_bridge.threads.post")``)
only when slack_bridge is installed; neither app imports the other.

Nothing here talks to Slack inline. Messages go through the outbox (durable, retried,
paced); the channel is resolved — and created — at delivery; reactions are reconciled
after delivery and by the outbox cron until Slack shows what is wanted.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import ClassVar

import frappe
from frappe import _
from frappe.utils import now_datetime

from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient, SlackError, SlackRateLimited

LOG = "Slack Message Log"
# A unique field that is taken raises one or the other, depending on the Frappe version.
DUPLICATE = (frappe.exceptions.DuplicateEntryError, frappe.exceptions.UniqueValidationError)
ROOT = "Root"
REPLY = "Reply"
REPLY_MODE = "Reply To Root"
THREAD_REPLY_HOOK = "slack_thread_reply"
# Slack channel names: lowercase letters, digits, hyphens and underscores, 80 at most.
CHANNEL_NAME_LIMIT = 80
# A reply a person typed; "file_share" is the same with an attachment.
REPLY_SUBTYPES = (None, "", "file_share", "thread_broadcast")
RECONCILE_BATCH = 50
ROOT_FIELDS = [
	"name",
	"status",
	"workspace",
	"channel_id",
	"message_ts",
	"payload",
	"reactions_wanted",
	"reactions_applied",
]


# ----------------------------------------------------------------------- lookup


def resolve_workspace(workspace: str | None = None) -> str | None:
	"""The workspace to use: the one named, else the Settings default, else the only
	enabled one. None when Slack Bridge is off or the choice is not clear."""
	from slack_bridge.api.base import is_enabled

	if not is_enabled():
		return None
	for candidate in (workspace, frappe.db.get_single_value("Slack Bridge Settings", "default_workspace")):
		if candidate and frappe.db.get_value("Slack Workspace", candidate, "enabled"):
			return candidate
	if workspace:
		return None
	names = frappe.get_all("Slack Workspace", filters={"enabled": 1}, pluck="name", limit=2)
	return names[0] if len(names) == 1 else None


def find_root(reference_doctype: str, reference_name: str, live_only: bool = True):
	"""The root message row of a document's thread, or None. ``live_only`` leaves out a
	root that was given up on (Dead / Skipped)."""
	filters = {
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"thread_role": ROOT,
	}
	if live_only:
		filters["status"] = ("not in", ("Dead", "Skipped"))
	rows = frappe.get_all(LOG, filters=filters, fields=ROOT_FIELDS, order_by="creation asc", limit=1)
	return rows[0] if rows else None


def has_thread(reference_doctype: str, reference_name: str) -> bool:
	return bool(find_root(reference_doctype, reference_name))


def clean_channel_name(name: str) -> str:
	name = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lstrip("#").lower()).strip("-")
	return name[:CHANNEL_NAME_LIMIT]


def _key(*parts) -> str:
	return hashlib.sha1("|".join(str(p or "") for p in parts).encode()).hexdigest()


def _enqueue(method: str, log_name: str) -> None:
	frappe.enqueue(
		method,
		queue="short",
		enqueue_after_commit=True,
		log_name=log_name,
	)


def _deliver_soon(log_name: str) -> None:
	frappe.enqueue(
		"slack_bridge.engine.outbox.deliver",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"slack-bridge-deliver-{log_name}",
		deduplicate=True,
		log_name=log_name,
	)


# ------------------------------------------------------------------------ write


def post(
	reference_doctype: str,
	reference_name: str,
	channel: str,
	text: str,
	blocks: list | None = None,
	workspace: str | None = None,
	create_channel: bool = True,
	invite: list | None = None,
	purpose: str | None = None,
	reactions: list | None = None,
) -> str | None:
	"""The document's message in ``channel`` (a channel NAME; created at delivery when
	missing and ``create_channel``). The first call queues it; a later call rewrites the
	same message. ``invite`` (Frappe user ids) joins a newly created channel;
	``reactions`` is what :func:`set_reactions` takes. Returns the log name, or None when
	no workspace is configured."""
	workspace = resolve_workspace(workspace)
	channel = clean_channel_name(channel)
	if not (workspace and channel and reference_doctype and reference_name):
		return None
	blocks = blocks or [bk.section(text)]
	text = text or bk.fallback_text(blocks)

	root = find_root(reference_doctype, reference_name, live_only=False)
	if root:
		name = _rewrite_root(root, blocks, text)
		if reactions is not None:
			set_reactions(reference_doctype, reference_name, reactions)
		return name

	log = frappe.get_doc(
		{
			"doctype": LOG,
			"status": "Queued",
			"workspace": workspace,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"thread_role": ROOT,
			"reactions_wanted": _join(reactions or []),
			"dedupe_key": _key("thread-root", workspace, reference_doctype, reference_name),
			"payload": json.dumps(
				{
					"blocks": blocks,
					"text": text,
					"thread_mode": "New Message",
					"channel_name": channel,
					"create_channel": 1 if create_channel else 0,
					"invite": list(invite or []),
					"purpose": purpose or "",
				},
				separators=(",", ":"),
			),
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except DUPLICATE:
		root = find_root(reference_doctype, reference_name, live_only=False)
		return root.name if root else None
	_deliver_soon(log.name)
	return log.name


def _rewrite_root(root, blocks: list, text: str) -> str:
	payload = json.loads(root.payload or "{}")
	revive = root.status in ("Dead", "Skipped")
	if payload.get("blocks") == blocks and payload.get("text") == text and not revive:
		return root.name
	payload.update({"blocks": blocks, "text": text})
	values = {"payload": json.dumps(payload, separators=(",", ":"))}
	if revive:
		# A root that was given up on (the app lacked a scope, the channel was gone)
		# gets a fresh start with the current content.
		values.update({"status": "Queued", "attempts": 0, "next_attempt_at": None, "last_error": None})
	frappe.db.set_value(LOG, root.name, values, update_modified=False)
	if root.status == "Sent":
		_enqueue("slack_bridge.threads.push_root_update", root.name)
	elif revive:
		_deliver_soon(root.name)
	return root.name


def push_root_update(log_name: str) -> None:
	"""Background: rewrite the delivered root message with its current payload."""
	log = frappe.db.get_value(
		LOG, log_name, ["status", "workspace", "channel_id", "message_ts", "payload"], as_dict=True
	)
	if not log or log.status != "Sent" or not log.message_ts:
		return
	payload = json.loads(log.payload or "{}")
	try:
		SlackClient(log.workspace).update_message(
			channel=log.channel_id,
			ts=log.message_ts,
			blocks=payload.get("blocks") or [],
			text=payload.get("text"),
		)
	except Exception:
		frappe.log_error(title="Slack Bridge: thread message not updated", message=frappe.get_traceback())


def reply(
	reference_doctype: str,
	reference_name: str,
	text: str | None = None,
	html: str | None = None,
	author: str | None = None,
	key: str | None = None,
	blocks: list | None = None,
) -> str | None:
	"""A message in the document's thread. ``text`` is Slack mrkdwn, or pass ``html``
	(converted). ``author`` (a Frappe user id or a display name) is put in front in
	bold. ``key`` identifies the message (e.g. the note's name): the same key is never
	posted twice, two different keys with the same words both are. Returns the log name,
	or None when the document has no thread."""
	root = find_root(reference_doctype, reference_name)
	if not root:
		return None
	body = (text or html_to_mrkdwn(html or "")).strip()
	if not body and not blocks:
		return None
	if author:
		name = frappe.utils.get_fullname(author) if frappe.db.exists("User", author) else author
		body = f"*{escape_mrkdwn(name or author)}:* {body}"
	blocks = blocks or [bk.section(body)]
	unique = key or f"{now_datetime().strftime('%Y%m%d%H%M')}|{hashlib.sha1(body.encode()).hexdigest()}"
	log = frappe.get_doc(
		{
			"doctype": LOG,
			"status": "Queued",
			"workspace": root.workspace,
			"channel_id": root.channel_id,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"thread_role": REPLY,
			"dedupe_key": _key("thread-reply", root.name, unique),
			"payload": json.dumps(
				{
					"blocks": blocks,
					"text": bk.truncate(body, 300) or bk.fallback_text(blocks),
					"thread_mode": REPLY_MODE,
				},
				separators=(",", ":"),
			),
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except DUPLICATE:
		return None
	_deliver_soon(log.name)
	return log.name


def set_reactions(reference_doctype: str, reference_name: str, reactions: list | None) -> bool:
	"""The bot reactions the document's root message should carry — all of them; what is
	not listed is removed. Applied after the root is delivered. False without a thread."""
	root = find_root(reference_doctype, reference_name)
	if not root:
		return False
	wanted = _join(reactions or [])
	if wanted == (root.reactions_wanted or ""):
		return True
	frappe.db.set_value(LOG, root.name, "reactions_wanted", wanted, update_modified=False)
	if root.status == "Sent":
		_enqueue("slack_bridge.threads.apply_reactions", root.name)
	return True


def _split(value: str | None) -> set[str]:
	return {r for r in (value or "").split(",") if r}


def _join(reactions) -> str:
	return ",".join(sorted({str(r or "").strip().strip(":") for r in reactions} - {""}))


# -------------------------------------------------------------------- reactions


def apply_reactions(log_name: str) -> None:
	"""Make the bot's reactions on a delivered root match what is wanted."""
	log = frappe.db.get_value(
		LOG,
		log_name,
		["name", "status", "workspace", "channel_id", "message_ts", "reactions_wanted", "reactions_applied"],
		as_dict=True,
	)
	if not log or log.status != "Sent" or not log.message_ts:
		return
	wanted, applied = _split(log.reactions_wanted), _split(log.reactions_applied)
	if wanted == applied:
		return
	client = SlackClient(log.workspace)
	for name in sorted(wanted - applied):
		if _react(client, log, name, add=True):
			applied.add(name)
	for name in sorted(applied - wanted):
		if _react(client, log, name, add=False):
			applied.discard(name)
	frappe.db.set_value(LOG, log.name, "reactions_applied", _join(applied), update_modified=False)


def _react(client: SlackClient, log, name: str, add: bool) -> bool:
	"""One reaction change. True when it is done or pointless to repeat; False when the
	next reconcile should try again (rate limit, network)."""
	call = client.add_reaction if add else client.remove_reaction
	try:
		call(log.channel_id, log.message_ts, name)
		return True
	except SlackRateLimited:
		return False
	except SlackError as e:
		if e.code in ("already_reacted", "no_reaction"):
			return True
		if e.code in ("not_in_channel", "channel_not_found"):
			# reactions need membership; a public channel can be joined on demand
			try:
				client.join_channel(log.channel_id)
				call(log.channel_id, log.message_ts, name)
				return True
			except Exception:
				pass
		frappe.log_error(
			title="Slack Bridge: reaction not set",
			message=f"{'add' if add else 'remove'} :{name}: on {log.name}\n\n{frappe.get_traceback()}",
		)
		return True
	except Exception:
		return False


def before_delivered_save(log) -> bool:
	"""Outbox hook, before a delivered root is saved: take over what was set while the
	message was on its way, so the save does not put the older values back. True when the
	content was rewritten meanwhile (the delivered message is then already outdated)."""
	if log.get("thread_role") != ROOT:
		return False
	# a locking read returns the latest committed row, whatever this transaction saw before
	fresh = frappe.db.get_value(LOG, log.name, ["reactions_wanted", "payload"], as_dict=True, for_update=True)
	if not fresh:
		return False
	log.reactions_wanted = fresh.reactions_wanted
	if fresh.payload and fresh.payload != log.payload:
		log.payload = fresh.payload
		return True
	return False


def after_delivery(log, rewritten: bool = False) -> None:
	"""Outbox hook: a root that was just delivered gets the reactions (and the content)
	set meanwhile."""
	if log.get("thread_role") != ROOT:
		return
	try:
		if rewritten:
			push_root_update(log.name)
		if (log.get("reactions_wanted") or "") != (log.get("reactions_applied") or ""):
			apply_reactions(log.name)
	except Exception:
		frappe.log_error(
			title="Slack Bridge: thread follow-up after delivery", message=frappe.get_traceback()
		)


def reconcile() -> None:
	"""Outbox cron: roots whose reactions still differ from what is wanted."""
	names = frappe.db.sql_list(
		f"""
		select name from `tab{LOG}`
		where thread_role = %s and status = 'Sent'
			and ifnull(reactions_wanted, '') != ifnull(reactions_applied, '')
		order by modified desc limit %s
		""",
		(ROOT, RECONCILE_BATCH),
	)
	for name in names:
		try:
			apply_reactions(name)
		except Exception:
			frappe.log_error(title="Slack Bridge: reaction reconcile failed", message=frappe.get_traceback())


# --------------------------------------------------------------------- channels


def ensure_channel(
	workspace: str,
	name: str,
	create: bool = True,
	invite: list | None = None,
	purpose: str | None = None,
) -> str:
	"""The id of the channel called ``name``, with the bot in it. A missing channel is
	created (public) when ``create``; a new one gets ``purpose`` and the ``invite``
	users (Frappe user ids that are linked to Slack). Raises SlackError otherwise."""
	name = clean_channel_name(name)
	client = SlackClient(workspace)
	row = _channel_row(workspace, name)
	if not row:
		# The directory may be stale: someone made the channel by hand.
		found = _find_in_slack(client, name)
		if found:
			row = _remember_channel(workspace, found)

	if row:
		if row.is_archived:
			raise SlackError(
				_("The Slack channel #{0} is archived; unarchive it in Slack.").format(name),
				code="is_archived",
			)
		if not row.is_member and not row.is_private:
			client.join_channel(row.channel_id)
			frappe.db.set_value("Slack Channel", row.name, "is_member", 1, update_modified=False)
		return row.channel_id

	if not create:
		raise SlackError(_("There is no Slack channel #{0}.").format(name), code="channel_not_found")

	try:
		channel = client.create_channel(name)
	except SlackError as e:
		if e.code != "name_taken":
			raise
		# Someone else created it this very moment.
		channel = _find_in_slack(client, name)
		if not channel:
			raise
		if not channel.get("is_member") and not channel.get("is_private"):
			client.join_channel(channel["id"])
		return _remember_channel(workspace, {**channel, "is_member": True}).channel_id

	row = _remember_channel(workspace, {**channel, "is_member": True})
	_dress_new_channel(client, workspace, row.channel_id, invite, purpose)
	return row.channel_id


def _channel_row(workspace: str, name: str):
	return frappe.db.get_value(
		"Slack Channel",
		{"workspace": workspace, "channel_name": name},
		["name", "channel_id", "is_member", "is_archived", "is_private"],
		as_dict=True,
		order_by="is_archived asc",
	)


def _find_in_slack(client: SlackClient, name: str) -> dict | None:
	for channel in client.list_conversations(types="public_channel"):
		if channel.get("name") == name:
			return channel
	return None


def _remember_channel(workspace: str, channel: dict):
	docname = f"{workspace}-{channel['id']}"
	values = {
		"channel_id": channel["id"],
		"channel_name": channel.get("name") or channel["id"],
		"workspace": workspace,
		"is_private": 1 if channel.get("is_private") else 0,
		"is_archived": 1 if channel.get("is_archived") else 0,
		"is_member": 1 if channel.get("is_member") else 0,
	}
	if frappe.db.exists("Slack Channel", docname):
		frappe.db.set_value("Slack Channel", docname, values, update_modified=False)
	else:
		try:
			frappe.get_doc({"doctype": "Slack Channel", **values}).insert(ignore_permissions=True)
		except DUPLICATE:
			pass
	return frappe._dict(name=docname, **values)


def _dress_new_channel(client: SlackClient, workspace: str, channel_id: str, invite, purpose) -> None:
	"""Purpose and members of a channel the bot just created. Never fails the delivery."""
	try:
		if purpose:
			client.set_channel_purpose(channel_id, purpose)
	except Exception:
		frappe.log_error(title="Slack Bridge: channel purpose not set", message=frappe.get_traceback())
	try:
		members = slack_ids_for(workspace, invite or [])
		if members:
			client.invite_to_channel(channel_id, members)
	except SlackError as e:
		if e.code not in ("already_in_channel", "cant_invite_self"):
			frappe.log_error(title="Slack Bridge: channel invite failed", message=frappe.get_traceback())
	except Exception:
		frappe.log_error(title="Slack Bridge: channel invite failed", message=frappe.get_traceback())


def slack_ids_for(workspace: str, users: list) -> list[str]:
	"""Slack member ids of the Frappe users that are linked in this workspace."""
	users = [u for u in users or [] if u]
	if not users:
		return []
	return frappe.get_all(
		"Slack User",
		filters={"workspace": workspace, "user": ("in", users), "enabled": 1},
		pluck="slack_user_id",
		distinct=True,
	)


def resolve_log_channel(log, payload: dict) -> None:
	"""Outbox: give a row that names its channel (``channel_name``) the channel id."""
	if log.channel_id or not payload.get("channel_name"):
		return
	log.channel_id = ensure_channel(
		log.workspace,
		payload["channel_name"],
		create=bool(payload.get("create_channel")),
		invite=payload.get("invite"),
		purpose=payload.get("purpose"),
	)


# ---------------------------------------------------------------------- inbound


def is_thread_reply(event: dict) -> bool:
	"""A message a person typed into a thread (not the root, not a bot, not an edit)."""
	if event.get("type") != "message" or event.get("subtype") not in REPLY_SUBTYPES:
		return False
	if event.get("bot_id") or not event.get("user"):
		return False
	thread_ts = event.get("thread_ts")
	return bool(thread_ts and thread_ts != event.get("ts"))


def thread_root_for(workspace: str, channel_id: str, thread_ts: str):
	"""The root row a Slack thread belongs to, or None when it is no document thread."""
	rows = frappe.get_all(
		LOG,
		filters={
			"workspace": workspace,
			"message_ts": thread_ts,
			"channel_id": channel_id,
			"thread_role": ROOT,
			"status": "Sent",
		},
		fields=["name", "reference_doctype", "reference_name"],
		limit=1,
	)
	return rows[0] if rows else None


def wants_event(workspace: str, event: dict) -> bool:
	"""Cheap gate for the events endpoint: only replies in a document thread are worth a
	log row and a job. Everything else said in a channel is dropped at the door."""
	if not is_thread_reply(event):
		return False
	return bool(thread_root_for(workspace, event.get("channel"), event.get("thread_ts")))


def handle_message_event(workspace: str, event: dict) -> str:
	"""Hand a reply in a document thread to the app that owns the document."""
	if not is_thread_reply(event):
		return "Not a thread reply"
	if event.get("user") == frappe.db.get_value("Slack Workspace", workspace, "bot_user_id"):
		return "Own message"
	root = thread_root_for(workspace, event.get("channel"), event.get("thread_ts"))
	if not root:
		return "No document thread"
	handlers = (frappe.get_hooks(THREAD_REPLY_HOOK) or {}).get(root.reference_doctype) or []
	if not handlers:
		return f"No handler for {root.reference_doctype}"

	message = build_reply_message(workspace, event)
	previous = frappe.session.user
	try:
		if message["user"]:
			frappe.set_user(message["user"])
		for path in handlers:
			frappe.get_attr(path)(doctype=root.reference_doctype, name=root.reference_name, message=message)
	except Exception:
		_flag_failed_reply(workspace, event)
		raise
	finally:
		frappe.set_user(previous)
	return f"Handed to {root.reference_doctype} {root.reference_name}"


def build_reply_message(workspace: str, event: dict) -> dict:
	from slack_bridge.api.comm_log import demarkdown, notes_to_html

	slack_user_id = event.get("user")
	mapping = frappe.db.get_value(
		"Slack User",
		{"workspace": workspace, "slack_user_id": slack_user_id},
		["user", "enabled", "slack_real_name", "slack_username"],
		as_dict=True,
	)
	user = mapping.user if mapping and mapping.enabled and mapping.user else None
	sender_name = (
		(frappe.utils.get_fullname(user) if user else None)
		or (mapping and (mapping.slack_real_name or mapping.slack_username))
		or _slack_display_name(workspace, slack_user_id)
		or slack_user_id
	)
	text = demarkdown(_name_mentions(workspace, event.get("text") or ""))
	return {
		"user": user,
		"sender_name": sender_name,
		"slack_user_id": slack_user_id,
		"text": text,
		"html": notes_to_html(text),
		"ts": event.get("ts"),
		"thread_ts": event.get("thread_ts"),
		"channel_id": event.get("channel"),
		"files": [
			{
				"name": f.get("name") or f.get("title") or "file",
				"url": f.get("permalink") or f.get("url_private"),
			}
			for f in event.get("files") or []
		],
	}


def _slack_display_name(workspace: str, slack_user_id: str) -> str | None:
	try:
		info = SlackClient(workspace).user_info(slack_user_id)
	except Exception:
		return None
	profile = info.get("profile") or {}
	return (
		info.get("real_name") or profile.get("real_name") or profile.get("display_name") or info.get("name")
	)


def _name_mentions(workspace: str, text: str) -> str:
	"""``<@U123>`` → ``@Jane Doe`` for the people the directory knows."""

	def name(match):
		real = frappe.db.get_value(
			"Slack User", {"workspace": workspace, "slack_user_id": match.group(1)}, "slack_real_name"
		)
		return f"@{real}" if real else match.group(0)

	return re.sub(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", name, text)


def _flag_failed_reply(workspace: str, event: dict) -> None:
	"""The person sees that their reply did not reach the ERP."""
	try:
		SlackClient(workspace).add_reaction(event.get("channel"), event.get("ts"), "warning")
	except Exception:
		pass


# ------------------------------------------------------------------- formatting


def escape_mrkdwn(text: str) -> str:
	return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _Mrkdwn(HTMLParser):
	MARKS: ClassVar[dict] = {
		"b": "*",
		"strong": "*",
		"i": "_",
		"em": "_",
		"s": "~",
		"strike": "~",
		"del": "~",
		"code": "`",
	}
	BLOCKS = ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "blockquote", "pre")

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.out: list[str] = []
		self.links: list[str | None] = []
		self.lists: list[list] = []

	def handle_starttag(self, tag, attrs):
		attrs = dict(attrs)
		if tag in self.MARKS:
			self.out.append(self.MARKS[tag])
		elif tag == "br":
			self.out.append("\n")
		elif tag == "a":
			href = (attrs.get("href") or "").strip()
			self.links.append(href if href.startswith(("http://", "https://", "mailto:")) else None)
			if self.links[-1]:
				self.out.append(f"<{self.links[-1]}|")
		elif tag in ("ul", "ol"):
			self.out.append("\n")
			self.lists.append([tag, 0])
		elif tag == "li":
			kind = self.lists[-1] if self.lists else ["ul", 0]
			kind[1] += 1
			indent = "    " * max(0, len(self.lists) - 1)
			self.out.append(f"\n{indent}{kind[1]}. " if kind[0] == "ol" else f"\n{indent}• ")
		elif tag == "img":
			self.out.append("_(image)_")
		elif tag == "pre":
			self.out.append("\n```\n")
		elif tag in self.BLOCKS:
			self.out.append("\n")

	def handle_endtag(self, tag):
		if tag in self.MARKS:
			self.out.append(self.MARKS[tag])
		elif tag == "a":
			if self.links and self.links.pop():
				self.out.append(">")
		elif tag in ("ul", "ol"):
			if self.lists:
				self.lists.pop()
			self.out.append("\n")
		elif tag == "pre":
			self.out.append("\n```\n")
		elif tag in self.BLOCKS:
			self.out.append("\n")

	def handle_data(self, data):
		self.out.append(escape_mrkdwn(data))


def html_to_mrkdwn(html: str) -> str:
	"""Editor HTML → Slack mrkdwn, best effort: bold, italic, strike, code, links, lists,
	line breaks. Everything else is reduced to its text."""
	if not html:
		return ""
	parser = _Mrkdwn()
	parser.feed(html)
	parser.close()
	text = "".join(parser.out).replace("\xa0", " ")
	text = re.sub(r"[ \t]+\n", "\n", text)
	return re.sub(r"\n{3,}", "\n\n", text).strip()
