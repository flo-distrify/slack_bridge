# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Dynamic forms: modals whose choices and fields come from another app at runtime.

A Slack Dynamic Form names three dotted method paths — the provider contract:

    options(query, context) -> [{"value": str, "label": str}, ...]
    schema(key, context)    -> {"title": str, "submit_label": str, "fields": [...]}
    submit(key, values, context) -> str | {"message": str}

The flow is two modal stages: a searchable picker (external_select over `options`),
then — via Slack's `response_action: update` — the input form rendered from `schema`.
Submitting calls `submit` as the mapped Frappe user. slack_bridge knows nothing about
the providing app; the coupling is a dotted path in configuration, nothing more.
See docs/dynamic-forms.md for the full contract.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint

from slack_bridge.api import base
from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient

PICK_CALLBACK_ID = "sb_dyn_pick"
FORM_CALLBACK_ID = "sb_dyn_form"
PICKER_ACTION_ID = "sbdf_options"
LINK_ACTION_ID = "sbdf_link"
PICKER_BLOCK_ID = "df_picker"

MULTILINE_FIELDTYPES = ("Small Text", "Text", "Long Text", "Text Editor")


# ------------------------------------------------------------------ provider calls


def build_context(form, user: str, slack_user_id: str, channel_id: str | None, arguments: str | None) -> dict:
	"""The contract context — everything a provider may want to know about the caller."""
	return {
		"user": user,
		"slack_user_id": slack_user_id,
		"channel_id": channel_id,
		"workspace": form.workspace,
		"arguments": arguments or "",
	}


def call_provider(dotted_path: str, user: str, *args):
	"""Run one provider method as the mapped Frappe user, restoring the session after."""
	method = frappe.get_attr(dotted_path)
	original_user = frappe.session.user
	try:
		frappe.set_user(user)
		return method(*args)
	finally:
		frappe.set_user(original_user)


def get_form(form_name: str):
	form = frappe.get_cached_doc("Slack Dynamic Form", form_name)
	if not form.enabled:
		frappe.throw(_("Dynamic form {0} is disabled.").format(form_name))
	return form


def check_role(form, user: str) -> None:
	if form.required_role and form.required_role not in frappe.get_roles(user):
		frappe.throw(_("You need the {0} role for that.").format(form.required_role))


# ------------------------------------------------------------------ stage 1: picker


def open_picker(workspace, trigger_id: str, form_name: str, user: str, context) -> None:
	"""Open the search picker inline — trigger_id expires three seconds after Slack issues it."""
	form = get_form(form_name)
	check_role(form, user)

	metadata = {
		"kind": "sbdf",
		"form": form.name,
		"channel": context.channel_id,
		"args": (context.arguments or "")[:200],
	}
	serialised = json.dumps(metadata, separators=(",", ":"))

	view = {
		"type": "modal",
		"callback_id": PICK_CALLBACK_ID,
		"title": {"type": "plain_text", "text": (form.modal_title or form.title)[:24]},
		"submit": {"type": "plain_text", "text": _("Next")},
		"close": {"type": "plain_text", "text": _("Cancel")},
		"private_metadata": serialised,
		"blocks": [
			{
				"type": "input",
				"block_id": PICKER_BLOCK_ID,
				"label": {"type": "plain_text", "text": (form.picker_label or _("Choose…"))[:75]},
				"element": {
					"type": "external_select",
					"action_id": PICKER_ACTION_ID,
					"min_query_length": max(cint(form.min_query_length), 0),
					"placeholder": {"type": "plain_text", "text": _("Type to search")},
				},
			}
		],
	}

	SlackClient(workspace).open_view(trigger_id, view)


# ------------------------------------------------------------------- suggestions


def handle_suggestion(workspace, payload: dict) -> None:
	"""Serve external_select options. Read-only, fired per keystroke — never 500."""
	try:
		options = build_suggestions(workspace, payload)
		base.respond({"options": options})
	except Exception:
		frappe.log_error(title="Slack Bridge: dynamic form options failed", message=frappe.get_traceback())
		base.respond({"options": []})


def build_suggestions(workspace, payload: dict) -> list[dict]:
	metadata = parse_metadata(payload.get("view") or {})
	if not metadata.get("form") or not frappe.db.exists("Slack Dynamic Form", metadata["form"]):
		return []

	form = frappe.get_cached_doc("Slack Dynamic Form", metadata["form"])
	if not form.enabled:
		return []

	slack_user_id = (payload.get("user") or {}).get("id")
	mapping = base.resolve_user(workspace.name, slack_user_id)
	if not mapping:
		return []

	query = (payload.get("value") or "").strip()
	context = build_context(form, mapping.user, slack_user_id, metadata.get("channel"), metadata.get("args"))

	if payload.get("action_id") == PICKER_ACTION_ID:
		raw = call_provider(form.options_method, mapping.user, query, context) or []
		return [make_option(o.get("label"), o.get("value")) for o in raw if o.get("value")][:100]

	if payload.get("action_id") == LINK_ACTION_ID:
		return link_options(form, metadata, payload, query, mapping.user, context)

	return []


def link_options(form, metadata: dict, payload: dict, query: str, user: str, context: dict) -> list[dict]:
	"""Search the Link field's target doctype, permission-filtered as the mapped user.

	The field's target lives in the provider's schema; re-fetching it here keeps
	private_metadata small (Slack caps it at 3000 chars) and always current.
	"""
	fieldname = parse_field_block_id(payload.get("block_id") or "")
	if not fieldname or not metadata.get("key"):
		return []

	schema = call_provider(form.schema_method, user, metadata["key"], context) or {}
	field = next((f for f in schema.get("fields") or [] if f.get("fieldname") == fieldname), None)
	target = (field or {}).get("options")
	if not target or not frappe.db.exists("DocType", target):
		return []

	meta = frappe.get_meta(target)
	title_field = meta.get_title_field()
	fields = list({"name", title_field or ""} - {""})
	search_fields = [f for f in ("name", title_field) if f]

	original_user = frappe.session.user
	try:
		frappe.set_user(user)
		# frappe.get_list validates fieldnames and applies the session user's permissions.
		records = frappe.get_list(
			target,
			or_filters=[[target, f, "like", f"%{query}%"] for f in search_fields],
			fields=fields,
			limit=20,
			order_by="modified desc",
		)
	finally:
		frappe.set_user(original_user)

	options = []
	for record in records:
		label = record.get(title_field) if title_field else None
		text = f"{label} ({record.name})" if label and label != record.name else record.name
		options.append(make_option(text, record.name))

	return options


def make_option(label: str | None, value: str) -> dict:
	return {
		"text": {"type": "plain_text", "text": bk.truncate(str(label or value), 75)},
		"value": bk.truncate(str(value), 150),
	}


def parse_metadata(view: dict) -> dict:
	try:
		return json.loads(view.get("private_metadata") or "{}")
	except ValueError:
		return {}


def parse_field_block_id(block_id: str) -> str | None:
	"""df_{index}_{fieldname} → fieldname."""
	parts = block_id.split("_", 2)
	if len(parts) == 3 and parts[0] == "df" and parts[1].isdigit():
		return parts[2]
	return None


# ------------------------------------------------- stage 1 → stage 2 transition


def handle_pick_submission(workspace, payload: dict) -> None:
	"""Swap the picker for the chosen entry's input form.

	Deliberately unclaimed: this renders a view and writes nothing, and claiming it
	would make a Slack retry of the same submission close the modal mid-flow.
	"""
	view = payload.get("view") or {}
	metadata = parse_metadata(view)
	slack_user_id = (payload.get("user") or {}).get("id")

	def picker_error(message: str) -> None:
		base.respond({"response_action": "errors", "errors": {PICKER_BLOCK_ID: message[:250]}})

	mapping = base.resolve_user(workspace.name, slack_user_id)
	if not mapping:
		picker_error(_("Your Slack account is not linked to an ERP user."))
		return

	state = ((view.get("state") or {}).get("values")) or {}
	element = (state.get(PICKER_BLOCK_ID) or {}).get(PICKER_ACTION_ID) or {}
	key = (element.get("selected_option") or {}).get("value")
	if not key:
		picker_error(_("Pick an entry first."))
		return

	try:
		form = get_form(metadata.get("form"))
		check_role(form, mapping.user)
		context = build_context(
			form, mapping.user, slack_user_id, metadata.get("channel"), metadata.get("args")
		)
		schema = call_provider(form.schema_method, mapping.user, key, context) or {}
		base.respond({"response_action": "update", "view": build_form_view(form, schema, key, metadata)})
	except Exception as e:
		frappe.log_error(title="Slack Bridge: dynamic form schema failed", message=frappe.get_traceback())
		picker_error(str(e) or _("Could not load the form."))


def build_form_view(form, schema: dict, key: str, metadata: dict) -> dict:
	fields = schema.get("fields") or []

	form_metadata = dict(metadata)
	form_metadata["key"] = key
	serialised = json.dumps(form_metadata, separators=(",", ":"))
	if len(serialised) > bk.MAX_PRIVATE_METADATA:
		frappe.throw(_("Too much context to carry through the modal."))

	blocks = [build_field_input(field, index, form) for index, field in enumerate(fields)]
	if not blocks:
		blocks = [bk.section(schema.get("title") or form.title)]

	return {
		"type": "modal",
		"callback_id": FORM_CALLBACK_ID,
		"title": {"type": "plain_text", "text": (schema.get("title") or form.modal_title or form.title)[:24]},
		"submit": {"type": "plain_text", "text": (schema.get("submit_label") or _("Submit"))[:24]},
		"close": {"type": "plain_text", "text": _("Cancel")},
		"private_metadata": serialised,
		"blocks": blocks,
	}


def build_field_input(field: dict, index: int, form) -> dict:
	"""Map one contract field (Frappe fieldtype vocabulary) to a Block Kit input."""
	fieldtype = field.get("fieldtype") or "Data"
	default = field.get("default")
	element = {"action_id": "value"}

	if fieldtype in MULTILINE_FIELDTYPES:
		element["type"] = "plain_text_input"
		element["multiline"] = True
		if default:
			element["initial_value"] = str(default)

	elif fieldtype in ("Int", "Float", "Currency"):
		element["type"] = "number_input"
		element["is_decimal_allowed"] = fieldtype != "Int"
		if default not in (None, ""):
			element["initial_value"] = str(default)

	elif fieldtype == "Date":
		element["type"] = "datepicker"
		if default:
			element["initial_date"] = str(default)[:10]

	elif fieldtype == "Check":
		element["type"] = "checkboxes"
		option = {"text": {"type": "plain_text", "text": _("Yes")}, "value": "1"}
		element["options"] = [option]
		if cint(default):
			element["initial_options"] = [option]

	elif fieldtype in ("Select", "Radio"):
		choices = [c.strip() for c in (field.get("options") or "").split("\n") if c.strip()]
		options = [make_option(choice, choice) for choice in choices]
		element["type"] = "radio_buttons" if fieldtype == "Radio" else "static_select"
		element["options"] = options or [make_option(_("No options"), "")]
		if default and default in choices:
			element["initial_option"] = make_option(default, default)

	elif fieldtype == "Link":
		element["type"] = "external_select"
		element["action_id"] = LINK_ACTION_ID
		element["min_query_length"] = max(cint(form.min_query_length), 0)
		if default:
			element["initial_option"] = make_option(default, default)

	else:
		# Data and any fieldtype a future provider invents degrade to plain text.
		element["type"] = "plain_text_input"
		if default:
			element["initial_value"] = str(default)

	block = {
		"type": "input",
		"block_id": f"df_{index}_{field.get('fieldname')}",
		"optional": not field.get("reqd"),
		"label": {"type": "plain_text", "text": str(field.get("label") or field.get("fieldname"))[:75]},
		"element": element,
	}

	if field.get("description"):
		block["hint"] = {"type": "plain_text", "text": str(field["description"])[:255]}

	return block


# ------------------------------------------------------------------- submission


def extract_values(fields: list[dict], view: dict) -> dict:
	"""Read the modal state back into {fieldname: value}.

	Empty inputs become "" (not omitted) so a provider always sees every schema key.
	"""
	state = ((view.get("state") or {}).get("values")) or {}
	values = {}

	for index, field in enumerate(fields):
		fieldname = field.get("fieldname")
		fieldtype = field.get("fieldtype") or "Data"
		block = state.get(f"df_{index}_{fieldname}") or {}
		element = block.get(LINK_ACTION_ID if fieldtype == "Link" else "value") or {}

		if fieldtype == "Check":
			values[fieldname] = 1 if element.get("selected_options") else 0
		elif fieldtype == "Date":
			values[fieldname] = element.get("selected_date") or ""
		elif fieldtype in ("Select", "Radio", "Link"):
			values[fieldname] = (element.get("selected_option") or {}).get("value") or ""
		else:
			values[fieldname] = element.get("value") or ""

	return values


def submit_dynamic_form(workspace, view: dict, metadata: dict, user: str, slack_user_id: str, log_name: str):
	"""The claimed stage-2 path. Inline by default so validation errors reach the modal."""
	form = get_form(metadata.get("form"))
	check_role(form, user)

	key = metadata.get("key")
	context = build_context(form, user, slack_user_id, metadata.get("channel"), metadata.get("args"))
	schema = call_provider(form.schema_method, user, key, context) or {}
	values = extract_values(schema.get("fields") or [], view)

	if form.submit_in_background:
		frappe.enqueue(
			"slack_bridge.api.dynamic_forms.execute_submit",
			queue="short",
			enqueue_after_commit=True,
			workspace=workspace.name,
			form_name=form.name,
			key=key,
			values=values,
			context=context,
			user=user,
			slack_user_id=slack_user_id,
			channel=metadata.get("channel"),
		)
		base.finish(log_name, "Processed", f"Queued {form.name}")
		return {"response_action": "clear"}

	try:
		result = call_provider(form.submit_method, user, key, values, context)
	except frappe.ValidationError as e:
		frappe.db.rollback()
		base.finish(log_name, "Failed", str(e))
		return {"response_action": "errors", "errors": map_errors(e, schema.get("fields") or [], view)}

	message = result.get("message") if isinstance(result, dict) else result
	message = str(message or _("Done."))

	base.finish(log_name, "Processed", message[:500])
	send_confirmation(workspace, slack_user_id, metadata.get("channel"), message)
	return {"response_action": "clear"}


def map_errors(exception, fields: list[dict], view: dict) -> dict:
	"""Attach errors to blocks. Providers may set a `field_errors` dict on the exception."""
	field_errors = getattr(exception, "field_errors", None)
	if isinstance(field_errors, dict) and field_errors:
		block_ids = {
			field.get("fieldname"): f"df_{index}_{field.get('fieldname')}"
			for index, field in enumerate(fields)
		}
		mapped = {
			block_ids[fieldname]: str(message)[:250]
			for fieldname, message in field_errors.items()
			if fieldname in block_ids
		}
		if mapped:
			return mapped

	# No per-field mapping: surface the message on the first real block so Slack renders it.
	first_block = (view.get("blocks") or [{}])[0].get("block_id", "unknown")
	return {first_block: str(exception)[:250] or _("Could not submit.")}


def execute_submit(
	workspace: str,
	form_name: str,
	key: str,
	values: dict,
	context: dict,
	user: str,
	slack_user_id: str,
	channel: str | None,
) -> None:
	"""Background variant: the modal is long closed, so results and errors go to Slack directly."""
	ws = frappe.get_cached_doc("Slack Workspace", workspace)

	try:
		form = get_form(form_name)
		result = call_provider(form.submit_method, user, key, values, context)
		message = result.get("message") if isinstance(result, dict) else result
		send_confirmation(ws, slack_user_id, channel, str(message or _("Done.")))
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title="Slack Bridge: dynamic form job failed", message=frappe.get_traceback())
		send_confirmation(ws, slack_user_id, channel, f":warning: {e}")


def send_confirmation(workspace, slack_user_id: str, channel: str | None, text: str) -> None:
	client = SlackClient(workspace)

	try:
		if channel:
			client.post_ephemeral(channel=channel, user=slack_user_id, text=text)
		else:
			dm = client.open_dm(slack_user_id)
			client.post_message(channel=dm, text=text, blocks=[bk.section(text)])
	except Exception:
		frappe.log_error(title="Slack Bridge: confirmation failed", message=frappe.get_traceback())
