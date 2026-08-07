# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack app manifest generation.

Each customer creates their own Slack app from this manifest. That keeps the install
"internal" (exempt from Slack's 2025 non-Marketplace rate limits), needs no central
OAuth relay, and lets the Request URLs point at the customer's own site.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import frappe

BOT_SCOPES = [
	"app_mentions:read",
	"channels:join",
	"channels:read",
	"chat:write",
	"chat:write.public",
	"commands",
	"files:read",
	"groups:read",
	"im:read",
	"im:write",
	"links:read",
	"links:write",
	"reactions:write",
	"users:read",
	"users:read.email",
]

SUBSCRIBED_EVENTS = [
	"app_home_opened",
	"app_mention",
	"link_shared",
]


def endpoint_url(path: str, token: str) -> str:
	base = frappe.utils.get_url().rstrip("/")
	return f"{base}/api/method/slack_bridge.api.{path}?token={token}"


def get_site_domain() -> str:
	host = urlparse(frappe.utils.get_url()).hostname or ""
	return host


def build_manifest(workspace) -> dict:
	"""Build the Slack app manifest for a Slack Workspace document."""
	token = workspace.endpoint_token
	commands = get_slash_commands(token)

	manifest = {
		"display_information": {
			"name": (workspace.slack_app_name or "ERP Bridge")[:35],
			"description": (workspace.slack_app_description or "Connects Slack with your Frappe/ERPNext site")[
				:140
			],
			"background_color": "#1f3b57",
		},
		"features": {
			"bot_user": {
				"display_name": (workspace.bot_display_name or "ERP Bridge")[:80],
				"always_online": True,
			},
			"unfurl_domains": [get_site_domain()] if get_site_domain() else [],
		},
		"oauth_config": {"scopes": {"bot": BOT_SCOPES}},
		"settings": {
			"event_subscriptions": {
				"request_url": endpoint_url("events.handle", token),
				"bot_events": SUBSCRIBED_EVENTS,
			},
			"interactivity": {
				"is_enabled": True,
				"request_url": endpoint_url("interactive.handle", token),
				# external_select option lookups (block_suggestion) are delivered to a
				# separate Options Load URL — without it, pickers silently stay empty.
				"message_menu_options_url": endpoint_url("interactive.handle", token),
			},
			"org_deploy_enabled": False,
			"socket_mode_enabled": False,
			"token_rotation_enabled": False,
		},
	}

	if commands:
		manifest["features"]["slash_commands"] = commands

	shortcuts = get_message_shortcuts(workspace.name)
	if shortcuts:
		manifest["features"]["shortcuts"] = shortcuts

	return manifest


def get_slash_commands(token: str) -> list[dict]:
	"""One manifest entry per distinct command configured in Slack Command Route."""
	if not frappe.db.table_exists("Slack Command Route"):
		return []

	rows = frappe.get_all(
		"Slack Command Route",
		filters={"enabled": 1},
		fields=["command", "subcommand"],
	)

	# Several routes share one command and differ only by subcommand, but Slack registers
	# the command itself — so describe the command generically and list the subcommands
	# it currently accepts as the usage hint.
	subcommands = {}
	for row in rows:
		command = (row.command or "").strip()
		if not command:
			continue
		if not command.startswith("/"):
			command = "/" + command

		subcommands.setdefault(command, set())
		if row.subcommand:
			subcommands[command].add(row.subcommand)

	commands = []
	for command, subs in subcommands.items():
		hint = " | ".join(sorted(subs)[:8]) if subs else "[arguments]"
		commands.append(
			{
				"command": command,
				"url": endpoint_url("commands.handle", token),
				"description": "Work with your ERP from Slack"[:100],
				"usage_hint": hint[:100],
				"should_escape": False,
			}
		)

	return commands


def get_message_shortcuts(workspace_name: str) -> list[dict]:
	"""One manifest entry per enabled Slack Communication Shortcut of this workspace.

	Shortcuts arrive on the interactivity Request URL, so they carry no URL of their own.
	"""
	if not frappe.db.table_exists("Slack Communication Shortcut"):
		return []

	rows = frappe.get_all(
		"Slack Communication Shortcut",
		filters={"enabled": 1, "workspace": workspace_name},
		fields=["shortcut_label", "shortcut_description", "callback_id"],
		order_by="creation asc",
	)

	return [
		{
			"name": row.shortcut_label[:24],
			"type": "message",
			"callback_id": row.callback_id[:255],
			"description": (row.shortcut_description or "Log this message to your ERP")[:50],
		}
		for row in rows
	]


def manifest_json(workspace) -> str:
	return json.dumps(build_manifest(workspace), indent=2)
