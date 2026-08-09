# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slash command endpoint and the built-in commands."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from slack_bridge.api import base
from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="slack_commands", limit=600, seconds=60, ip_based=True)
def handle():
	try:
		workspace = base.get_workspace()
	except Exception:
		base.respond({"error": "unauthorized"}, status=401)
		return

	command = (frappe.form_dict.get("command") or "").strip().lower()
	text = (frappe.form_dict.get("text") or "").strip()
	slack_user_id = frappe.form_dict.get("user_id")
	channel_id = frappe.form_dict.get("channel_id")
	channel_name = frappe.form_dict.get("channel_name")
	trigger_id = frappe.form_dict.get("trigger_id")
	response_url = frappe.form_dict.get("response_url")

	subcommand, _sep, arguments = text.partition(" ")
	subcommand = subcommand.strip().lower()
	arguments = arguments.strip()

	route = find_route(workspace.name, command, subcommand)

	if not route:
		base.respond(
			{
				"response_type": "ephemeral",
				"text": _("I don't know `{0} {1}`.").format(command, subcommand),
				"blocks": build_help_blocks(workspace.name),
			}
		)
		return

	key = base.idempotency_key(
		frappe.form_dict.get("trigger_id"), command, text, slack_user_id
	)
	log_name = base.claim(
		key,
		"Command",
		workspace.name,
		slack_user_id=slack_user_id,
		action=f"{command} {subcommand}".strip(),
	)

	if not log_name:
		# A Slack retry of an already-claimed command must not print "{}" either.
		base.respond_empty()
		return

	mapping = base.resolve_user(workspace.name, slack_user_id, require_actions=False)

	# `link` is how an unmapped person gets mapped, so it can never require a mapping.
	if route.require_account_link and route.builtin != "link" and not mapping:
		base.finish(log_name, "Ignored", "No linked account")
		base.respond(
			{
				"response_type": "ephemeral",
				"text": _("Your Slack account is not linked yet. Run `{0} link` first.").format(command),
			}
		)
		return

	user = mapping.user if mapping else None

	if route.required_role and user and route.required_role not in frappe.get_roles(user):
		base.finish(log_name, "Ignored", "Missing role")
		base.respond(
			{"response_type": "ephemeral", "text": _("You need the {0} role for that.").format(route.required_role)}
		)
		return

	context = frappe._dict(
		workspace=workspace,
		command=command,
		subcommand=subcommand,
		arguments=arguments,
		slack_user_id=slack_user_id,
		channel_id=channel_id,
		channel_name=channel_name,
		trigger_id=trigger_id,
		response_url=response_url,
		user=user,
		log_name=log_name,
	)

	try:
		if route.route_type == "Open Form":
			result = run_open_form(route, context)
		elif route.route_type == "Open Dynamic Form":
			result = run_open_dynamic_form(route, context)
		elif route.route_type == "Built-in":
			result = run_builtin(route, context)
		else:
			result = run_background(route, context)
	except Exception as e:
		frappe.log_error(title="Slack Bridge: command failed", message=frappe.get_traceback())
		base.finish(log_name, "Failed", str(e))
		base.respond({"response_type": "ephemeral", "text": f":warning: {e}"})
		return

	base.finish(log_name, "Processed", route.name)
	if result:
		base.respond(result)
	else:
		# A modal was opened; any body — even "{}" — would render as a channel message.
		base.respond_empty()


def find_route(workspace: str, command: str, subcommand: str):
	"""Exact subcommand match wins; otherwise fall back to the catch-all route."""
	name = frappe.db.get_value(
		"Slack Command Route",
		{"workspace": workspace, "command": command, "subcommand": subcommand, "enabled": 1},
		"name",
	)

	if not name:
		name = frappe.db.get_value(
			"Slack Command Route",
			{"workspace": workspace, "command": command, "subcommand": "", "enabled": 1},
			"name",
		)

	return frappe.get_cached_doc("Slack Command Route", name) if name else None


# --------------------------------------------------------------- route runners


def run_open_form(route, context) -> dict | None:
	from slack_bridge.api.forms import open_form_modal

	if not context.user:
		return {"response_type": "ephemeral", "text": _("Link your account first.")}

	open_form_modal(
		workspace=context.workspace,
		trigger_id=context.trigger_id,
		form_name=route.form,
		user=context.user,
		private_metadata={
			"channel": context.channel_id,
			"response_url": context.response_url,
			"docname": context.arguments or None,
			"doctype": frappe.db.get_value("Slack Form", route.form, "document_type"),
		},
	)
	# Slack shows the modal; an empty 200 avoids a duplicate message in the channel.
	return None


def run_open_dynamic_form(route, context) -> dict | None:
	from slack_bridge.api.dynamic_forms import open_picker

	if not context.user:
		return {"response_type": "ephemeral", "text": _("Link your account first.")}

	open_picker(
		workspace=context.workspace,
		trigger_id=context.trigger_id,
		form_name=route.dynamic_form,
		user=context.user,
		context=context,
	)
	# Slack shows the modal; an empty 200 avoids a duplicate message in the channel.
	return None


def run_background(route, context) -> dict:
	frappe.enqueue(
		"slack_bridge.api.commands.execute_route",
		queue="short",
		enqueue_after_commit=True,
		route_name=route.name,
		user=context.user,
		arguments=context.arguments,
		channel_id=context.channel_id,
		slack_user_id=context.slack_user_id,
		response_url=context.response_url,
		workspace=context.workspace.name,
	)

	return {"response_type": "ephemeral", "text": _("Working on it…")}


def execute_route(
	route_name: str,
	user: str,
	arguments: str,
	channel_id: str,
	slack_user_id: str,
	response_url: str,
	workspace: str,
) -> None:
	from slack_bridge.slack.client import respond as post_response

	route = frappe.get_cached_doc("Slack Command Route", route_name)
	original_user = frappe.session.user

	try:
		if user:
			frappe.set_user(user)

		if route.route_type == "Run Method":
			result = frappe.get_attr(route.method)(
				arguments=arguments,
				slack_user_id=slack_user_id,
				channel_id=channel_id,
				workspace=workspace,
			)
		else:
			script = frappe.get_doc("Server Script", route.server_script)
			script.execute_method()
			result = frappe.local.response.get("message")

		text = result if isinstance(result, str) else frappe.as_json(result) if result else _("Done.")
		post_response(response_url, {"response_type": "ephemeral", "text": text[:3000]})

	except Exception as e:
		frappe.log_error(title="Slack Bridge: command job failed", message=frappe.get_traceback())
		post_response(response_url, {"response_type": "ephemeral", "text": f":warning: {e}"})
	finally:
		frappe.set_user(original_user)


# ------------------------------------------------------------------- built-ins


def run_builtin(route, context) -> dict:
	handler = {
		"subscribe": builtin_subscribe,
		"unsubscribe": builtin_unsubscribe,
		"subscriptions": builtin_subscriptions,
		"link": builtin_link,
		"whoami": builtin_whoami,
		"help": builtin_help,
	}.get(route.builtin)

	if not handler:
		return {"response_type": "ephemeral", "text": _("Unknown built-in {0}").format(route.builtin)}

	return handler(context)


def ensure_channel(workspace_name: str, channel_id: str, channel_name: str | None) -> str:
	"""Make sure a Slack Channel record exists for the channel the command came from."""
	name = f"{workspace_name}-{channel_id}"

	if frappe.db.exists("Slack Channel", name):
		return name

	frappe.get_doc(
		{
			"doctype": "Slack Channel",
			"channel_id": channel_id,
			"channel_name": channel_name or channel_id,
			"workspace": workspace_name,
			"is_member": 1,
		}
	).insert(ignore_permissions=True)

	return name


def find_rule(workspace: str, query: str):
	if not query:
		return None

	name = frappe.db.get_value(
		"Slack Notification Rule", {"workspace": workspace, "title": query}, "name"
	)
	if name:
		return name

	matches = frappe.get_all(
		"Slack Notification Rule",
		filters=[["workspace", "=", workspace], ["title", "like", f"%{query}%"]],
		pluck="name",
		limit=2,
	)

	return matches[0] if len(matches) == 1 else None


def builtin_subscribe(context) -> dict:
	rule_name = find_rule(context.workspace.name, context.arguments)
	if not rule_name:
		return {
			"response_type": "ephemeral",
			"text": _("Name exactly one rule, e.g. `subscribe Large Orders`. Use `subscriptions` to list them."),
		}

	channel = ensure_channel(context.workspace.name, context.channel_id, context.channel_name)
	rule = frappe.get_doc("Slack Notification Rule", rule_name)

	for row in rule.recipients:
		if row.source == "Static" and row.recipient_type == "Channel" and row.channel == channel:
			return {"response_type": "ephemeral", "text": _("This channel already gets {0}.").format(rule.title)}

	rule.append("recipients", {"recipient_type": "Channel", "source": "Static", "channel": channel})
	rule.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"response_type": "in_channel",
		"text": _("This channel will now receive *{0}*.").format(rule.title),
	}


def builtin_unsubscribe(context) -> dict:
	rule_name = find_rule(context.workspace.name, context.arguments)
	if not rule_name:
		return {"response_type": "ephemeral", "text": _("Name exactly one rule to unsubscribe from.")}

	channel = f"{context.workspace.name}-{context.channel_id}"
	rule = frappe.get_doc("Slack Notification Rule", rule_name)

	remaining = [
		row
		for row in rule.recipients
		if not (row.source == "Static" and row.recipient_type == "Channel" and row.channel == channel)
	]

	if len(remaining) == len(rule.recipients):
		return {"response_type": "ephemeral", "text": _("This channel is not subscribed to {0}.").format(rule.title)}

	if not remaining:
		return {
			"response_type": "ephemeral",
			"text": _("This is the only recipient of {0}; disable the rule in the ERP instead.").format(rule.title),
		}

	rule.recipients = remaining
	rule.save(ignore_permissions=True)
	frappe.db.commit()

	return {"response_type": "in_channel", "text": _("Unsubscribed from *{0}*.").format(rule.title)}


def builtin_subscriptions(context) -> dict:
	channel = f"{context.workspace.name}-{context.channel_id}"

	subscribed = frappe.get_all(
		"Slack Notification Recipient",
		filters={"channel": channel, "parenttype": "Slack Notification Rule"},
		pluck="parent",
	)

	rules = frappe.get_all(
		"Slack Notification Rule",
		filters={"workspace": context.workspace.name, "enabled": 1},
		fields=["name", "title", "document_type", "event"],
		order_by="title asc",
	)

	lines = []
	for rule in rules:
		marker = ":white_check_mark:" if rule.name in subscribed else ":heavy_minus_sign:"
		lines.append(f"{marker} *{rule.title}* — {rule.document_type} / {rule.event}")

	if not lines:
		return {"response_type": "ephemeral", "text": _("No notification rules are configured yet.")}

	return {
		"response_type": "ephemeral",
		"text": _("Notification rules"),
		"blocks": [
			bk.section("*" + _("Notification rules for this workspace") + "*\n" + "\n".join(lines)),
			bk.context(_("Use `subscribe <rule name>` in a channel to add it there.")),
		],
	}


def builtin_link(context) -> dict:
	"""Self-service account linking by verified Slack email."""
	if context.user:
		return {"response_type": "ephemeral", "text": _("You are already linked to {0}.").format(context.user)}

	client = SlackClient(context.workspace)

	try:
		info = client.call("users.info", user=context.slack_user_id, use_json=False)
	except Exception:
		return {"response_type": "ephemeral", "text": _("Could not read your Slack profile.")}

	email = ((info.get("user") or {}).get("profile") or {}).get("email")
	if not email:
		return {
			"response_type": "ephemeral",
			"text": _("Slack did not share your email, so I cannot match you automatically. Ask an administrator to map you."),
		}

	user = frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")
	if not user:
		return {
			"response_type": "ephemeral",
			"text": _("No enabled ERP user has the email {0}.").format(email),
		}

	if frappe.db.exists("Slack User", {"workspace": context.workspace.name, "user": user}):
		return {"response_type": "ephemeral", "text": _("{0} is already linked to another Slack account.").format(user)}

	member = (info.get("user") or {})
	frappe.get_doc(
		{
			"doctype": "Slack User",
			"workspace": context.workspace.name,
			"user": user,
			"slack_user_id": context.slack_user_id,
			"slack_username": member.get("name"),
			"slack_real_name": (member.get("profile") or {}).get("real_name"),
			"mapping_source": "Account Link",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	return {"response_type": "ephemeral", "text": _("Linked to *{0}*. You can now act on documents from Slack.").format(user)}


def builtin_whoami(context) -> dict:
	if not context.user:
		return {"response_type": "ephemeral", "text": _("You are not linked. Run `link` to connect your account.")}

	roles = ", ".join(sorted(frappe.get_roles(context.user))[:10])
	return {
		"response_type": "ephemeral",
		"blocks": [bk.section(_("You are *{0}*.").format(context.user)), bk.context(roles)],
	}


def builtin_help(context) -> dict:
	return {"response_type": "ephemeral", "blocks": build_help_blocks(context.workspace.name)}


def build_help_blocks(workspace: str) -> list[dict]:
	routes = frappe.get_all(
		"Slack Command Route",
		filters={"workspace": workspace, "enabled": 1},
		fields=["command", "subcommand", "help_text"],
		order_by="command asc, subcommand asc",
	)

	if not routes:
		return [bk.section(_("No commands are configured yet."))]

	lines = []
	for route in routes:
		usage = f"`{route.command} {route.subcommand}`".replace("  ", " ")
		lines.append(f"{usage} — {route.help_text or ''}".strip(" —"))

	return [bk.section("*" + _("Available commands") + "*\n" + "\n".join(lines))]
