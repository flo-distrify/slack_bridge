# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Shared plumbing for the three Slack Request URLs.

Order matters on every inbound request: resolve the workspace, verify the signature over
the raw body, then do anything else. Slack expects HTTP 200 within three seconds, so the
handlers acknowledge first and push real work to the background — except when the work
needs the `trigger_id`, which expires in three seconds and therefore must be used inline.
"""

from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _

from slack_bridge.slack.verify import SlackSignatureError, verify_request


class SlackRequestError(frappe.ValidationError):
	pass


def respond(payload: dict | None = None, status: int = 200) -> None:
	"""Emit an exact JSON body to Slack instead of Frappe's {"message": ...} envelope."""
	frappe.local.response.clear()
	# Document hooks (a failing Notification, a msgprint in a server script) queue
	# server messages that Frappe appends to the body at serialisation time — Slack
	# rejects the unexpected key and shows a generic error. Drop them; they belong
	# to Desk sessions, not protocol responses.
	frappe.clear_messages()
	if payload:
		frappe.local.response.update(payload)
	frappe.local.response["http_status_code"] = status


def respond_empty() -> None:
	"""Send a truly empty 200.

	Slash commands render ANY response body as a message — even the "{}" Frappe
	produces when serialising an empty response dict shows up in the channel as
	literal braces. The binary response type is the one way to emit zero bytes.
	"""
	frappe.local.response.clear()
	frappe.clear_messages()
	frappe.local.response["type"] = "binary"
	frappe.local.response["filename"] = "empty"
	frappe.local.response["filecontent"] = b""


def get_raw_body() -> bytes:
	return frappe.request.get_data() or b""


def get_workspace():
	"""Resolve the workspace from the ?token= in the Request URL and verify the signature."""
	from slack_bridge.slack_bridge.doctype.slack_workspace.slack_workspace import (
		get_workspace_by_token,
	)

	if not is_enabled():
		raise SlackRequestError(_("Slack Bridge is disabled"))

	# Slack's own payloads carry a legacy `token` field in the BODY (slash commands,
	# the events url_verification challenge) which Frappe merges into form_dict — it
	# must never shadow the endpoint token in the Request URL's query string. This is
	# why interactivity (whose body has only `payload`) worked while commands and the
	# events challenge failed with 401.
	token = frappe.request.args.get("token") or frappe.form_dict.get("token")
	workspace = get_workspace_by_token(token)

	if not workspace:
		# Do not reveal whether the token merely mismatched.
		raise SlackSignatureError(_("Unknown Slack endpoint"))

	signing_secret = workspace.get_password("signing_secret", raise_exception=False)
	verify_request(
		signing_secret,
		frappe.get_request_header("X-Slack-Request-Timestamp"),
		frappe.get_request_header("X-Slack-Signature"),
		get_raw_body(),
	)

	return workspace


def is_enabled() -> bool:
	enabled = frappe.db.get_single_value("Slack Bridge Settings", "enabled")
	return enabled is None or bool(enabled)


def parse_interactive_payload() -> dict:
	"""Interactivity and shortcuts arrive form-encoded with a JSON `payload` field."""
	raw = frappe.form_dict.get("payload")
	if not raw:
		raise SlackRequestError(_("Missing payload"))

	try:
		return json.loads(raw)
	except ValueError:
		raise SlackRequestError(_("Malformed payload"))


def parse_event_payload() -> dict:
	body = get_raw_body()
	try:
		return json.loads(body or b"{}")
	except ValueError:
		raise SlackRequestError(_("Malformed event body"))


# ------------------------------------------------------------------ idempotency


def idempotency_key(*parts) -> str:
	return hashlib.sha1("|".join(str(p or "") for p in parts).encode()).hexdigest()


def claim(key: str, kind: str, workspace: str, **fields) -> str | None:
	"""Record an interaction, returning None if we have already handled this key.

	Slack redelivers events up to three times and users double-click buttons, so every
	inbound side effect is gated on winning this claim.
	"""
	if frappe.db.exists("Slack Interaction Log", {"idempotency_key": key}):
		return None

	log = frappe.get_doc(
		{
			"doctype": "Slack Interaction Log",
			"idempotency_key": key,
			"kind": kind,
			"workspace": workspace,
			"status": "Received",
			**fields,
		}
	)

	try:
		log.insert(ignore_permissions=True)
		# Commit the claim immediately: the unique index is what makes concurrent
		# duplicates lose, and it only protects us once it is visible to other workers.
		frappe.db.commit()
	except frappe.exceptions.DuplicateEntryError:
		frappe.db.rollback()
		return None

	return log.name


def finish(log_name: str | None, status: str, result: str | None = None) -> None:
	if not log_name:
		return

	frappe.db.set_value(
		"Slack Interaction Log",
		log_name,
		{"status": status, "result": (result or "")[:500]},
		update_modified=False,
	)


def store_payload(log_name: str | None, payload: dict) -> None:
	"""Keep the full payload only when verbose logging is on."""
	if not log_name:
		return

	if not frappe.db.get_single_value("Slack Bridge Settings", "debug_logging"):
		return

	frappe.db.set_value(
		"Slack Interaction Log",
		log_name,
		"payload",
		json.dumps(payload, indent=1)[:100000],
		update_modified=False,
	)


# ----------------------------------------------------------------------- identity


def resolve_user(workspace: str, slack_user_id: str, require_actions: bool = True):
	"""Map a Slack user to a Frappe user, or return None when unlinked/not allowed."""
	from slack_bridge.engine.users import get_mapping

	mapping = get_mapping(workspace, slack_user_id)
	if not mapping or not mapping.enabled:
		return None

	if require_actions and not mapping.allow_actions:
		return None

	return mapping


def link_prompt() -> dict:
	return {
		"response_type": "ephemeral",
		"text": _(
			"Your Slack account is not linked to a user in the ERP yet, so this action was not run. "
			"Ask an administrator to map you under Slack User."
		),
	}
