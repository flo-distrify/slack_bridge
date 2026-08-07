# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Log a call (or any communication) from a Slack message onto an ERP document.

A message shortcut opens a modal prefilled with the message text — or, for Slack voice
clips, with Slack's own transcript — the user picks the target document in a type-ahead
picker spanning the configured doctypes, and submission inserts a Communication on that
document's timeline as the mapped Frappe user. Configured per workspace through
Slack Communication Shortcut.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import escape_html

from slack_bridge.api import base
from slack_bridge.engine.context import get_doc_url, render
from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient
from slack_bridge.slack.client import respond as respond_url

CALLBACK_ID = "sb_comm_log"
PARTY_ACTION_ID = "sb_party_options"

BLOCK_PARTY = "cl_party"
BLOCK_SUBJECT = "cl_subject"
BLOCK_DIRECTION = "cl_direction"
BLOCK_NOTES = "cl_notes"
BLOCK_PHONE = "cl_phone"

# Leave room under Slack's 3000-char cap for plain_text_input initial values.
MAX_NOTES_PREFILL = 2900
MAX_SUBJECT = 140


def get_config(workspace_name: str, callback_id: str):
	rows = frappe.get_all(
		"Slack Communication Shortcut",
		filters={"workspace": workspace_name, "callback_id": callback_id, "enabled": 1},
		limit=1,
	)
	return frappe.get_cached_doc("Slack Communication Shortcut", rows[0].name) if rows else None


# -------------------------------------------------------------- message shortcut


def handle_message_action(workspace, payload: dict) -> None:
	slack_user_id = (payload.get("user") or {}).get("id")

	# action_ts is unique per invocation, so re-running the shortcut on the same
	# message later (say, once a transcript finished) is allowed, while Slack's
	# redeliveries of one invocation are deduped.
	key = base.idempotency_key(
		"comm_log", payload.get("callback_id"), payload.get("action_ts"), slack_user_id
	)
	log_name = base.claim(key, "Interactive", workspace.name, slack_user_id=slack_user_id)
	if not log_name:
		base.respond()
		return

	base.store_payload(log_name, payload)

	config = get_config(workspace.name, payload.get("callback_id"))
	if not config:
		base.finish(log_name, "Ignored", "No enabled shortcut config")
		notify_via_response_url(payload, _("This shortcut is not configured for this workspace."))
		return

	mapping = base.resolve_user(workspace.name, slack_user_id)
	if not mapping:
		base.finish(log_name, "Ignored", "Unmapped Slack user")
		# The HTTP response to a shortcut is not rendered by Slack — use the response_url.
		notify_via_response_url(payload, base.link_prompt()["text"])
		return

	if config.required_role and config.required_role not in frappe.get_roles(mapping.user):
		base.finish(log_name, "Ignored", f"Missing role {config.required_role}")
		notify_via_response_url(
			payload, _("You need the {0} role to use this shortcut.").format(config.required_role)
		)
		return

	try:
		notes, warning, file_id = extract_notes(workspace, payload)

		metadata = {
			"cs": config.name,
			"channel": (payload.get("channel") or {}).get("id"),
			"message_ts": (payload.get("message") or {}).get("ts"),
			"log": log_name,
		}
		if file_id:
			metadata["file"] = file_id

		view = build_comm_modal(config, notes=notes, warning=warning, metadata=metadata)
		SlackClient(workspace).open_view(payload.get("trigger_id"), view)
		base.finish(log_name, "Processed", "Modal opened")
		base.respond()
	except Exception as e:
		frappe.log_error(title="Slack Bridge: comm log shortcut failed", message=frappe.get_traceback())
		base.finish(log_name, "Failed", str(e))
		notify_via_response_url(payload, _("Could not open the form: {0}").format(str(e)[:150]))


def notify_via_response_url(payload: dict, text: str) -> None:
	url = payload.get("response_url")
	if not url:
		base.respond()
		return

	try:
		respond_url(url, {"response_type": "ephemeral", "text": text})
	except Exception:
		frappe.log_error(title="Slack Bridge: shortcut notify failed", message=frappe.get_traceback())

	base.respond()


def extract_notes(workspace, payload: dict) -> tuple[str, str | None, str | None]:
	"""The message text — or, for a voice clip, Slack's own transcript.

	Returns (notes, warning, file_id). file_id is set for audio files so the
	submission can refetch a transcript that was still processing.
	"""
	message = payload.get("message") or {}
	text = demarkdown((message.get("text") or "").strip())

	audio = next(
		(
			f
			for f in message.get("files") or []
			if f.get("subtype") == "slack_audio" or (f.get("mimetype") or "").startswith("audio/")
		),
		None,
	)
	if not audio:
		return text, None, None

	transcript, warning = fetch_transcript(workspace, audio.get("id"))
	if transcript:
		return transcript, warning, audio.get("id")

	return text, warning, audio.get("id")


def fetch_transcript(workspace, file_id: str) -> tuple[str | None, str | None]:
	"""(transcript, warning) from Slack's native voice-clip transcription."""
	if not file_id:
		return None, None

	try:
		info = SlackClient(workspace).file_info(file_id).get("file") or {}
	except Exception:
		frappe.log_error(title="Slack Bridge: files.info failed", message=frappe.get_traceback())
		return None, _("Could not read the audio file (is the files:read scope installed?).")

	transcription = info.get("transcription") or {}
	status = transcription.get("status")

	if status == "processing":
		return None, _(
			"Slack is still transcribing this clip. Type notes below, or close and retry the shortcut in a minute."
		)

	if status != "complete":
		return None, _("No transcript is available for this clip.")

	preview = transcription.get("preview") or {}
	text = (preview.get("content") or "").strip()

	if preview.get("has_more") and info.get("vtt"):
		try:
			full = vtt_to_text(SlackClient(workspace).fetch_file(info["vtt"], timeout=4))
			if full:
				text = full
		except Exception:
			# The preview is still a usable transcript; don't fail the modal over the tail.
			frappe.log_error(title="Slack Bridge: VTT fetch failed", message=frappe.get_traceback())

	return text or None, None if text else _("No transcript is available for this clip.")


def vtt_to_text(vtt: str) -> str:
	"""Collapse a WebVTT captions file into plain text."""
	lines = []
	in_note = False

	for raw in (vtt or "").splitlines():
		line = raw.strip()

		if not line:
			in_note = False
			continue
		if line.startswith("WEBVTT") or line.startswith("NOTE"):
			in_note = line.startswith("NOTE")
			continue
		if in_note or "-->" in line or line.isdigit():
			continue

		# Strip <v Speaker> style voice tags.
		while "<" in line and ">" in line:
			start = line.find("<")
			end = line.find(">", start)
			if end == -1:
				break
			line = line[:start] + line[end + 1 :]

		line = line.strip()
		# Slack VTT repeats rolling captions; drop consecutive duplicates.
		if line and (not lines or lines[-1] != line):
			lines.append(line)

	return " ".join(lines)


# ------------------------------------------------------------------ modal build


def build_comm_modal(config, notes: str, warning: str | None, metadata: dict) -> dict:
	party_labels = ", ".join(row.label or row.ref_doctype for row in config.party_doctypes)
	first_line = next((line.strip() for line in (notes or "").splitlines() if line.strip()), "")
	subject = bk.truncate(first_line, MAX_SUBJECT) or config.default_subject or _("Phone call")

	blocks = [
		{
			"type": "input",
			"block_id": BLOCK_PARTY,
			"label": {"type": "plain_text", "text": _("Regarding")[:75]},
			"element": {
				"type": "external_select",
				"action_id": PARTY_ACTION_ID,
				"min_query_length": config.min_query_length or 1,
				"placeholder": {
					"type": "plain_text",
					"text": bk.truncate(_("Search {0}").format(party_labels), 150),
				},
			},
		},
		{
			"type": "input",
			"block_id": BLOCK_SUBJECT,
			"label": {"type": "plain_text", "text": _("Subject")[:75]},
			"element": {"type": "plain_text_input", "action_id": "value", "initial_value": subject},
		},
		{
			"type": "input",
			"block_id": BLOCK_DIRECTION,
			"label": {"type": "plain_text", "text": _("Direction")[:75]},
			"element": {
				"type": "static_select",
				"action_id": "value",
				"options": direction_options(),
				"initial_option": direction_option(config.default_direction or "Outbound"),
			},
		},
		{
			"type": "input",
			"block_id": BLOCK_NOTES,
			"optional": True,
			"label": {"type": "plain_text", "text": _("Notes")[:75]},
			"element": {
				"type": "plain_text_input",
				"action_id": "value",
				"multiline": True,
				**({"initial_value": bk.truncate(notes, MAX_NOTES_PREFILL)} if notes else {}),
			},
		},
	]

	if config.collect_phone_no:
		blocks.append(
			{
				"type": "input",
				"block_id": BLOCK_PHONE,
				"optional": True,
				"label": {"type": "plain_text", "text": _("Phone Number")[:75]},
				"element": {"type": "plain_text_input", "action_id": "value"},
			}
		)

	if warning:
		blocks.append(bk.context(":warning: " + warning))

	serialised = json.dumps(metadata, separators=(",", ":"))
	if len(serialised) > bk.MAX_PRIVATE_METADATA:
		frappe.throw(_("Too much context to carry through the modal."))

	return {
		"type": "modal",
		"callback_id": CALLBACK_ID,
		"title": {"type": "plain_text", "text": (config.shortcut_label or _("Log Call"))[:24]},
		"submit": {"type": "plain_text", "text": _("Log")[:24]},
		"close": {"type": "plain_text", "text": _("Cancel")},
		"blocks": blocks,
		"private_metadata": serialised,
	}


def direction_options() -> list[dict]:
	return [direction_option("Outbound"), direction_option("Inbound")]


def direction_option(direction: str) -> dict:
	labels = {"Outbound": _("Outbound"), "Inbound": _("Inbound")}
	return {
		"text": {"type": "plain_text", "text": labels.get(direction, direction)[:75]},
		"value": direction,
	}


# ------------------------------------------------------------------ party picker


def handle_block_suggestion(workspace, payload: dict) -> None:
	"""Serve external_select options. Read-only, fired per keystroke — never 500."""
	try:
		groups = party_option_groups(workspace, payload)
		base.respond({"option_groups": groups} if groups else {"options": []})
	except Exception:
		frappe.log_error(title="Slack Bridge: option lookup failed", message=frappe.get_traceback())
		base.respond({"options": []})


def party_option_groups(workspace, payload: dict) -> list[dict]:
	if payload.get("action_id") != PARTY_ACTION_ID:
		return []

	try:
		metadata = json.loads((payload.get("view") or {}).get("private_metadata") or "{}")
	except ValueError:
		metadata = {}

	if not metadata.get("cs") or not frappe.db.exists("Slack Communication Shortcut", metadata["cs"]):
		return []

	config = frappe.get_cached_doc("Slack Communication Shortcut", metadata["cs"])
	mapping = base.resolve_user(workspace.name, (payload.get("user") or {}).get("id"))
	if not mapping:
		return []

	query = (payload.get("value") or "").strip()
	groups = []
	original_user = frappe.session.user
	try:
		frappe.set_user(mapping.user)
		for index, row in enumerate(config.party_doctypes):
			options = party_options(row, index, query, config.search_limit or 8)
			if options:
				groups.append(
					{
						"label": {"type": "plain_text", "text": (row.label or row.ref_doctype)[:75]},
						"options": options,
					}
				)
	finally:
		frappe.set_user(original_user)

	return groups


def party_options(row, index: int, query: str, limit: int) -> list[dict]:
	from slack_bridge.slack_bridge.doctype.slack_communication_shortcut.slack_communication_shortcut import (
		split_fields,
	)

	search_fields = split_fields(row.search_fields)
	fields = list({"name", row.display_field or "", *search_fields} - {""})

	try:
		# frappe.get_list validates fieldnames and applies the session user's permissions.
		records = frappe.get_list(
			row.ref_doctype,
			or_filters=[[row.ref_doctype, f, "like", f"%{query}%"] for f in search_fields],
			fields=fields,
			limit=limit,
			order_by="modified desc",
		)
	except Exception:
		frappe.log_error(title="Slack Bridge: party search failed", message=frappe.get_traceback())
		return []

	options = []
	for record in records:
		label = record.get(row.display_field) if row.display_field else None
		if not label:
			label = next((record.get(f) for f in search_fields if record.get(f)), None)
		text = f"{label} ({record.name})" if label and label != record.name else record.name
		options.append(
			{
				"text": {"type": "plain_text", "text": bk.truncate(text, 75)},
				# Slack caps option values at 150 chars; "{doctype}::{name}" could
				# overflow, so carry the child-row index instead of the doctype.
				"value": f"{index}::{record.name}"[:150],
			}
		)

	return options


# ------------------------------------------------------------------- submission


def submit_comm_log(workspace, view: dict, metadata: dict, user: str, slack_user_id: str, log_name: str):
	"""Runs inline from handle_view_submission; returns the response_action dict."""
	state = ((view.get("state") or {}).get("values")) or {}

	def value_of(block_id: str, action_id: str = "value"):
		return (state.get(block_id) or {}).get(action_id) or {}

	party_raw = (value_of(BLOCK_PARTY, PARTY_ACTION_ID).get("selected_option") or {}).get("value") or ""
	subject = (value_of(BLOCK_SUBJECT).get("value") or "").strip()
	direction = (value_of(BLOCK_DIRECTION).get("selected_option") or {}).get("value") or "Outbound"
	notes = (value_of(BLOCK_NOTES).get("value") or "").strip()
	phone = (value_of(BLOCK_PHONE).get("value") or "").strip()

	if not metadata.get("cs") or not frappe.db.exists("Slack Communication Shortcut", metadata["cs"]):
		return errors(BLOCK_PARTY, _("This shortcut is no longer configured."))

	config = frappe.get_cached_doc("Slack Communication Shortcut", metadata["cs"])
	if not config.enabled:
		return errors(BLOCK_PARTY, _("This shortcut has been disabled."))

	index, sep, docname = party_raw.partition("::")
	if not sep or not index.isdigit() or int(index) >= len(config.party_doctypes):
		return errors(BLOCK_PARTY, _("Pick a document from the list."))

	ref_doctype = config.party_doctypes[int(index)].ref_doctype

	# A transcript that was still processing when the modal opened may be done now.
	if not notes and metadata.get("file"):
		transcript, _warning = fetch_transcript(workspace, metadata["file"])
		notes = transcript or notes

	original_user = frappe.session.user
	try:
		frappe.set_user(user)

		if not frappe.db.exists(ref_doctype, docname):
			return errors(BLOCK_PARTY, _("{0} {1} no longer exists.").format(ref_doctype, docname))

		ref = frappe.get_doc(ref_doctype, docname)
		try:
			ref.check_permission("write")
		except frappe.PermissionError:
			return errors(BLOCK_PARTY, _("You don't have permission to log against {0}.").format(docname))

		comm = frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"communication_medium": config.communication_medium,
				"subject": subject[:MAX_SUBJECT] or config.default_subject or _("Phone call"),
				"content": notes_to_html(notes),
				"phone_no": phone or None,
				"sender": user,
				"sent_or_received": "Sent" if direction == "Outbound" else "Received",
				"reference_doctype": ref_doctype,
				"reference_name": docname,
				"status": "Linked",
			}
		)
		comm.insert(ignore_permissions=True)
		frappe.db.commit()
	finally:
		frappe.set_user(original_user)

	base.finish(log_name, "Processed", f"Communication {comm.name} on {ref_doctype} {docname}")
	confirm_logged(workspace, config, ref, metadata, slack_user_id)
	return {"response_action": "clear"}


def errors(block_id: str, message: str) -> dict:
	return {"response_action": "errors", "errors": {block_id: message}}


def demarkdown(text: str) -> str:
	"""Undo Slack's mrkdwn transport encoding for plain-text use.

	Slack escapes &, < and > in message text and wraps links as <url|label>.
	Without this, the escaped entities get escaped AGAIN on the way into the
	Communication ("->" arrives as "-&amp;gt;").
	"""
	if not text:
		return ""

	out = []
	rest = text
	while "<" in rest:
		before, _bracket, tail = rest.partition("<")
		out.append(before)
		inner, closed, rest = tail.partition(">")
		if not closed:
			out.append("<" + inner)
			break
		# <url|label> → "label (url)"; <@U…>/<#C…|name> and bare <url> keep their body.
		target, pipe, label = inner.partition("|")
		if pipe and not target.startswith(("@", "#", "!")):
			out.append(f"{label} ({target})")
		else:
			out.append(label if pipe else target)
	out.append(rest)

	return "".join(out).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


BULLET_PREFIXES = ("• ", "- ", "* ")


def notes_to_html(notes: str) -> str:
	"""Escaped HTML for the Communication, with Slack-style bullets as real lists."""
	if not notes:
		return ""

	parts: list[str] = []
	bullets: list[str] = []

	def flush_bullets():
		if bullets:
			parts.append("<ul>" + "".join(f"<li>{escape_html(b)}</li>" for b in bullets) + "</ul>")
			bullets.clear()

	for line in notes.splitlines():
		stripped = line.strip()
		prefix = next((p for p in BULLET_PREFIXES if stripped.startswith(p)), None)

		if prefix:
			bullets.append(stripped[len(prefix) :].strip())
		elif not stripped:
			flush_bullets()
		else:
			flush_bullets()
			parts.append(f"<div>{escape_html(stripped)}</div>")

	flush_bullets()
	return "".join(parts)


def confirm_logged(workspace, config, ref, metadata: dict, slack_user_id: str) -> None:
	url = get_doc_url(ref)
	title = ref.get(ref.meta.get_title_field() or "name") or ref.name
	text = _("Logged a {0} communication on <{1}|{2}>").format(config.communication_medium, url, title)

	if config.confirmation_message:
		try:
			text = render(config.confirmation_message, ref, {"doc_url": url})
		except Exception:
			pass

	client = SlackClient(workspace)
	try:
		if metadata.get("channel"):
			client.post_ephemeral(channel=metadata["channel"], user=slack_user_id, text=text)
		else:
			dm = client.open_dm(slack_user_id)
			client.post_message(channel=dm, text=text, blocks=[bk.section(text)])
	except Exception:
		frappe.log_error(title="Slack Bridge: confirmation failed", message=frappe.get_traceback())
