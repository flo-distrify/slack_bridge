# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Rule matching for outbound notifications.

`on_doc_event` runs on every document write in the site, so the no-match path must be
cheap: one Redis hash lookup keyed by doctype, populated lazily and cleared whenever a
rule changes. Everything expensive (rendering, recipient resolution, HTTP) happens in a
background job queued after commit.
"""

from __future__ import annotations

import frappe

CACHE_KEY = "slack_bridge_rules"

# Map the framework's doc_event method to the rule's "Send On" value.
METHOD_TO_EVENT = {
	"after_insert": "New Document",
	"on_update": "Save",
	"on_submit": "Submit",
	"on_cancel": "Cancel",
	"on_trash": "Delete",
	"on_update_after_submit": "Save",
	"on_change": None,  # only used by Value Change and Method rules
}

# Our own doctypes plus the framework's high-churn bookkeeping tables never trigger rules.
IGNORED_DOCTYPES = {
	"Slack Message Log",
	"Slack Interaction Log",
	"Slack Notification Rule",
	"Slack Workspace",
	"Slack Channel",
	"Slack User",
	"Slack Action",
	"Slack Form",
	"Slack Command Route",
	"Slack Unfurl Rule",
	"Slack Bridge Settings",
	"DocType",
	"DocField",
	"DocPerm",
	"Version",
	"Error Log",
	"Scheduled Job Log",
	"Activity Log",
	"Access Log",
	"Route History",
	"Comment",
	"Notification Log",
	"Email Queue",
	"Email Queue Recipient",
	"Prepared Report",
	"Custom Field",
	"Property Setter",
	"Package",
	"Patch Log",
	"Data Import",
	"Document Naming Settings",
}


def get_rules(doctype: str) -> list[dict]:
	"""Enabled rules for a doctype, cached per doctype."""
	cache = frappe.cache()
	cached = cache.hget(CACHE_KEY, doctype)
	if cached is not None:
		return cached

	rules = frappe.get_all(
		"Slack Notification Rule",
		filters={"enabled": 1, "document_type": doctype},
		fields=["name", "event", "value_changed", "method", "priority"],
		order_by="priority desc",
	)
	cache.hset(CACHE_KEY, doctype, rules)
	return rules


def clear_cache(doctype: str | None = None) -> None:
	if doctype:
		frappe.cache().hdel(CACHE_KEY, doctype)
	else:
		frappe.cache().delete_key(CACHE_KEY)


def on_doc_event(doc, method=None):
	"""Wildcard doc_events entry point. Must stay cheap when nothing matches."""
	doctype = doc.doctype

	if doctype in IGNORED_DOCTYPES or doctype.startswith("Slack "):
		return

	# Installs, migrations and bulk imports should never spam a channel.
	if (
		frappe.flags.in_install
		or frappe.flags.in_migrate
		or frappe.flags.in_patch
		or frappe.flags.in_import
		or frappe.flags.in_setup_wizard
		or frappe.flags.in_test
	):
		return

	rules = get_rules(doctype)
	if not rules:
		return

	if not is_enabled():
		return

	for rule_name in match_rules(rules, method, doc):
		queue_rule(rule_name, doc, method)


def match_rules(rules: list[dict], method: str, doc) -> list[str]:
	"""Names of the rules that this (method, document) pair should fire."""
	event = METHOD_TO_EVENT.get(method)
	matched = []

	for rule in rules:
		if rule.get("event") == "Value Change":
			# Value Change is evaluated once per write, on on_change.
			if method == "on_change" and has_field_changed(doc, rule.get("value_changed")):
				matched.append(rule["name"])
		elif rule.get("event") == "Method":
			if rule.get("method") == method:
				matched.append(rule["name"])
		elif event and rule.get("event") == event:
			matched.append(rule["name"])

	return matched


def has_field_changed(doc, fieldname: str | None) -> bool:
	if not fieldname:
		return False

	previous = doc.get_doc_before_save()
	if previous is None:
		# First write of the document — there is no previous value to compare against.
		return False

	return previous.get(fieldname) != doc.get(fieldname)


def is_enabled() -> bool:
	enabled = frappe.db.get_single_value("Slack Bridge Settings", "enabled")
	# Default to on when the Single has never been saved.
	return enabled is None or bool(enabled)


def queue_rule(rule_name: str, doc, method=None):
	frappe.enqueue(
		"slack_bridge.engine.dispatch.run_rule",
		queue="short",
		enqueue_after_commit=True,
		rule_name=rule_name,
		doctype=doc.doctype,
		docname=doc.name,
		event_method=method,
		# The document may be deleted before the job runs, so pass a snapshot along.
		doc_snapshot=doc.as_dict() if method == "on_trash" else None,
	)


def trigger(doc, rule_name: str | None = None, event: str = "Custom"):
	"""Public API for firing Custom rules from server scripts or app code.

	    slack_bridge.trigger(doc, rule_name="Big Order Alert")
	"""
	if isinstance(doc, str):
		frappe.throw("trigger() expects a document object, not a name")

	if rule_name:
		queue_rule(rule_name, doc)
		return

	for rule in get_rules(doc.doctype):
		if rule.get("event") == event:
			queue_rule(rule["name"], doc)
