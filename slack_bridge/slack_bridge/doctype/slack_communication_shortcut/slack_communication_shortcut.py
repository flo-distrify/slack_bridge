# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# Slack app manifest limits for message shortcuts.
MAX_LABEL = 24
MAX_DESCRIPTION = 50
MAX_SEARCH_LIMIT = 15

SEARCHABLE_FIELDTYPES = {"Data", "Small Text", "Text", "Long Text", "Text Editor", "Link", "Select"}


class SlackCommunicationShortcut(Document):
	def validate(self):
		if len(self.shortcut_label or "") > MAX_LABEL:
			frappe.throw(_("Shortcut Label must be at most {0} characters (Slack limit).").format(MAX_LABEL))

		if len(self.shortcut_description or "") > MAX_DESCRIPTION:
			frappe.throw(
				_("Shortcut Description must be at most {0} characters (Slack limit).").format(
					MAX_DESCRIPTION
				)
			)

		self.search_limit = min(max(self.search_limit or 8, 1), MAX_SEARCH_LIMIT)
		self.min_query_length = max(self.min_query_length or 1, 1)

		for row in self.party_doctypes:
			self.validate_party_row(row)

	def validate_party_row(self, row):
		"""Fail at config time, not at picker-keystroke time."""
		meta = frappe.get_meta(row.ref_doctype)

		for fieldname in split_fields(row.search_fields):
			field = meta.get_field(fieldname)
			if fieldname != "name" and (not field or field.fieldtype not in SEARCHABLE_FIELDTYPES):
				frappe.throw(
					_("Row {0}: {1} has no searchable field named {2}.").format(
						row.idx, row.ref_doctype, fieldname
					)
				)

		if row.display_field and row.display_field != "name" and not meta.get_field(row.display_field):
			frappe.throw(
				_("Row {0}: {1} has no field named {2}.").format(row.idx, row.ref_doctype, row.display_field)
			)


def split_fields(value: str) -> list[str]:
	return [part.strip() for part in (value or "").split(",") if part.strip()]
