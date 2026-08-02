// Copyright (c) 2026, Automates UG and contributors
// For license information, please see license.txt

frappe.ui.form.on("Slack Notification Rule", {
	refresh(frm) {
		frm.set_query("channel", "recipients", () => ({
			filters: { workspace: frm.doc.workspace, is_archived: 0 },
		}));

		frm.set_query("action", "buttons", () => ({
			filters: { document_type: frm.doc.document_type, enabled: 1 },
		}));
	},

	document_type(frm) {
		// Field pickers are only useful once the doctype is known.
		frm.refresh_field("value_changed");
	},
});
