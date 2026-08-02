# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from slack_bridge.engine.context import validate_template_field

# Slack rejects modal titles longer than 24 characters.
MAX_MODAL_TITLE = 24


class SlackForm(Document):
	def validate(self):
		validate_template_field(self.after_submit_message, _("Confirmation Message"))

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
				frappe.throw(
					_("{0} has no field called {1}.").format(self.document_type, row.fieldname)
				)

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

	def get_modal_title(self) -> str:
		return (self.modal_title or self.title)[:MAX_MODAL_TITLE]
