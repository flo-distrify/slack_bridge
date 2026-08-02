# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

import time
import unittest

from slack_bridge.slack.verify import (
	SlackSignatureError,
	compute_signature,
	verify_request,
)

# The worked example from Slack's own verification documentation.
SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BODY = (
	b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow"
	b"&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner"
	b"&command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands"
	b"%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
TIMESTAMP = "1531420618"
EXPECTED = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"


class TestSignatureVerification(unittest.TestCase):
	def test_matches_slack_reference_vector(self):
		self.assertEqual(compute_signature(SECRET, TIMESTAMP, BODY), EXPECTED)

	def test_accepts_fresh_request(self):
		now = str(int(time.time()))
		signature = compute_signature(SECRET, now, b"hello")
		# Does not raise.
		verify_request(SECRET, now, signature, b"hello")

	def test_rejects_tampered_body(self):
		now = str(int(time.time()))
		signature = compute_signature(SECRET, now, b"hello")

		with self.assertRaises(SlackSignatureError):
			verify_request(SECRET, now, signature, b"hello, but modified")

	def test_rejects_wrong_secret(self):
		now = str(int(time.time()))
		signature = compute_signature("someone-elses-secret", now, b"hello")

		with self.assertRaises(SlackSignatureError):
			verify_request(SECRET, now, signature, b"hello")

	def test_rejects_replay_outside_window(self):
		stale = str(int(time.time()) - 60 * 10)
		signature = compute_signature(SECRET, stale, b"hello")

		with self.assertRaises(SlackSignatureError):
			verify_request(SECRET, stale, signature, b"hello")

	def test_rejects_missing_headers(self):
		with self.assertRaises(SlackSignatureError):
			verify_request(SECRET, None, None, b"hello")

	def test_rejects_missing_secret(self):
		now = str(int(time.time()))
		with self.assertRaises(SlackSignatureError):
			verify_request("", now, compute_signature(SECRET, now, b"x"), b"x")
