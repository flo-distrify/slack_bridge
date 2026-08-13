# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack as a message channel for Distrify Processes' Send Message nodes.

Registered through distrify_processes' ``process_message_channels`` hook — a bare
hook string in our hooks.py, mirrored on ``slack_bridge_doc_url`` (which
distrify_studio implements for us the same way). There is **no import in either
direction**: distrify_processes resolves this module by dotted path via the hook,
and this module only uses slack_bridge's own machinery. When distrify_processes
is not installed, the hook is never read and this file is inert.

Contract (defined by distrify_processes.messaging):
    get_channels() -> [spec]
    send(recipient, subject, body, context) -> detail str | None; raises on failure.
``body`` is Markdown; we down-convert to Slack mrkdwn (best effort). ``context``
is plain data: {instance, node_id, definition, instance_subject, variables,
references}.
"""

from __future__ import annotations

import re

import frappe
from frappe import _

from slack_bridge.engine import outbox, recipients
from slack_bridge.slack import blocks
from slack_bridge.slack.client import SlackClient


def get_channels() -> list[dict]:
	return [
		{
			"name": "slack",
			"label": "Slack",
			"description": "Post to a Slack channel or DM a user via Slack Bridge.",
			"recipient_hint": "#channel, user email, or conversation id — comma-separated",
			"supports_subject": False,
			"send": "slack_bridge.integrations.process_channel.send",
		}
	]


def send(recipient: str, subject: str | None, body: str, context: dict) -> str | None:
	"""Queue the message into the Slack outbox (durable delivery: pacing, retries,
	threading per process run, desk-timeline row). Raises when the workspace is
	ambiguous or a recipient cannot be resolved."""
	workspace = _resolve_workspace()
	client = SlackClient(workspace)

	text = _md_to_mrkdwn(body)
	if subject:
		text = f"*{blocks.truncate(subject, 150)}*\n{text}"
	message_blocks = [blocks.section(blocks.truncate(text))]
	if context.get("instance_subject"):
		message_blocks.append(blocks.context(_("Process run: {0}").format(context["instance_subject"])))

	# Reference the run only when the caller actually named one — the reference
	# doctype is a Link, and "Process Instance" only validates where the processes
	# app is installed (which is guaranteed for hook-driven calls, but this module
	# must also stand alone, e.g. under this app's own test suite).
	reference = (
		{"reference_doctype": "Process Instance", "reference_name": context["instance"]}
		if context.get("instance")
		else {}
	)

	sent = []
	for target in _split(recipient):
		resolved = _resolve_target(target, workspace, client)
		if not resolved:
			frappe.throw(_("Could not resolve Slack recipient '{0}' in workspace {1}.").format(target, workspace))
		# event_method distinguishes nodes in the outbox dedupe key; queue() -> None
		# means an identical send this minute is already queued (e.g. a fast operator
		# Retry) — that IS delivered, so treat it as success.
		result = outbox.queue(
			workspace=workspace,
			channel_id=resolved["channel_id"],
			blocks=message_blocks,
			text=blocks.fallback_text(message_blocks),
			event_method=f"process-node:{context.get('node_id')}",
			**reference,
		)
		label = resolved.get("label") or resolved["channel_id"]
		sent.append(label if result else f"{label} ({_('already queued')})")
	if not sent:
		frappe.throw(_("No Slack recipients given."))
	return ", ".join(sent)


def _resolve_workspace() -> str:
	"""Settings default → the single enabled workspace → a designer-actionable error."""
	default = frappe.db.get_single_value("Slack Bridge Settings", "default_workspace")
	if default:
		return default
	enabled = frappe.get_all(  # perm-safe: infrastructure lookup, no user data
		"Slack Workspace", filters={"enabled": 1}, pluck="name", limit=2
	)
	if len(enabled) == 1:
		return enabled[0]
	if not enabled:
		frappe.throw(_("No enabled Slack workspace is configured."))
	frappe.throw(
		_("Several Slack workspaces are enabled — set a default workspace in Slack Bridge Settings.")
	)


def _split(recipient: str) -> list[str]:
	return [t.strip() for t in (recipient or "").replace("\n", ",").split(",") if t.strip()]


def _resolve_target(target: str, workspace: str, client) -> dict | None:
	"""'#name' or a raw conversation id → channel; anything else (email / Frappe
	user / Slack user id) → DM."""
	if target.startswith("#") or re.fullmatch(r"[CGD][A-Z0-9]{6,}", target):
		return recipients.resolve_channel(target, workspace)
	return recipients.resolve_user_dm(target, workspace, client)


def _md_to_mrkdwn(body: str) -> str:
	"""Best-effort Markdown → Slack mrkdwn. Full fidelity is not promised — Slack
	has no headings or nested lists; we keep the text readable rather than exact."""
	text = (body or "").replace("\r\n", "\n")
	text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
	# links first: [label](url) -> <url|label>
	text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"<\2|\1>", text)
	# headings become bold lines
	text = re.sub(r"(?m)^#{1,4}\s+(.+)$", r"*\1*", text)
	# bold: **x** -> *x* (before italics so ** is consumed first)
	text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)
	# bullet dashes -> Slack-friendly bullets
	text = re.sub(r"(?m)^\s*-\s+", "• ", text)
	return text
