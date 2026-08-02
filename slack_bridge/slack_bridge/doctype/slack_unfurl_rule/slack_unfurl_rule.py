# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from slack_bridge.engine.context import validate_template_field


class SlackUnfurlRule(Document):
	def validate(self):
		validate_template_field(self.title_template, _("Title"))

		meta = frappe.get_meta(self.document_type)
		for row in self.fields:
			field = meta.get_field(row.fieldname)
			if not field and row.fieldname not in ("name", "owner", "creation", "modified", "status"):
				frappe.throw(_("{0} has no field called {1}.").format(self.document_type, row.fieldname))

			if not row.label:
				row.label = (field.label if field else None) or row.fieldname

		for row in self.get("buttons") or []:
			validate_template_field(row.url_template, _("Button URL (row {0})").format(row.idx))
