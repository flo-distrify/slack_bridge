# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

MAX_MODAL_TITLE = 24

METHOD_FIELDS = ("options_method", "schema_method", "submit_method")


class SlackDynamicForm(Document):
	def validate(self):
		# Resolve the provider paths now so a typo fails in the form, not mid-conversation.
		for fieldname in METHOD_FIELDS:
			path = (self.get(fieldname) or "").strip()
			self.set(fieldname, path)

			try:
				resolved = frappe.get_attr(path)
			except Exception:
				frappe.throw(_("Cannot resolve method {0}.").format(path))

			if not callable(resolved):
				frappe.throw(_("{0} is not callable.").format(path))

		if self.modal_title and len(self.modal_title) > MAX_MODAL_TITLE:
			frappe.throw(
				_("Modal Title must be at most {0} characters (Slack limit).").format(MAX_MODAL_TITLE)
			)

		if (self.min_query_length or 0) < 0:
			self.min_query_length = 0
