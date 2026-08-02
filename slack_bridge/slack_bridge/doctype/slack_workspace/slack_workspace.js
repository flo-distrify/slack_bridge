// Copyright (c) 2026, Automates UG and contributors
// For license information, please see license.txt

frappe.ui.form.on("Slack Workspace", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Connection"), () => {
			frm.call("test_connection").then((r) => {
				if (r.message) {
					frappe.show_alert({ message: r.message.message, indicator: "green" });
					frm.reload_doc();
				}
			});
		});

		frm.add_custom_button(__("Sync Users & Channels"), () => {
			frappe.show_alert(__("Syncing…"));
			frm.call("sync_directory").then((r) => {
				if (r.message) {
					frappe.msgprint(r.message.message, __("Sync complete"));
				}
			});
		});

		frm.add_custom_button(__("Create Starter Commands"), () => {
			frappe.prompt(
				{
					fieldname: "command",
					label: __("Slash Command"),
					fieldtype: "Data",
					default: "/erp",
					reqd: 1,
				},
				({ command }) => {
					frm.call("create_starter_configuration", { command }).then((r) => {
						if (r.message) frappe.msgprint(r.message.message, __("Starter commands"));
					});
				},
				__("Create Starter Commands")
			);
		});

		frm.add_custom_button(__("Copy Manifest"), () => {
			frm.call("refresh_manifest").then((r) => {
				const manifest = r.message || frm.doc.app_manifest;
				frappe.utils.copy_to_clipboard(manifest);
				frappe.msgprint({
					title: __("Manifest copied"),
					indicator: "green",
					message: __(
						"Now open <a href='https://api.slack.com/apps' target='_blank'>api.slack.com/apps</a>, choose <b>Create New App</b> → <b>From a manifest</b>, and paste it."
					),
				});
			});
		});

		if (frm.doc.connection_status === "Connected") {
			frm.dashboard.set_headline_safe(
				__("Connected to <b>{0}</b>", [frappe.utils.escape_html(frm.doc.team_name || "")])
			);
		} else if (frm.doc.bot_token) {
			frm.dashboard.set_headline_safe(
				__("Not verified yet — click <b>Test Connection</b>.")
			);
		}
	},
});
