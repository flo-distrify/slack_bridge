# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from slack_bridge.engine.context import validate_template_field

# Slack rejects modal titles longer than 24 characters.
MAX_MODAL_TITLE = 24
# Slack's caps for shortcut menu entries.
MAX_SHORTCUT_LABEL = 24
MAX_SHORTCUT_DESCRIPTION = 50

PREFILL_INPUT_TYPES = ("Text", "Long Text")


class SlackForm(Document):
	def validate(self):
		validate_template_field(self.after_submit_message, _("Confirmation Message"))
		self.validate_shortcut()

		if self.modal_title and len(self.modal_title) > MAX_MODAL_TITLE:
			frappe.throw(
				_("Modal Title must be at most {0} characters (Slack's limit).").format(MAX_MODAL_TITLE)
			)

		meta = frappe.get_meta(self.document_type)
		seen = set()

		for row in self.fields:
			validate_template_field(row.default_value, _("Default (row {0})").format(row.idx))

			if row.fieldname in seen:
				frappe.throw(_("Field {0} is listed twice.").format(row.fieldname))
			seen.add(row.fieldname)

			field = meta.get_field(row.fieldname)
			if not field:
				frappe.throw(_("{0} has no field called {1}.").format(self.document_type, row.fieldname))

			if not row.label:
				row.label = field.label or row.fieldname

			if row.input_type == "Select" and not row.options:
				# Fall back to the doctype's own options so authors don't retype them.
				row.options = field.options or ""
				if not row.options:
					frappe.throw(_("Set options for the Select field {0}.").format(row.fieldname))

			if row.input_type == "Document Link" and not row.options:
				row.options = field.options or ""
				if not row.options:
					frappe.throw(_("Set the target DocType for {0}.").format(row.fieldname))

	def validate_shortcut(self):
		if not self.message_shortcut:
			return

		if not self.shortcut_label or len(self.shortcut_label) > MAX_SHORTCUT_LABEL:
			frappe.throw(
				_("Shortcut Label is required and limited to {0} characters (Slack limit).").format(
					MAX_SHORTCUT_LABEL
				)
			)

		if self.shortcut_description and len(self.shortcut_description) > MAX_SHORTCUT_DESCRIPTION:
			frappe.throw(
				_("Shortcut Description is limited to {0} characters (Slack limit).").format(
					MAX_SHORTCUT_DESCRIPTION
				)
			)

		if not self.shortcut_callback_id:
			frappe.throw(_("Set a Shortcut Callback Id."))

		# Callback ids route incoming shortcut payloads, so they must be unique across
		# everything that registers shortcuts in the manifest.
		other_form = frappe.db.get_value(
			"Slack Form",
			{"shortcut_callback_id": self.shortcut_callback_id, "name": ["!=", self.name]},
			"name",
		)
		comm_shortcut = frappe.db.get_value(
			"Slack Communication Shortcut", {"callback_id": self.shortcut_callback_id}, "name"
		)
		if other_form or comm_shortcut:
			frappe.throw(
				_("Callback id {0} is already used by {1}.").format(
					self.shortcut_callback_id, other_form or comm_shortcut
				)
			)

		if self.shortcut_prefill_field:
			row = next((r for r in self.fields if r.fieldname == self.shortcut_prefill_field), None)
			if not row:
				frappe.throw(
					_("Prefill Field {0} is not one of this form's fields.").format(
						self.shortcut_prefill_field
					)
				)
			if row.input_type not in PREFILL_INPUT_TYPES:
				frappe.throw(
					_("Prefill Field {0} must be a Text or Long Text input.").format(
						self.shortcut_prefill_field
					)
				)

	def on_update(self):
		# Shortcut entries are declared in the manifest, so it needs rebuilding.
		if self.message_shortcut or (self.get_doc_before_save() or frappe._dict()).get("message_shortcut"):
			frappe.clear_cache(doctype="Slack Workspace")

	def get_modal_title(self) -> str:
		return (self.modal_title or self.title)[:MAX_MODAL_TITLE]
