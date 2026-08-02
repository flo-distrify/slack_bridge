# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from slack_bridge.slack.manifest import endpoint_url, manifest_json


class SlackWorkspace(Document):
	def before_insert(self):
		if not self.endpoint_token:
			self.endpoint_token = secrets.token_urlsafe(32)

	def validate(self):
		if not self.endpoint_token:
			self.endpoint_token = secrets.token_urlsafe(32)

		self.app_manifest = manifest_json(self)
		self.request_urls = self.build_request_urls_html()

	def on_update(self):
		# The manifest embeds this workspace's endpoint URLs, so clear anything cached.
		frappe.clear_cache(doctype="Slack Workspace")

	def build_request_urls_html(self) -> str:
		rows = [
			(_("Events"), endpoint_url("events.handle", self.endpoint_token)),
			(_("Interactivity"), endpoint_url("interactive.handle", self.endpoint_token)),
			(_("Slash Commands"), endpoint_url("commands.handle", self.endpoint_token)),
		]

		items = "".join(
			f"<tr><td style='padding-right:12px'><b>{label}</b></td>"
			f"<td><code style='word-break:break-all'>{url}</code></td></tr>"
			for label, url in rows
		)

		return (
			"<p class='text-muted small'>The manifest already contains these URLs. "
			"They are listed here in case you configure the Slack app by hand.</p>"
			f"<table class='table table-bordered' style='font-size:12px'>{items}</table>"
		)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Call auth.test and store the workspace identity."""
		frappe.only_for("System Manager")

		from slack_bridge.slack.client import SlackClient, SlackError

		try:
			result = SlackClient(self).auth_test()
		except SlackError as e:
			self.db_set("connection_status", f"Failed: {e.code}", update_modified=False)
			self.db_set("last_connection_check", now_datetime(), update_modified=False)
			frappe.throw(str(e), title=_("Slack connection failed"))

		self.db_set(
			{
				"team_id": result.get("team_id"),
				"team_name": result.get("team"),
				"bot_user_id": result.get("user_id"),
				"app_id": result.get("app_id"),
				"connection_status": "Connected",
				"last_connection_check": now_datetime(),
			},
			update_modified=False,
		)

		return {
			"team": result.get("team"),
			"bot": result.get("user"),
			"message": _("Connected to {0} as {1}").format(result.get("team"), result.get("user")),
		}

	@frappe.whitelist()
	def sync_directory(self) -> dict:
		"""Pull channels and match users by email."""
		frappe.only_for("System Manager")

		from slack_bridge.engine import users

		channels = users.sync_channels(self.name)
		mapped = users.sync_users(self.name)

		return {
			"channels": channels,
			"users": mapped,
			"message": _("Synced {0} channels and mapped {1} users.").format(channels, mapped),
		}

	@frappe.whitelist()
	def create_starter_configuration(self, command: str = "/erp") -> dict:
		"""Create the built-in slash commands so a fresh install is usable immediately."""
		frappe.only_for("System Manager")

		starters = [
			("link", "link", _("Connect your Slack account to your ERP user"), 0),
			("help", "help", _("List the available commands"), 0),
			("whoami", "whoami", _("Show which ERP user you are linked to"), 1),
			("subscriptions", "subscriptions", _("List notification rules and this channel's subscriptions"), 1),
			("subscribe", "subscribe", _("Send a notification rule to this channel"), 1),
			("unsubscribe", "unsubscribe", _("Stop sending a notification rule to this channel"), 1),
		]

		created = []
		for subcommand, builtin, help_text, require_link in starters:
			exists = frappe.db.exists(
				"Slack Command Route",
				{"workspace": self.name, "command": command, "subcommand": subcommand},
			)
			if exists:
				continue

			frappe.get_doc(
				{
					"doctype": "Slack Command Route",
					"workspace": self.name,
					"command": command,
					"subcommand": subcommand,
					"route_type": "Built-in",
					"builtin": builtin,
					"help_text": help_text,
					"require_account_link": require_link,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
			created.append(f"{command} {subcommand}")

		self.refresh_manifest()

		return {
			"created": created,
			"message": _("Created {0} commands. Re-paste the manifest in Slack so it registers {1}.").format(
				len(created), command
			)
			if created
			else _("All starter commands already exist."),
		}

	@frappe.whitelist()
	def refresh_manifest(self) -> str:
		"""Rebuild the manifest, e.g. after adding slash commands."""
		frappe.only_for("System Manager")

		self.app_manifest = manifest_json(self)
		self.db_set("app_manifest", self.app_manifest, update_modified=False)
		return self.app_manifest


def get_workspace_by_token(token: str):
	"""Resolve the endpoint token in a Slack Request URL to its workspace."""
	if not token:
		return None

	name = frappe.db.get_value("Slack Workspace", {"endpoint_token": token, "enabled": 1}, "name")
	return frappe.get_cached_doc("Slack Workspace", name) if name else None


def get_workspace_by_team(team_id: str):
	name = frappe.db.get_value("Slack Workspace", {"team_id": team_id, "enabled": 1}, "name")
	return frappe.get_cached_doc("Slack Workspace", name) if name else None
