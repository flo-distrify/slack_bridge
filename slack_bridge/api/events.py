# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack Events API endpoint."""

from __future__ import annotations

import frappe
from frappe.rate_limiter import rate_limit

from slack_bridge.api import base


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="slack_events", limit=1200, seconds=60, ip_based=True)
def handle():
	try:
		workspace = base.get_workspace()
	except Exception:
		base.respond({"error": "unauthorized"}, status=401)
		return

	payload = base.parse_event_payload()
	kind = payload.get("type")

	# One-time endpoint ownership check when the Request URL is saved in Slack.
	if kind == "url_verification":
		base.respond({"challenge": payload.get("challenge")})
		return

	if kind != "event_callback":
		base.respond()
		return

	event = payload.get("event") or {}

	# Slack redelivers on slow acks; the event_id makes replays harmless.
	key = base.idempotency_key(payload.get("event_id"), event.get("type"), event.get("event_ts"))
	log_name = base.claim(
		key,
		"Event",
		workspace.name,
		slack_user_id=event.get("user"),
		action=event.get("type"),
	)

	if not log_name:
		base.respond()
		return

	base.store_payload(log_name, payload)

	frappe.enqueue(
		"slack_bridge.api.events.process",
		queue="short",
		enqueue_after_commit=True,
		workspace=workspace.name,
		event=event,
		log_name=log_name,
	)

	base.respond()


def process(workspace: str, event: dict, log_name: str) -> None:
	"""Background half — the ack has already gone out."""
	handler = {
		"link_shared": handle_link_shared,
		"app_home_opened": handle_app_home,
		"app_mention": handle_app_mention,
	}.get(event.get("type"))

	if not handler:
		base.finish(log_name, "Ignored", f"No handler for {event.get('type')}")
		return

	try:
		result = handler(workspace, event)
		base.finish(log_name, "Processed", result)
	except Exception:
		frappe.log_error(
			title="Slack Bridge: event handler failed",
			message=f"Event: {event.get('type')}\n\n{frappe.get_traceback()}",
		)
		base.finish(log_name, "Failed", frappe.get_traceback(with_context=False))


def handle_link_shared(workspace: str, event: dict) -> str:
	from slack_bridge.api.unfurl import unfurl_links

	return unfurl_links(workspace, event)


def handle_app_home(workspace: str, event: dict) -> str:
	from slack_bridge.api.home import publish_home

	return publish_home(workspace, event.get("user"))


def handle_app_mention(workspace: str, event: dict) -> str:
	"""Reply to @mentions with the command help, so the bot is never a dead end."""
	from slack_bridge.api.commands import build_help_blocks
	from slack_bridge.slack.client import SlackClient

	client = SlackClient(workspace)
	blocks = build_help_blocks(workspace)

	client.post_ephemeral(
		channel=event.get("channel"),
		user=event.get("user"),
		text="Here is what I can do.",
		blocks=blocks,
	)
	return "Replied with help"
