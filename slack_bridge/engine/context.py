# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Shared evaluation context for conditions and templates.

Conditions use `frappe.safe_eval` with the framework's safe globals — the same sandbox
core Notification and Webhook use, so a rule author can never reach beyond it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import nowdate, nowtime
from frappe.utils.safe_exec import get_safe_globals


def get_doc_url(doc) -> str:
	try:
		return frappe.utils.get_url_to_form(doc.doctype, doc.name)
	except Exception:
		return frappe.utils.get_url()


def get_context(doc, extra: dict | None = None) -> dict:
	context = {
		"doc": doc,
		"nowdate": nowdate,
		"nowtime": nowtime,
		"frappe": get_safe_globals().get("frappe"),
		"doc_url": get_doc_url(doc),
	}

	if extra:
		context.update(extra)

	return context


def evaluate_condition(condition: str | None, doc, extra: dict | None = None) -> bool:
	"""Evaluate a rule condition. An empty condition always matches."""
	if not condition or not condition.strip():
		return True

	try:
		return bool(frappe.safe_eval(condition, None, get_context(doc, extra)))
	except Exception:
		frappe.log_error(
			title="Slack Bridge: condition failed",
			message=f"Condition: {condition}\nDocument: {doc.doctype} {doc.name}\n\n{frappe.get_traceback()}",
		)
		# A broken condition must not fire a notification with unknown intent.
		return False


def render(template: str | None, doc, extra: dict | None = None) -> str:
	"""Render a Jinja template against the document context."""
	if not template:
		return ""

	try:
		return frappe.render_template(template, get_context(doc, extra))
	except Exception as e:
		frappe.log_error(
			title="Slack Bridge: template failed",
			message=f"Template: {template}\nDocument: {doc.doctype} {doc.name}\n\n{frappe.get_traceback()}",
		)
		raise frappe.ValidationError(_("Template rendering failed: {0}").format(e))


def validate_template_field(template: str | None, fieldname: str) -> None:
	"""Validate Jinja syntax at save time so authors get errors in the form, not at send time."""
	if not template:
		return

	from frappe.utils.jinja import validate_template

	try:
		validate_template(template)
	except Exception as e:
		frappe.throw(_("{0} contains invalid Jinja: {1}").format(fieldname, e))


def validate_condition_field(condition: str | None, fieldname: str) -> None:
	if not condition or not condition.strip():
		return

	try:
		compile(condition.strip(), "<condition>", "eval")
	except SyntaxError as e:
		frappe.throw(_("{0} is not a valid Python expression: {1}").format(fieldname, e))
