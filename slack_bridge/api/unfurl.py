# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Rich previews for ERP document links pasted into Slack.

Unfurling shows document data to everyone in the conversation, so the person who shared
the link must be a linked user with read permission. Without that we stay silent rather
than leaking fields to a channel.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import frappe
from frappe.utils import format_datetime, formatdate

from slack_bridge.engine.context import get_doc_url, render
from slack_bridge.engine.render import build_buttons
from slack_bridge.slack import blocks as bk
from slack_bridge.slack.client import SlackClient

SLUG_CACHE_KEY = "slack_bridge_doctype_slugs"


def unfurl_links(workspace: str, event: dict) -> str:
	from slack_bridge.engine.users import get_frappe_user

	links = event.get("links") or []
	if not links:
		return "No links"

	user = get_frappe_user(workspace, event.get("user"))
	if not user:
		return "Sharer is not a linked user"

	unfurls = {}
	original_user = frappe.session.user

	try:
		frappe.set_user(user)

		for link in links:
			url = link.get("url")
			target = parse_document_url(url)
			if not target:
				continue

			doctype, docname = target
			card = build_card(doctype, docname, workspace)
			if card:
				unfurls[url] = card
	finally:
		frappe.set_user(original_user)

	if not unfurls:
		return "Nothing to unfurl"

	SlackClient(workspace).unfurl(
		channel=event.get("channel"),
		ts=event.get("message_ts"),
		unfurls=unfurls,
		unfurl_id=event.get("unfurl_id"),
		source=event.get("source"),
	)

	return f"Unfurled {len(unfurls)} link(s)"


def parse_document_url(url: str) -> tuple[str, str] | None:
	"""Extract (doctype, docname) from a Desk URL such as /app/sales-order/SO-0001."""
	if not url:
		return None

	site_host = urlparse(frappe.utils.get_url()).hostname
	parsed = urlparse(url)

	if site_host and parsed.hostname and parsed.hostname != site_host:
		return None

	parts = [p for p in parsed.path.split("/") if p]
	if len(parts) < 3 or parts[0] != "app":
		return None

	doctype = doctype_from_slug(parts[1])
	if not doctype:
		return None

	docname = unquote(parts[2])
	return (doctype, docname) if frappe.db.exists(doctype, docname) else None


def doctype_from_slug(slug: str) -> str | None:
	guess = slug.replace("-", " ").title()
	if frappe.db.exists("DocType", guess):
		return guess

	slugs = frappe.cache().get_value(SLUG_CACHE_KEY)
	if slugs is None:
		slugs = {
			name.lower().replace(" ", "-"): name for name in frappe.get_all("DocType", pluck="name")
		}
		frappe.cache().set_value(SLUG_CACHE_KEY, slugs, expires_in_sec=3600)

	return slugs.get(slug.lower())


def build_card(doctype: str, docname: str, workspace: str) -> dict | None:
	rule_name = frappe.db.get_value(
		"Slack Unfurl Rule", {"document_type": doctype, "enabled": 1}, "name"
	)
	if not rule_name:
		return None

	rule = frappe.get_cached_doc("Slack Unfurl Rule", rule_name)

	if rule.workspace and rule.workspace != workspace:
		return None

	if rule.required_role and rule.required_role not in frappe.get_roles(frappe.session.user):
		return None

	if not frappe.has_permission(doctype, "read", doc=docname):
		return None

	doc = frappe.get_doc(doctype, docname)
	url = get_doc_url(doc)

	title = docname
	if rule.title_template:
		try:
			title = render(rule.title_template, doc) or docname
		except Exception:
			pass

	blocks = [bk.section(f"*<{url}|{title}>*")]

	rows = format_fields(rule, doc)
	if rows:
		blocks.append({"type": "section", "fields": rows})

	blocks.append(bk.context(f"{doctype} · {docname}"))

	if rule.show_buttons:
		blocks += build_buttons(rule.get("buttons"), doc)

	return {"blocks": bk.clamp_blocks(blocks)}


def format_fields(rule, doc) -> list[dict]:
	"""Slack renders at most ten fields, two per row."""
	meta = frappe.get_meta(doc.doctype)
	fields = []

	for row in rule.fields[:10]:
		value = doc.get(row.fieldname)
		if value in (None, ""):
			continue

		field = meta.get_field(row.fieldname)
		fields.append(
			{
				"type": "mrkdwn",
				"text": f"*{row.label or row.fieldname}*\n{format_value(value, field, doc)}",
			}
		)

	return fields


def format_value(value, field, doc) -> str:
	if field:
		if field.fieldtype in ("Currency", "Float", "Percent"):
			return frappe.utils.fmt_money(
				value,
				currency=doc.get("currency") if field.fieldtype == "Currency" else None,
			)
		if field.fieldtype == "Date":
			return formatdate(value)
		if field.fieldtype in ("Datetime", "Timestamp"):
			return format_datetime(value)
		if field.fieldtype == "Check":
			return "Yes" if value else "No"

	return bk.truncate(str(value), 200)
