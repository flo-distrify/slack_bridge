# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

# Frappe renamed its test base classes in v16; support both so one branch runs on either.
try:
	from frappe.tests import IntegrationTestCase as SlackBridgeTestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as SlackBridgeTestCase

__all__ = ["SlackBridgeTestCase"]
