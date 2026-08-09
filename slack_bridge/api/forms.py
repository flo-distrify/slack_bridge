# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Slack modals backed by Slack Form configuration."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from slack_bridge.api import base
from slack_bridge.engine.context import render
from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient

CALLBACK_ID = "sb_form"


# ------------------------------------------------------------------ modal build


def build_view(
	form,
	doc=None,
	private_metadata: dict | None = None,
	user: str | None = None,
	prefills: dict | None = None,
) -> dict:
	context = {"slack_user": user}

	view = {
		"type": "modal",
		"callback_id": CALLBACK_ID,
		"title": {"type": "plain_text", "text": form.get_modal_title()},
		"submit": {"type": "plain_text", "text": (form.submit_label or _("Submit"))[:24]},
		"close": {"type": "plain_text", "text": _("Cancel")},
		"blocks": [build_input(row, doc, context, index, prefills) for index, row in enumerate(form.fields)],
	}

	metadata = dict(private_metadata or {})
	metadata["form"] = form.name
	serialised = json.dumps(metadata, separators=(",", ":"))

	if len(serialised) > bk.MAX_PRIVATE_METADATA:
		frappe.throw(_("Too much context to carry through the modal."))

	view["private_metadata"] = serialised
	return view


def build_input(row, doc, context: dict, index: int, prefills: dict | None = None) -> dict:
	block_id = f"f_{index}_{row.fieldname}"
	default = None

	if prefills and prefills.get(row.fieldname):
		default = prefills[row.fieldname]
	elif row.default_value:
		try:
			default = render(row.default_value, doc, context) if doc else row.default_value
		except Exception:
			default = None

	element = {"action_id": "value"}

	if row.input_type in ("Text", "Long Text"):
		element["type"] = "plain_text_input"
		if row.input_type == "Long Text":
			element["multiline"] = True
		if default:
			element["initial_value"] = str(default)

	elif row.input_type == "Number":
		element["type"] = "number_input"
		element["is_decimal_allowed"] = True
		if default:
			element["initial_value"] = str(default)

	elif row.input_type == "Date":
		element["type"] = "datepicker"
		if default:
			element["initial_date"] = str(default)[:10]

	elif row.input_type == "Checkbox":
		element["type"] = "checkboxes"
		option = {"text": {"type": "plain_text", "text": _("Yes")}, "value": "1"}
		element["options"] = [option]
		if cint(default):
			element["initial_options"] = [option]

	elif row.input_type == "User":
		element["type"] = "users_select"

	elif row.input_type == "Select":
		options = [
			{"text": {"type": "plain_text", "text": choice[:75]}, "value": choice[:75]}
			for choice in (row.options or "").split("\n")
			if choice.strip()
		]
		element["type"] = "static_select"
		element["options"] = options or [
			{"text": {"type": "plain_text", "text": _("No options")}, "value": ""}
		]

	elif row.input_type == "Document Link":
		element["type"] = "static_select"
		element["options"] = get_link_options(row.options)

	else:
		element["type"] = "plain_text_input"

	return {
		"type": "input",
		"block_id": block_id,
		"optional": not row.reqd,
		"label": {"type": "plain_text", "text": (row.label or row.fieldname)[:75]},
		"element": element,
	}


def get_link_options(doctype: str) -> list[dict]:
	"""Slack static selects cap at 100 options, so offer the most recent records."""
	if not doctype or not frappe.db.exists("DocType", doctype):
		return [{"text": {"type": "plain_text", "text": _("No options")}, "value": ""}]

	meta = frappe.get_meta(doctype)
	title_field = meta.get_title_field()

	rows = frappe.get_all(
		doctype,
		fields=["name"] + ([title_field] if title_field and title_field != "name" else []),
		order_by="modified desc",
		limit=100,
	)

	options = []
	for row in rows:
		label = row.get(title_field) if title_field else None
		text = f"{row.name} — {label}" if label and label != row.name else row.name
		options.append({"text": {"type": "plain_text", "text": text[:75]}, "value": row.name})

	return options or [{"text": {"type": "plain_text", "text": _("No options")}, "value": ""}]


def open_form_modal(
	workspace,
	trigger_id: str,
	form_name: str,
	user: str,
	private_metadata: dict,
	prefills: dict | None = None,
):
	"""Open a modal inline — trigger_id expires three seconds after Slack issues it."""
	form = frappe.get_cached_doc("Slack Form", form_name)
	if not form.enabled:
		frappe.throw(_("Form {0} is disabled.").format(form_name))

	doc = None
	if private_metadata.get("doctype") and private_metadata.get("docname"):
		if frappe.db.exists(private_metadata["doctype"], private_metadata["docname"]):
			doc = frappe.get_doc(private_metadata["doctype"], private_metadata["docname"])

	view = build_view(form, doc=doc, private_metadata=private_metadata, user=user, prefills=prefills)
	SlackClient(workspace).open_view(trigger_id, view)


# --------------------------------------------------------------- message shortcut


def handle_message_action(workspace, payload: dict) -> None:
	"""A Slack Form offered in the message menu: open it with the message text prefilled."""
	from slack_bridge.api.comm_log import demarkdown, notify_via_response_url

	slack_user_id = (payload.get("user") or {}).get("id")
	callback_id = payload.get("callback_id")

	# action_ts is unique per invocation; Slack's redeliveries of one invocation dedupe.
	key = base.idempotency_key("form_shortcut", callback_id, payload.get("action_ts"), slack_user_id)
	log_name = base.claim(key, "Interactive", workspace.name, slack_user_id=slack_user_id)
	if not log_name:
		base.respond()
		return

	base.store_payload(log_name, payload)

	form_name = frappe.db.get_value(
		"Slack Form", {"shortcut_callback_id": callback_id, "message_shortcut": 1, "enabled": 1}, "name"
	)
	if not form_name:
		base.finish(log_name, "Ignored", "No enabled form for shortcut")
		notify_via_response_url(payload, _("This shortcut is not configured."))
		return

	mapping = base.resolve_user(workspace.name, slack_user_id)
	if not mapping:
		base.finish(log_name, "Ignored", "Unmapped Slack user")
		# The HTTP response to a shortcut is not rendered by Slack — use the response_url.
		notify_via_response_url(payload, base.link_prompt()["text"])
		return

	form = frappe.get_cached_doc("Slack Form", form_name)
	prefills = {}
	if form.shortcut_prefill_field:
		text = demarkdown((payload.get("message") or {}).get("text") or "")
		prefills[form.shortcut_prefill_field] = bk.truncate(text, 2900)

	try:
		open_form_modal(
			workspace=workspace,
			trigger_id=payload.get("trigger_id"),
			form_name=form_name,
			user=mapping.user,
			private_metadata={
				"channel": (payload.get("channel") or {}).get("id"),
				"message_ts": (payload.get("message") or {}).get("ts"),
				"response_url": payload.get("response_url"),
				"log": log_name,
			},
			prefills=prefills,
		)
		base.finish(log_name, "Processed", f"Opened {form_name}")
	except Exception:
		frappe.log_error(title="Slack Bridge: could not open modal", message=frappe.get_traceback())
		base.finish(log_name, "Failed", "Could not open modal")
		notify_via_response_url(payload, _("Could not open the form — try again."))

	base.respond()


# ------------------------------------------------------------------- submission


def extract_values(form, view: dict) -> dict:
	state = ((view.get("state") or {}).get("values")) or {}
	values = {}

	for index, row in enumerate(form.fields):
		block_id = f"f_{index}_{row.fieldname}"
		block = state.get(block_id) or {}
		element = block.get("value") or {}

		if row.input_type == "Checkbox":
			values[row.fieldname] = 1 if element.get("selected_options") else 0
		elif row.input_type == "Date":
			values[row.fieldname] = element.get("selected_date")
		elif row.input_type in ("Select", "Document Link"):
			values[row.fieldname] = (element.get("selected_option") or {}).get("value")
		elif row.input_type == "User":
			values[row.fieldname] = element.get("selected_user")
		elif row.input_type == "Number":
			raw = element.get("value")
			values[row.fieldname] = flt(raw) if raw not in (None, "") else None
		else:
			values[row.fieldname] = element.get("value")

	return values


def submit_form(workspace, view: dict, metadata: dict, user: str, slack_user_id: str, log_name: str):
	"""Runs inline: Slack needs the verdict, and field errors are only reportable here."""
	form = frappe.get_cached_doc("Slack Form", metadata.get("form"))
	values = extract_values(form, view)

	# Slack user pickers return Slack ids; translate them back to Frappe users.
	for row in form.fields:
		if row.input_type == "User" and values.get(row.fieldname):
			from slack_bridge.engine.users import get_frappe_user

			values[row.fieldname] = (
				get_frappe_user(workspace.name, values[row.fieldname]) or values[row.fieldname]
			)

	# A modal opened by an action is collecting input for that action, not saving a doc.
	if metadata.get("action"):
		frappe.enqueue(
			"slack_bridge.api.actions.run",
			queue="short",
			enqueue_after_commit=True,
			workspace=workspace.name,
			action_name=metadata["action"],
			doctype=metadata.get("doctype"),
			docname=metadata.get("docname"),
			user=user,
			slack_user_id=slack_user_id,
			channel=metadata.get("channel"),
			message_ts=metadata.get("message_ts"),
			response_url=metadata.get("response_url"),
			log_name=metadata.get("log") or log_name,
			inputs=values,
		)
		base.finish(log_name, "Processed", "Collected input")
		return {"response_action": "clear"}

	doc = save_document(form, values, metadata, user)
	base.finish(log_name, "Processed", f"{doc.doctype} {doc.name}")

	notify_submitter(workspace, form, doc, metadata, slack_user_id)
	return {"response_action": "clear"}


def save_document(form, values: dict, metadata: dict, user: str):
	original_user = frappe.session.user
	try:
		frappe.set_user(user)

		if form.mode == "Update":
			docname = metadata.get("docname")
			if not docname:
				frappe.throw(_("No document to update."))
			doc = frappe.get_doc(form.document_type, docname)
			doc.check_permission("write")
			doc.update(values)
		else:
			doc = frappe.get_doc({"doctype": form.document_type, **values})

		doc.save()

		if form.submit_action == "Save and Submit" and doc.docstatus == 0:
			doc.submit()

		frappe.db.commit()
		return doc
	finally:
		frappe.set_user(original_user)


def notify_submitter(workspace, form, doc, metadata: dict, slack_user_id: str) -> None:
	from slack_bridge.engine.context import get_doc_url

	url = get_doc_url(doc)
	text = _("Saved {0} <{1}|{2}>").format(doc.doctype, url, doc.name)

	if form.after_submit_message:
		try:
			text = render(form.after_submit_message, doc, {"doc_url": url})
		except Exception:
			pass

	from slack_bridge.slack.client import confirm_to_user

	client = SlackClient(workspace)
	channel = metadata.get("channel")

	try:
		if form.post_to_channel and channel:
			client.post_message(channel=channel, text=text, blocks=[bk.section(text)])
		else:
			confirm_to_user(client, channel, slack_user_id, text)
	except Exception:
		frappe.log_error(title="Slack Bridge: confirmation failed", message=frappe.get_traceback())
