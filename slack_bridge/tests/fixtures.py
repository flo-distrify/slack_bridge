# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Test helpers: a workspace, a channel and a fake Slack transport."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe

WORKSPACE = "Test Workspace"
CHANNEL_ID = "C0TEST0001"
SIGNING_SECRET = "test-signing-secret"
BOT_TOKEN = "xoxb-test-token"


class FakeResponse:
	def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None):
		self._payload = payload
		self.status_code = status_code
		self.headers = headers or {}
		self.text = json.dumps(payload)

	def json(self):
		return self._payload


def fake_slack(payload: dict | None = None, status_code: int = 200, headers: dict | None = None):
	"""Patch the HTTP layer so no test ever reaches Slack."""
	response = FakeResponse(
		payload if payload is not None else {"ok": True, "ts": "1700000000.000100", "channel": CHANNEL_ID},
		status_code=status_code,
		headers=headers,
	)
	return patch("slack_bridge.slack.client.requests.post", return_value=response)


def ensure_workspace(name: str = WORKSPACE):
	if frappe.db.exists("Slack Workspace", name):
		return frappe.get_doc("Slack Workspace", name)

	workspace = frappe.get_doc(
		{
			"doctype": "Slack Workspace",
			"workspace_name": name,
			"enabled": 1,
			"bot_token": BOT_TOKEN,
			"signing_secret": SIGNING_SECRET,
			"team_id": "T0TEST",
			"team_name": "Test Team",
		}
	)
	workspace.insert(ignore_permissions=True)
	return workspace


def ensure_channel(workspace: str = WORKSPACE, channel_id: str = CHANNEL_ID, channel_name="general"):
	name = f"{workspace}-{channel_id}"
	if frappe.db.exists("Slack Channel", name):
		return name

	frappe.get_doc(
		{
			"doctype": "Slack Channel",
			"channel_id": channel_id,
			"channel_name": channel_name,
			"workspace": workspace,
			"is_member": 1,
		}
	).insert(ignore_permissions=True)

	return name


def ensure_rule(title: str, **overrides):
	if frappe.db.exists("Slack Notification Rule", title):
		frappe.delete_doc("Slack Notification Rule", title, force=True, ignore_permissions=True)

	values = {
		"doctype": "Slack Notification Rule",
		"title": title,
		"enabled": 1,
		"workspace": WORKSPACE,
		"document_type": "ToDo",
		"event": "New Document",
		"message_mode": "Simple",
		"subject": "New task",
		"message": "{{ doc.description }}",
		"recipients": [
			{"recipient_type": "Channel", "source": "Static", "channel": ensure_channel()}
		],
	}
	values.update(overrides)

	rule = frappe.get_doc(values)
	rule.insert(ignore_permissions=True)
	return rule


def make_todo(description: str = "Check the shipment"):
	todo = frappe.get_doc(
		{"doctype": "ToDo", "description": description, "status": "Open"}
	)
	todo.insert(ignore_permissions=True)
	return todo
