# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Shared evaluation context for conditions and templates.

Conditions use `frappe.safe_eval` with the framework's safe globals — the same sandbox
core Notification and Webhook use, so a rule author can never reach beyond it.
"""

from __future__ import annotations

import html as html_module
import re

import frappe
from frappe import _
from frappe.utils import nowdate, nowtime
from frappe.utils.safe_exec import get_safe_globals

# Link placeholders survive tag-stripping and entity work, then become mrkdwn links.
_LINK_START = "\x00"
_LINK_END = "\x01"


def html_to_mrkdwn(value) -> str:
	"""Convert HTML field content (Text Editor, rich descriptions) to Slack mrkdwn.

	Slack renders mrkdwn, so raw HTML shows its tags literally. This maps the common
	structure — bold/italic/strike/code, line breaks, list items, links — and strips
	everything else. Exposed to rule templates as `html_to_mrkdwn`.
	"""
	if not value:
		return ""

	text = str(value)

	text = re.sub(r"(?i)<br\s*/?>", "\n", text)
	# Block-level elements break the line on both sides; duplicates collapse below.
	text = re.sub(r"(?i)</?(p|div|tr|h[1-6])(\s[^>]*)?>", "\n", text)
	text = re.sub(r"(?i)</li>", "\n", text)
	text = re.sub(r"(?i)<li[^>]*>", "• ", text)

	text = re.sub(r"(?i)</?(b|strong)(\s[^>]*)?>", "*", text)
	text = re.sub(r"(?i)</?(i|em)(\s[^>]*)?>", "_", text)
	text = re.sub(r"(?i)</?(s|strike|del)(\s[^>]*)?>", "~", text)
	text = re.sub(r"(?i)</?code(\s[^>]*)?>", "`", text)

	text = re.sub(
		r"(?is)<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
		lambda m: f"{_LINK_START}{m.group(1)}|{re.sub(r'<[^>]+>', '', m.group(2)).strip()}{_LINK_END}",
		text,
	)

	text = re.sub(r"<[^>]+>", "", text)
	text = html_module.unescape(text)

	# mrkdwn transport escaping — after unescaping, so entities don't double up.
	text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
	text = text.replace(_LINK_START, "<").replace(_LINK_END, ">")

	text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
	text = re.sub(r"\n{3,}", "\n\n", text)
	return text.strip()


def get_doc_url(doc) -> str:
	try:
		return frappe.utils.get_url_to_form(doc.doctype, doc.name)
	except Exception:
		return frappe.utils.get_url()


def get_context(doc, extra: dict | None = None) -> dict:
	safe_globals = get_safe_globals()

	context = {
		"doc": doc,
		"nowdate": nowdate,
		"nowtime": nowtime,
		"frappe": safe_globals.get("frappe"),
		# `json` sits at the top level of Frappe's safe globals, not under `frappe`.
		# Without it a template cannot read a JSON-valued field at all — common when
		# notifying on log or event doctypes that carry a payload blob.
		"json": safe_globals.get("json"),
		"as_json": safe_globals.get("as_json"),
		# frappe v16 exposes frappe.utils.parse_json to templates, v15 does not —
		# providing it here gives both majors the same template vocabulary.
		"parse_json": frappe.parse_json,
		# HTML-valued fields (Text Editor, generated descriptions) render their tags
		# literally in Slack; this converts them to mrkdwn.
		"html_to_mrkdwn": html_to_mrkdwn,
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
