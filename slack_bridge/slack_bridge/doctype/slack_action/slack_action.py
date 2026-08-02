# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from slack_bridge.engine.context import validate_template_field


class SlackAction(Document):
	def validate(self):
		validate_template_field(self.value_to_set, _("Value"))
		validate_template_field(self.success_message, _("Success Message"))
		validate_template_field(self.confirm_text, _("Confirmation Text"))

		if self.action_type == "Set Field Value" and self.field_to_set:
			meta = frappe.get_meta(self.document_type)
			if not meta.get_field(self.field_to_set):
				frappe.throw(_("{0} has no field called {1}.").format(self.document_type, self.field_to_set))

		if self.action_type == "Run Method" and self.method:
			# Fail at save time rather than when somebody clicks the button in Slack.
			try:
				frappe.get_attr(self.method)
			except Exception:
				frappe.throw(_("Cannot resolve method {0}.").format(self.method))

		if self.collect_input_form:
			form_doctype = frappe.db.get_value("Slack Form", self.collect_input_form, "document_type")
			if form_doctype and form_doctype != self.document_type:
				frappe.throw(
					_("The input form targets {0}, but this action is for {1}.").format(
						form_doctype, self.document_type
					)
				)
