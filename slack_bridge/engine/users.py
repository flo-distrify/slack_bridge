# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack ↔ Frappe identity mapping and workspace directory sync."""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, now_datetime

SYNC_INTERVAL_HOURS = 24


def get_frappe_user(workspace: str, slack_user_id: str) -> str | None:
	"""Resolve a Slack user id to a Frappe user, if a mapping exists and is enabled."""
	return frappe.db.get_value(
		"Slack User",
		{"workspace": workspace, "slack_user_id": slack_user_id, "enabled": 1},
		"user",
	)


def get_mapping(workspace: str, slack_user_id: str):
	name = frappe.db.get_value(
		"Slack User", {"workspace": workspace, "slack_user_id": slack_user_id}, "name"
	)
	return frappe.get_doc("Slack User", name) if name else None


def sync_channels(workspace_name: str) -> int:
	"""Pull the conversation directory into Slack Channel records."""
	from slack_bridge.slack.client import SlackClient

	workspace = frappe.get_doc("Slack Workspace", workspace_name)
	client = SlackClient(workspace)
	count = 0

	for channel in client.list_conversations():
		name = f"{workspace_name}-{channel['id']}"
		values = {
			"channel_id": channel["id"],
			"channel_name": channel.get("name") or channel["id"],
			"workspace": workspace_name,
			"is_private": 1 if channel.get("is_private") else 0,
			"is_archived": 1 if channel.get("is_archived") else 0,
			"is_member": 1 if channel.get("is_member") else 0,
			"purpose": (channel.get("purpose") or {}).get("value"),
		}

		if frappe.db.exists("Slack Channel", name):
			doc = frappe.get_doc("Slack Channel", name)
			doc.update(values)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({"doctype": "Slack Channel", **values})
			doc.insert(ignore_permissions=True)

		count += 1

	frappe.db.set_value(
		"Slack Workspace", workspace_name, "last_connection_check", now_datetime(), update_modified=False
	)
	return count


def sync_users(workspace_name: str) -> int:
	"""Match enabled Frappe users to Slack accounts by email."""
	from slack_bridge.slack.client import SlackClient

	workspace = frappe.get_doc("Slack Workspace", workspace_name)
	client = SlackClient(workspace)

	# One users.list call is far cheaper than a lookupByEmail per user.
	by_email = {}
	for member in client.list_users():
		if member.get("deleted") or member.get("is_bot"):
			continue
		email = (member.get("profile") or {}).get("email")
		if email:
			by_email[email.lower()] = member

	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User"},
		fields=["name", "email"],
	)

	count = 0
	for user in users:
		email = (user.email or user.name or "").lower()
		member = by_email.get(email)
		if not member:
			continue

		if upsert_mapping(workspace_name, user.name, member):
			count += 1

	return count


def upsert_mapping(workspace_name: str, user: str, member: dict) -> bool:
	profile = member.get("profile") or {}
	values = {
		"slack_username": member.get("name"),
		"slack_real_name": profile.get("real_name"),
	}

	existing = frappe.db.get_value(
		"Slack User", {"workspace": workspace_name, "user": user}, "name"
	)

	if existing:
		doc = frappe.get_doc("Slack User", existing)
		# Never silently repoint a manual mapping.
		if doc.mapping_source == "Manual":
			return False
		doc.update(values)
		doc.slack_user_id = member["id"]
		doc.save(ignore_permissions=True)
		return True

	frappe.get_doc(
		{
			"doctype": "Slack User",
			"workspace": workspace_name,
			"user": user,
			"slack_user_id": member["id"],
			"mapping_source": "Email Match",
			**values,
		}
	).insert(ignore_permissions=True)
	return True


def on_user_change(doc, method=None):
	"""Try to map newly enabled system users without waiting for the next sync."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_test:
		return

	if doc.user_type != "System User" or not doc.enabled:
		return

	workspaces = frappe.get_all("Slack Workspace", filters={"enabled": 1}, pluck="name")
	if not workspaces:
		return

	for workspace in workspaces:
		if frappe.db.exists("Slack User", {"workspace": workspace, "user": doc.name}):
			continue

		frappe.enqueue(
			"slack_bridge.engine.users.map_single_user",
			queue="long",
			enqueue_after_commit=True,
			workspace=workspace,
			user=doc.name,
		)


def map_single_user(workspace: str, user: str) -> None:
	from slack_bridge.slack.client import SlackClient

	email = frappe.db.get_value("User", user, "email") or user
	try:
		member = SlackClient(workspace).lookup_user_by_email(email)
	except Exception:
		# A missing scope or a revoked token should not break user creation.
		return

	if member:
		upsert_mapping(workspace, user, member)


def refresh_stale_workspaces() -> None:
	"""Hourly: re-sync any workspace whose directory is older than a day."""
	cutoff = add_to_date(now_datetime(), hours=-SYNC_INTERVAL_HOURS)

	workspaces = frappe.get_all(
		"Slack Workspace",
		filters=[["enabled", "=", 1]],
		or_filters=[["last_connection_check", "<", cutoff], ["last_connection_check", "is", "not set"]],
		pluck="name",
	)

	for workspace in workspaces:
		frappe.enqueue(
			"slack_bridge.engine.users.sync_channels",
			queue="long",
			job_id=f"slack-bridge-sync-{workspace}",
			deduplicate=True,
			workspace_name=workspace,
		)
