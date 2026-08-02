# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Recipient resolution.

A recipient row resolves to a Slack conversation id — either a channel or a DM opened
with a mapped user. The Static / Document Field / Jinja triad is what makes rules
reusable: "notify the document's owner" is a Document Field row, not a hardcoded name.
"""

from __future__ import annotations

import frappe

from slack_bridge.engine.context import evaluate_condition, render


def resolve(rule, doc, client) -> list[dict]:
	"""Return [{"channel_id": ..., "label": ...}] for every recipient row that matches."""
	resolved = []
	seen = set()

	for row in rule.recipients:
		if not evaluate_condition(row.condition, doc):
			continue

		try:
			target = resolve_row(row, doc, rule, client)
		except Exception:
			frappe.log_error(
				title="Slack Bridge: recipient resolution failed",
				message=f"Rule: {rule.name}\nDocument: {doc.doctype} {doc.name}\n\n{frappe.get_traceback()}",
			)
			continue

		if target and target["channel_id"] not in seen:
			seen.add(target["channel_id"])
			resolved.append(target)

	return resolved


def resolve_row(row, doc, rule, client) -> dict | None:
	value = None

	if row.source == "Static":
		if row.recipient_type == "Channel":
			if not row.channel:
				return None
			channel_id = frappe.db.get_value("Slack Channel", row.channel, "channel_id")
			return {"channel_id": channel_id, "label": row.channel} if channel_id else None

		value = row.user
	elif row.source == "Document Field":
		value = doc.get(row.fieldname)
	elif row.source == "Jinja":
		value = (render(row.jinja_value, doc) or "").strip()

	if not value:
		return None

	if row.recipient_type == "Channel":
		return resolve_channel(value, rule.workspace)

	return resolve_user_dm(value, rule.workspace, client)


def resolve_channel(value: str, workspace: str) -> dict | None:
	"""Accept a Slack channel id, a #name, or a Slack Channel document name."""
	value = value.strip()

	if value.startswith("#"):
		value = value[1:]

	channel = frappe.db.get_value(
		"Slack Channel",
		{"workspace": workspace, "channel_id": value},
		["channel_id", "channel_name"],
		as_dict=True,
	) or frappe.db.get_value(
		"Slack Channel",
		{"workspace": workspace, "channel_name": value},
		["channel_id", "channel_name"],
		as_dict=True,
	)

	if channel:
		return {"channel_id": channel.channel_id, "label": f"#{channel.channel_name}"}

	# Not synced yet — a raw conversation id still works, Slack will validate it.
	if value.startswith(("C", "G", "D")):
		return {"channel_id": value, "label": value}

	return None


def resolve_user_dm(value: str, workspace: str, client) -> dict | None:
	"""Open (or reuse) a DM channel for a Frappe user, an email, or a Slack user id."""
	value = value.strip()

	slack_user_id = None

	if value.startswith("U") and " " not in value and "@" not in value:
		slack_user_id = value
	else:
		slack_user_id = get_slack_user_id(value, workspace, client)

	if not slack_user_id:
		return None

	try:
		channel_id = client.open_dm(slack_user_id)
	except Exception:
		frappe.log_error(
			title="Slack Bridge: could not open DM",
			message=f"Slack user: {slack_user_id}\n\n{frappe.get_traceback()}",
		)
		return None

	return {"channel_id": channel_id, "label": f"@{value}"}


def get_slack_user_id(user: str, workspace: str, client=None) -> str | None:
	"""Map a Frappe user (or email) to a Slack user id, learning the mapping on first use."""
	mapping = frappe.db.get_value(
		"Slack User",
		{"user": user, "workspace": workspace, "enabled": 1},
		"slack_user_id",
	)
	if mapping:
		return mapping

	email = user if "@" in user else frappe.db.get_value("User", user, "email")
	if not email:
		return None

	if not client:
		from slack_bridge.slack.client import SlackClient

		client = SlackClient(workspace)

	slack_user = client.lookup_user_by_email(email)
	if not slack_user:
		return None

	# Cache the discovered mapping so later sends skip the API call.
	frappe.get_doc(
		{
			"doctype": "Slack User",
			"user": frappe.db.get_value("User", {"email": email}, "name") or user,
			"workspace": workspace,
			"slack_user_id": slack_user["id"],
			"slack_username": slack_user.get("name"),
			"slack_real_name": (slack_user.get("profile") or {}).get("real_name"),
			"mapping_source": "Email Match",
		}
	).insert(ignore_permissions=True)

	return slack_user["id"]
