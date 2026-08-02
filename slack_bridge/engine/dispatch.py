# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Background execution of a matched rule: condition → render → recipients → outbox."""

from __future__ import annotations

import frappe

from slack_bridge.engine import outbox, recipients
from slack_bridge.engine.context import evaluate_condition
from slack_bridge.engine.render import render_message
from slack_bridge.slack.client import SlackClient


def run_rule(rule_name: str, doctype: str, docname: str, event_method=None, doc_snapshot=None):
	"""Entry point for the enqueued job. Never raises into the worker."""
	try:
		rule = frappe.get_cached_doc("Slack Notification Rule", rule_name)
	except frappe.DoesNotExistError:
		return

	if not rule.enabled:
		return

	doc = load_document(doctype, docname, doc_snapshot)
	if doc is None:
		return

	if not evaluate_condition(rule.condition, doc):
		return

	workspace = frappe.get_cached_doc("Slack Workspace", rule.workspace)
	if not workspace.enabled:
		return

	client = SlackClient(workspace)

	targets = recipients.resolve(rule, doc, client)
	if not targets:
		return

	try:
		blocks, text = render_message(rule, doc)
	except Exception:
		frappe.log_error(
			title="Slack Bridge: render failed",
			message=f"Rule: {rule_name}\nDocument: {doctype} {docname}\n\n{frappe.get_traceback()}",
		)
		return

	for target in targets:
		outbox.queue(
			workspace=rule.workspace,
			channel_id=target["channel_id"],
			blocks=blocks,
			text=text,
			rule=rule.name,
			reference_doctype=doctype,
			reference_name=docname,
			thread_mode=rule.thread_mode,
			event_method=event_method,
		)


def load_document(doctype: str, docname: str, snapshot=None):
	"""Reload the document, falling back to the snapshot taken for deletions."""
	if frappe.db.exists(doctype, docname):
		try:
			return frappe.get_doc(doctype, docname)
		except Exception:
			pass

	if snapshot:
		doc = frappe.get_doc(snapshot)
		# A deleted document has no row to reload from; keep the snapshot read-only.
		doc.flags.slack_bridge_snapshot = True
		return doc

	return None
