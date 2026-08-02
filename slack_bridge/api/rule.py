# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Desk actions on Slack Notification Rule."""

from __future__ import annotations

import json

import frappe
from frappe import _


def _get_rule(doc):
	if isinstance(doc, str):
		doc = json.loads(doc)

	name = doc.get("name") if isinstance(doc, dict) else doc
	return frappe.get_doc("Slack Notification Rule", name)


@frappe.whitelist()
def preview_action(doc=None):
	"""Render the rule against the most recently modified document, without sending."""
	frappe.only_for("System Manager")

	rule = _get_rule(doc)
	result = rule.preview()

	condition = (
		_("Condition passes") if result["condition_passed"] else _("Condition does <b>not</b> pass")
	)

	frappe.msgprint(
		_("Previewed against <b>{0}</b> — {1}").format(result["docname"], condition)
		+ f"<pre style='max-height:400px;overflow:auto'>{frappe.utils.escape_html(json.dumps(result['blocks'], indent=2))}</pre>",
		title=_("Message Preview"),
		wide=True,
	)


@frappe.whitelist()
def send_test_message_action(doc=None):
	"""Queue a real message for the most recently modified document."""
	frappe.only_for("System Manager")

	rule = _get_rule(doc)
	result = rule.send_test()

	frappe.msgprint(result["message"], title=_("Test message"), indicator="green")
