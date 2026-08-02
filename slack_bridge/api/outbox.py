# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Desk actions on Slack Message Log."""

from __future__ import annotations

import json

import frappe
from frappe import _


@frappe.whitelist()
def retry_action(doc=None):
	"""Put a failed or dead message back in the queue and try it immediately."""
	frappe.only_for("System Manager")

	if isinstance(doc, str):
		doc = json.loads(doc)

	name = doc.get("name") if isinstance(doc, dict) else doc
	log = frappe.get_doc("Slack Message Log", name)

	if log.status == "Sent":
		frappe.msgprint(_("This message was already delivered."), indicator="blue")
		return

	log.db_set({"status": "Queued", "next_attempt_at": None, "attempts": 0}, update_modified=False)

	frappe.enqueue(
		"slack_bridge.engine.outbox.deliver",
		queue="short",
		enqueue_after_commit=True,
		log_name=log.name,
	)

	frappe.msgprint(_("Queued for delivery."), indicator="green")
