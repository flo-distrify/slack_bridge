# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Turn a rule plus a document into Block Kit blocks."""

from __future__ import annotations

import json

import frappe
from frappe import _

from slack_bridge.engine.context import evaluate_condition, get_doc_url, render
from slack_bridge.slack import blocks as bk

ACTION_ID_PREFIX = "sb_action"


def render_message(rule, doc, extra: dict | None = None) -> tuple[list[dict], str]:
	"""Return (blocks, fallback_text)."""
	if rule.message_mode == "Block Kit":
		rendered = render(rule.blocks_template, doc, extra)
		body = bk.validate_blocks(rendered, _("Blocks Template"))
	else:
		body = render_simple(rule, doc, extra)

	body += build_buttons(rule.get("buttons"), doc, rule.name)
	body = bk.clamp_blocks(body)

	return body, bk.fallback_text(body)


def render_simple(rule, doc, extra: dict | None = None) -> list[dict]:
	body = []

	headline = render(rule.subject, doc, extra).strip()
	message = render(rule.message, doc, extra).strip()

	if headline and message:
		body.append(bk.section(f"*{headline}*\n{message}"))
	elif headline:
		body.append(bk.section(f"*{headline}*"))
	elif message:
		body.append(bk.section(message))

	if rule.attach_document_link:
		url = get_doc_url(doc)
		body.append(bk.context(f"<{url}|{doc.doctype} {doc.name}>"))

	if not body:
		body.append(bk.section(f"{doc.doctype} *{doc.name}*"))

	return body


def build_buttons(rows, doc, rule_name: str | None = None) -> list[dict]:
	"""Build the actions blocks for a rule's (or unfurl rule's) button rows."""
	if not rows:
		return []

	elements = []
	for index, row in enumerate(rows):
		if not evaluate_condition(row.condition, doc):
			continue

		style = bk.STYLE_MAP.get(row.style)
		label = render(row.label, doc) or row.label

		if row.button_type == "Link":
			url = (render(row.url_template, doc) or "").strip() if row.url_template else ""
			elements.append(
				bk.button(label, f"{ACTION_ID_PREFIX}_link_{index}", url=url or get_doc_url(doc), style=style)
			)
			continue

		if not row.action:
			continue

		action = frappe.get_cached_doc("Slack Action", row.action)
		if not action.enabled:
			continue

		confirm = None
		if action.confirm:
			confirm = bk.confirm_dialog(
				render(action.confirm_text, doc) or _("This will change {0} {1}.").format(doc.doctype, doc.name),
				title=label,
				confirm_label=label,
			)

		value = json.dumps(
			{
				"action": row.action,
				"doctype": doc.doctype,
				"docname": doc.name,
				"rule": rule_name,
			},
			separators=(",", ":"),
		)

		elements.append(
			bk.button(label, f"{ACTION_ID_PREFIX}_{index}", value=value, style=style, confirm=confirm)
		)

	return bk.actions(elements)


def resolved_blocks(blocks: list[dict], audit_line: str) -> list[dict]:
	"""Strip interactive elements and append an audit line.

	Used after an action so the message shows what happened and cannot be clicked twice.
	"""
	kept = [b for b in blocks if b.get("type") != "actions"]
	kept.append(bk.context(audit_line))
	return bk.clamp_blocks(kept)
