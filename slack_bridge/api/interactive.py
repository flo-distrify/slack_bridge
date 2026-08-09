# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack interactivity endpoint: buttons, shortcuts and modal submissions."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from slack_bridge.api import base


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(key="slack_interactive", limit=1200, seconds=60, ip_based=True)
def handle():
	try:
		workspace = base.get_workspace()
	except Exception:
		base.respond({"error": "unauthorized"}, status=401)
		return

	payload = base.parse_interactive_payload()
	kind = payload.get("type")

	if kind == "block_actions":
		handle_block_actions(workspace, payload)
	elif kind == "view_submission":
		handle_view_submission(workspace, payload)
	elif kind == "message_action":
		from slack_bridge.api.comm_log import handle_message_action

		handle_message_action(workspace, payload)
	elif kind == "block_suggestion":
		handle_block_suggestion(workspace, payload)
	elif kind == "view_closed":
		base.respond()
	else:
		base.respond()


def handle_block_suggestion(workspace, payload: dict) -> None:
	"""Route option lookups to whichever feature owns the action_id."""
	if (payload.get("action_id") or "").startswith("sbdf_"):
		from slack_bridge.api.dynamic_forms import handle_suggestion

		handle_suggestion(workspace, payload)
	else:
		from slack_bridge.api.comm_log import handle_block_suggestion as comm_log_suggestion

		comm_log_suggestion(workspace, payload)


# ----------------------------------------------------------------- button clicks


def handle_block_actions(workspace, payload: dict) -> None:
	action = (payload.get("actions") or [{}])[0]
	slack_user_id = (payload.get("user") or {}).get("id")

	try:
		value = json.loads(action.get("value") or "{}")
	except ValueError:
		value = {}

	if not value.get("action"):
		# Link buttons and anything we do not own need no server work.
		base.respond()
		return

	# A given button on a given message may only fire once per user.
	key = base.idempotency_key(
		(payload.get("message") or {}).get("ts"),
		action.get("action_id"),
		slack_user_id,
		value.get("action"),
	)

	log_name = base.claim(
		key,
		"Interactive",
		workspace.name,
		slack_user_id=slack_user_id,
		action=value.get("action"),
		reference_doctype=value.get("doctype"),
		reference_name=value.get("docname"),
	)

	if not log_name:
		base.respond()
		return

	base.store_payload(log_name, payload)

	mapping = base.resolve_user(workspace.name, slack_user_id)
	if not mapping:
		base.finish(log_name, "Ignored", "No linked account")
		base.respond(base.link_prompt())
		return

	frappe.db.set_value(
		"Slack Interaction Log", log_name, "frappe_user", mapping.user, update_modified=False
	)

	action_doc = frappe.get_cached_doc("Slack Action", value["action"])

	# A form-collecting action must open its modal inline: trigger_id expires in 3 seconds.
	if action_doc.collect_input_form:
		from slack_bridge.api.forms import open_form_modal

		try:
			open_form_modal(
				workspace=workspace,
				trigger_id=payload.get("trigger_id"),
				form_name=action_doc.collect_input_form,
				user=mapping.user,
				private_metadata={
					"action": value["action"],
					"doctype": value.get("doctype"),
					"docname": value.get("docname"),
					"channel": (payload.get("channel") or {}).get("id"),
					"message_ts": (payload.get("message") or {}).get("ts"),
					"response_url": payload.get("response_url"),
					"log": log_name,
				},
			)
			base.finish(log_name, "Processed", "Opened input modal")
		except Exception:
			frappe.log_error(
				title="Slack Bridge: could not open modal", message=frappe.get_traceback()
			)
			base.finish(log_name, "Failed", "Could not open modal")

		base.respond()
		return

	frappe.enqueue(
		"slack_bridge.api.actions.run",
		queue="short",
		enqueue_after_commit=True,
		workspace=workspace.name,
		action_name=value["action"],
		doctype=value.get("doctype"),
		docname=value.get("docname"),
		user=mapping.user,
		slack_user_id=slack_user_id,
		channel=(payload.get("channel") or {}).get("id"),
		message_ts=(payload.get("message") or {}).get("ts"),
		response_url=payload.get("response_url"),
		log_name=log_name,
	)

	base.respond()


# ------------------------------------------------------------- modal submissions


def handle_view_submission(workspace, payload: dict) -> None:
	from slack_bridge.api.forms import submit_form

	view = payload.get("view") or {}
	slack_user_id = (payload.get("user") or {}).get("id")

	try:
		metadata = json.loads(view.get("private_metadata") or "{}")
	except ValueError:
		metadata = {}

	# The dynamic-form picker stage only renders the next view — it writes nothing, and
	# claiming it would make a Slack retry of the same submission close the modal mid-flow.
	if view.get("callback_id") == "sb_dyn_pick":
		from slack_bridge.api.dynamic_forms import handle_pick_submission

		handle_pick_submission(workspace, payload)
		return

	mapping = base.resolve_user(workspace.name, slack_user_id)
	if not mapping:
		base.respond(
			{
				"response_action": "errors",
				"errors": {
					(view.get("blocks") or [{}])[0].get("block_id", "unknown"): _(
						"Your Slack account is not linked to an ERP user."
					)
				},
			}
		)
		return

	# Stage-2 dynamic-form views share the view id with their stage-1 picker, but Slack
	# issues a fresh hash on every response_action update, so the keys never collide.
	key = base.idempotency_key(view.get("id"), view.get("hash"), slack_user_id)
	log_name = base.claim(
		key,
		"View Submission",
		workspace.name,
		slack_user_id=slack_user_id,
		frappe_user=mapping.user,
		action=metadata.get("form") or metadata.get("action"),
		reference_doctype=metadata.get("doctype"),
		reference_name=metadata.get("docname"),
	)

	if not log_name:
		base.respond({"response_action": "clear"})
		return

	base.store_payload(log_name, payload)

	# Submissions run inline: Slack needs the validation verdict within three seconds,
	# and reporting field errors is only possible in this response.
	try:
		if view.get("callback_id") == "sb_comm_log":
			from slack_bridge.api.comm_log import submit_comm_log

			result = submit_comm_log(
				workspace=workspace,
				view=view,
				metadata=metadata,
				user=mapping.user,
				slack_user_id=slack_user_id,
				log_name=log_name,
			)
		elif view.get("callback_id") == "sb_dyn_form":
			from slack_bridge.api.dynamic_forms import submit_dynamic_form

			result = submit_dynamic_form(
				workspace=workspace,
				view=view,
				metadata=metadata,
				user=mapping.user,
				slack_user_id=slack_user_id,
				log_name=log_name,
			)
		else:
			result = submit_form(
				workspace=workspace,
				view=view,
				metadata=metadata,
				user=mapping.user,
				slack_user_id=slack_user_id,
				log_name=log_name,
			)
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title="Slack Bridge: form submission failed", message=frappe.get_traceback())
		base.finish(log_name, "Failed", str(e))
		base.respond(
			{
				"response_action": "errors",
				"errors": {"sb_error": str(e)[:150] or _("Could not save.")},
			}
		)
		return

	base.respond(result or {"response_action": "clear"})
