# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Execute a Slack Action against a document.

Everything here runs as the Frappe user the Slack account maps to — never as
Administrator — so document permissions, workflow rules and the version trail all
reflect the person who actually clicked the button. Slack is the interface; Frappe
remains the system of record.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from slack_bridge.api import base
from slack_bridge.engine.context import render
from slack_bridge.engine.render import resolved_blocks
from slack_bridge.slack.client import SlackClient, respond


def run(
	workspace: str,
	action_name: str,
	doctype: str,
	docname: str,
	user: str,
	slack_user_id: str,
	channel: str | None = None,
	message_ts: str | None = None,
	response_url: str | None = None,
	log_name: str | None = None,
	inputs: dict | None = None,
) -> None:
	"""Background entry point for a button click."""
	action = frappe.get_cached_doc("Slack Action", action_name)
	original_user = frappe.session.user

	try:
		frappe.set_user(user)

		if not frappe.db.exists(doctype, docname):
			raise frappe.DoesNotExistError(_("{0} {1} no longer exists.").format(doctype, docname))

		if action.required_role and action.required_role not in frappe.get_roles(user):
			raise frappe.PermissionError(
				_("You need the {0} role to do that.").format(action.required_role)
			)

		doc = frappe.get_doc(doctype, docname)
		doc.check_permission("write")

		summary = execute(action, doc, inputs or {})
		frappe.db.commit()

	except Exception as e:
		frappe.db.rollback()
		message = str(e) or e.__class__.__name__
		frappe.set_user(original_user)

		base.finish(log_name, "Failed", message)
		notify_error(response_url, workspace, channel, slack_user_id, message)

		if not isinstance(e, frappe.PermissionError | frappe.DoesNotExistError | frappe.ValidationError):
			frappe.log_error(
				title="Slack Bridge: action failed",
				message=f"Action: {action_name}\nDocument: {doctype} {docname}\n\n{frappe.get_traceback()}",
			)
		return

	finally:
		if frappe.session.user != original_user:
			frappe.set_user(original_user)

	base.finish(log_name, "Processed", summary)

	audit = build_audit_line(action, doc, user, summary)
	if action.update_original_message and channel and message_ts:
		update_message(workspace, channel, message_ts, audit, response_url)
	elif response_url:
		respond(response_url, {"response_type": "ephemeral", "text": audit})


def execute(action, doc, inputs: dict) -> str:
	"""Perform the configured change and return a short human summary."""
	context = {"input": frappe._dict(inputs), "slack_user": frappe.session.user}

	if action.action_type == "Workflow Action":
		from frappe.model.workflow import apply_workflow

		apply_workflow(doc, action.workflow_action)
		return _("{0} applied").format(action.workflow_action)

	if action.action_type == "Set Field Value":
		value = render(action.value_to_set, doc, context) if action.value_to_set else None
		doc.set(action.field_to_set, value)
		doc.save()

		if action.submit_after_update and doc.docstatus == 0:
			doc.submit()

		return _("{0} set to {1}").format(action.field_to_set, value or _("(empty)"))

	if action.action_type == "Run Server Script":
		script = frappe.get_doc("Server Script", action.server_script)
		if script.script_type == "API":
			script.execute_method()
		else:
			script.execute_doc(doc)
		return _("{0} ran").format(action.server_script)

	if action.action_type == "Run Method":
		method = frappe.get_attr(action.method)
		method(doc=doc, **context)
		return _("{0} ran").format(action.method)

	raise frappe.ValidationError(_("Unsupported action type {0}").format(action.action_type))


def build_audit_line(action, doc, user: str, summary: str) -> str:
	full_name = frappe.db.get_value("User", user, "full_name") or user

	if action.success_message:
		try:
			return render(action.success_message, doc, {"slack_user": user, "summary": summary})
		except Exception:
			pass

	return f":white_check_mark: {summary} — {full_name}"


def update_message(
	workspace: str, channel: str, message_ts: str, audit: str, response_url: str | None
) -> None:
	"""Strip the buttons from the original message and record who acted.

	This is what stops a second person clicking an approval that already happened.
	"""
	log = frappe.db.get_value(
		"Slack Message Log",
		{"channel_id": channel, "message_ts": message_ts},
		["name", "payload"],
		as_dict=True,
	)

	if log and log.payload:
		try:
			payload = json.loads(log.payload)
			blocks = resolved_blocks(payload.get("blocks") or [], audit)

			client = SlackClient(workspace)
			client.update_message(channel=channel, ts=message_ts, blocks=blocks, text=audit)

			payload["blocks"] = blocks
			frappe.db.set_value(
				"Slack Message Log", log.name, "payload", json.dumps(payload), update_modified=False
			)
			return
		except Exception:
			frappe.log_error(
				title="Slack Bridge: could not update message", message=frappe.get_traceback()
			)

	# No stored copy of the message — fall back to the response_url, which can replace
	# a message we did not compose (valid for 30 minutes).
	if response_url:
		respond(response_url, {"replace_original": False, "text": audit})


def notify_error(
	response_url: str | None, workspace: str, channel: str | None, slack_user_id: str, message: str
) -> None:
	"""Errors go to the person who clicked, never to the whole channel."""
	text = f":warning: {message}"

	if response_url:
		respond(response_url, {"response_type": "ephemeral", "replace_original": False, "text": text})
		return

	if channel and slack_user_id:
		try:
			SlackClient(workspace).post_ephemeral(channel=channel, user=slack_user_id, text=text)
		except Exception:
			pass
