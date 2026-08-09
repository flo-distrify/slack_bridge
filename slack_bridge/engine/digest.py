# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Daily Digest rules: one DM per user summarising the documents grouped on them.

Unlike every other event, a digest is not tied to a single document. The hourly
scheduler finds due rules, collects matching documents, groups them by a user
field (e.g. ToDo.allocated_to), and sends each mapped user one message rendered
against their whole group. Users without a Slack mapping are skipped quietly.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import get_time, get_url_to_list, now_datetime, nowdate

from slack_bridge.engine import outbox, recipients
from slack_bridge.engine.context import evaluate_condition
from slack_bridge.engine.render import render_digest_message
from slack_bridge.engine.rules import is_enabled
from slack_bridge.slack.client import SlackClient

DIGEST_EVENT = "Daily Digest"
DEFAULT_SEND_AFTER = "08:00:00"
# One query guard and one message guard: a digest is a summary, not an export.
MAX_DOCUMENTS = 1000
MAX_PER_USER = 50


def trigger_digest_rules() -> None:
	"""Hourly tick: send every due digest rule once per day, after its send time."""
	if not is_enabled():
		return

	for name in frappe.get_all(
		"Slack Notification Rule",
		filters={"enabled": 1, "event": DIGEST_EVENT},
		pluck="name",
	):
		try:
			run_digest_rule(name)
		except Exception:
			frappe.log_error(
				title="Slack Bridge: digest rule failed",
				message=f"Rule: {name}\n\n{frappe.get_traceback()}",
			)


def run_digest_rule(rule_name: str, force: bool = False) -> int:
	"""Send one rule's digests. Returns the number of messages queued.

	`force` (used by Send Test Message) skips the once-a-day and send-time gates.
	"""
	rule = frappe.get_doc("Slack Notification Rule", rule_name)
	if rule.event != DIGEST_EVENT or (not rule.enabled and not force):
		return 0

	today = nowdate()
	if not force:
		if rule.last_digest_date and str(rule.last_digest_date) == today:
			return 0
		if now_datetime().time() < get_time(rule.digest_send_after or DEFAULT_SEND_AFTER):
			return 0

	workspace = frappe.get_cached_doc("Slack Workspace", rule.workspace)
	if not workspace.enabled:
		return 0

	client = SlackClient(workspace)
	queued = 0

	for user, docs in collect_groups(rule).items():
		target = recipients.resolve_user_dm(user, rule.workspace, client)
		if not target:
			# Not everyone is on Slack — an unmapped user is expected, not an error.
			continue

		blocks, text = render_digest_message(rule, build_context(rule, user, docs))
		outbox.queue(
			workspace=rule.workspace,
			channel_id=target["channel_id"],
			blocks=blocks,
			text=text,
			rule=rule.name,
			reference_doctype=rule.document_type,
			thread_mode="New Message",
			# The date keys the dedupe, so an identical list still sends again tomorrow.
			event_method=f"digest:{today}",
		)
		queued += 1

	# "Ran today" also covers the nothing-to-send case: no re-checks until tomorrow.
	if not force:
		frappe.db.set_value(
			"Slack Notification Rule", rule.name, "last_digest_date", today, update_modified=False
		)

	return queued


def collect_groups(rule) -> dict[str, list]:
	"""Group matching documents by the rule's user field, most recently modified first."""
	names = frappe.get_all(
		rule.document_type,
		filters=parse_filters(rule.digest_filters),
		pluck="name",
		order_by="modified desc",
		limit=MAX_DOCUMENTS,
	)

	groups: dict[str, list] = {}
	for name in names:
		doc = frappe.get_doc(rule.document_type, name)
		if not evaluate_condition(rule.condition, doc):
			continue

		user = (doc.get(rule.digest_group_by) or "").strip()
		if user:
			groups.setdefault(user, []).append(doc)

	return groups


def parse_filters(raw: str | None):
	if not raw or not raw.strip():
		return {}
	# Validated on save; a value broken since then should fail loudly, not send everything.
	return json.loads(raw)


def build_context(rule, user: str, docs: list) -> dict:
	return {
		"docs": docs[:MAX_PER_USER],
		"count": len(docs),
		"user": user,
		"doc_url": get_url_to_list(rule.document_type),
	}
