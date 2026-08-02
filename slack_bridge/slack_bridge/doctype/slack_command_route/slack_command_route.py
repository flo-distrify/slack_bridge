# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SlackCommandRoute(Document):
	def validate(self):
		self.normalise_command()
		self.validate_unique_route()

		if self.route_type == "Run Method" and self.method:
			try:
				frappe.get_attr(self.method)
			except Exception:
				frappe.throw(_("Cannot resolve method {0}.").format(self.method))

	def normalise_command(self):
		command = (self.command or "").strip().lower()
		if command and not command.startswith("/"):
			command = "/" + command
		self.command = command

		self.subcommand = (self.subcommand or "").strip().lower()

	def validate_unique_route(self):
		duplicate = frappe.db.exists(
			"Slack Command Route",
			{
				"workspace": self.workspace,
				"command": self.command,
				"subcommand": self.subcommand or "",
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				_("{0} {1} is already routed by {2}.").format(
					self.command, self.subcommand or "(default)", duplicate
				)
			)

	def on_update(self):
		# Slash commands are declared in the manifest, so it needs rebuilding.
		frappe.clear_cache(doctype="Slack Workspace")
