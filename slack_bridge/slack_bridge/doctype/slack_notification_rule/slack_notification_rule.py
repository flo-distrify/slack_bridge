# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from slack_bridge.engine.context import validate_condition_field, validate_template_field
from slack_bridge.engine.rules import clear_cache


class SlackNotificationRule(Document):
	def validate(self):
		self.validate_field_references()
		self.validate_templates()

		validate_condition_field(self.condition, _("Condition"))
		for row in self.recipients:
			validate_condition_field(row.condition, _("Recipient condition (row {0})").format(row.idx))
			validate_template_field(row.jinja_value, _("Recipient Jinja (row {0})").format(row.idx))

		for row in self.get("buttons") or []:
			validate_condition_field(row.condition, _("Button condition (row {0})").format(row.idx))
			validate_template_field(row.url_template, _("Button URL (row {0})").format(row.idx))
			if row.button_type == "Action" and row.action:
				self.validate_action_doctype(row)

	def validate_field_references(self):
		meta = frappe.get_meta(self.document_type)

		if self.event == "Value Change" and self.value_changed:
			if not meta.get_field(self.value_changed) and self.value_changed not in (
				"docstatus",
				"status",
			):
				frappe.throw(
					_("{0} has no field called {1}.").format(self.document_type, self.value_changed)
				)

		if self.event in ("Days Before", "Days After", "Minutes Before", "Minutes After"):
			if self.date_field and not meta.get_field(self.date_field):
				if self.date_field not in ("creation", "modified"):
					frappe.throw(
						_("{0} has no field called {1}.").format(self.document_type, self.date_field)
					)

	def validate_templates(self):
		validate_template_field(self.subject, _("Headline"))
		validate_template_field(self.message, _("Message"))

		if self.message_mode == "Block Kit":
			validate_template_field(self.blocks_template, _("Blocks Template"))
		elif not (self.subject or self.message):
			frappe.throw(_("Set a Headline or a Message."))

	def validate_action_doctype(self, row):
		action_doctype = frappe.db.get_value("Slack Action", row.action, "document_type")
		if action_doctype and action_doctype != self.document_type:
			frappe.throw(
				_("Button {0} runs an action for {1}, but this rule is for {2}.").format(
					row.idx, action_doctype, self.document_type
				)
			)

	def on_update(self):
		clear_cache(self.document_type)

		# On a retarget, the doctype the rule used to watch must be cleared too.
		previous = self.get_doc_before_save()
		if previous and previous.document_type != self.document_type:
			clear_cache(previous.document_type)

	def on_trash(self):
		clear_cache(self.document_type)

	@frappe.whitelist()
	def preview(self, docname: str | None = None) -> dict:
		"""Render this rule against a real document without sending anything."""
		frappe.only_for("System Manager")

		from slack_bridge.engine.context import evaluate_condition
		from slack_bridge.engine.render import render_message

		doc = self.get_sample_document(docname)
		blocks, text = render_message(self, doc)

		return {
			"docname": doc.name,
			"condition_passed": evaluate_condition(self.condition, doc),
			"blocks": blocks,
			"text": text,
		}

	@frappe.whitelist()
	def send_test(self, docname: str | None = None) -> dict:
		"""Render and deliver this rule for one document, ignoring its condition."""
		frappe.only_for("System Manager")

		from slack_bridge.engine import outbox, recipients
		from slack_bridge.engine.render import render_message
		from slack_bridge.slack.client import SlackClient

		doc = self.get_sample_document(docname)
		client = SlackClient(self.workspace)

		targets = recipients.resolve(self, doc, client)
		if not targets:
			frappe.throw(_("No recipients resolved for {0}.").format(doc.name))

		blocks, text = render_message(self, doc)

		sent = []
		for target in targets:
			outbox.queue(
				workspace=self.workspace,
				channel_id=target["channel_id"],
				blocks=blocks,
				text=text,
				rule=self.name,
				reference_doctype=doc.doctype,
				reference_name=doc.name,
				thread_mode="New Message",
				event_method="test",
			)
			sent.append(target["label"])

		return {"message": _("Queued for {0}").format(", ".join(sent))}

	def get_sample_document(self, docname: str | None = None):
		if not docname:
			docname = frappe.db.get_value(
				self.document_type, {}, "name", order_by="modified desc"
			)

		if not docname:
			frappe.throw(_("No {0} exists to test with.").format(self.document_type))

		return frappe.get_doc(self.document_type, docname)
