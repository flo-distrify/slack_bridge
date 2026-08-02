# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SlackUser(Document):
	def validate(self):
		self.validate_unique_mapping()

	def validate_unique_mapping(self):
		"""One Frappe user per Slack account per workspace, in both directions."""
		duplicate = frappe.db.exists(
			"Slack User",
			{
				"workspace": self.workspace,
				"user": self.user,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				_("{0} is already mapped in workspace {1} ({2}).").format(
					self.user, self.workspace, duplicate
				)
			)

		duplicate = frappe.db.exists(
			"Slack User",
			{
				"workspace": self.workspace,
				"slack_user_id": self.slack_user_id,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				_("Slack user {0} is already mapped to another Frappe user ({1}).").format(
					self.slack_user_id, duplicate
				)
			)
