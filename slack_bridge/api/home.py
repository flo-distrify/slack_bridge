# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""The bot's App Home tab: link status and what this workspace is wired up to do."""

from __future__ import annotations

import frappe
from frappe import _

from slack_bridge.engine.users import get_frappe_user
from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient


def publish_home(workspace: str, slack_user_id: str) -> str:
	if not slack_user_id:
		return "No user"

	user = get_frappe_user(workspace, slack_user_id)
	blocks = [bk.section("*" + _("ERP Bridge") + "*")]

	if user:
		full_name = frappe.db.get_value("User", user, "full_name") or user
		blocks.append(bk.section(_("You are linked to *{0}*.").format(full_name)))
	else:
		blocks.append(
			bk.section(
				_("Your Slack account is not linked to an ERP user yet, so buttons and forms will not run.")
			)
		)
		blocks.append(bk.context(_("Run the `link` subcommand of your ERP slash command to connect.")))

	blocks.append(bk.divider())

	commands = frappe.get_all(
		"Slack Command Route",
		filters={"workspace": workspace, "enabled": 1},
		fields=["command", "subcommand", "help_text"],
		order_by="command asc, subcommand asc",
		limit=20,
	)

	if commands:
		lines = [
			f"`{c.command} {c.subcommand}`".replace("  ", " ") + (f" — {c.help_text}" if c.help_text else "")
			for c in commands
		]
		blocks.append(bk.section("*" + _("Commands") + "*\n" + "\n".join(lines)))

	rules = frappe.db.count("Slack Notification Rule", {"workspace": workspace, "enabled": 1})
	blocks.append(bk.context(_("{0} notification rules are active.").format(rules)))

	SlackClient(workspace).publish_home(
		slack_user_id, {"type": "home", "blocks": bk.clamp_blocks(blocks)}
	)

	return "Published home"
