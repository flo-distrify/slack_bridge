# Copyright (c) 2026, Automates UG and contributors
# For license information, please see license.txt

"""Document threads: the root message in a channel created on first use, replies both
ways, reactions as a desired state."""

import json
from unittest.mock import patch

import frappe

from slack_bridge import threads
from slack_bridge.api import events
from slack_bridge.engine import outbox
from slack_bridge.slack.manifest import BOT_SCOPES, SUBSCRIBED_EVENTS
from slack_bridge.tests import SlackBridgeTestCase
from slack_bridge.tests.fixtures import (
	WORKSPACE,
	FakeResponse,
	ensure_slack_user,
	ensure_user,
	ensure_workspace,
	make_todo,
)
from slack_bridge.tests.test_inbound import slack_request

AGENT = "threads-agent@example.com"
AGENT_SLACK_ID = "U0THREADAGENT"
STRANGER_SLACK_ID = "U0STRANGER"
RECEIVED: list[dict] = []


def record_reply(doctype, name, message):
	RECEIVED.append(
		{"doctype": doctype, "name": name, "message": message, "session_user": frappe.session.user}
	)


def failing_reply(doctype, name, message):
	raise frappe.ValidationError("the ticket is gone")


class FakeSlackApi:
	"""Answers the Web API by method name and remembers every call."""

	def __init__(self):
		self.calls: list[tuple[str, dict]] = []
		self.channels: list[dict] = []
		self.errors: dict[str, list[str]] = {}
		self.ts = 1700000000

	def __call__(self, url, headers=None, data=None, timeout=None):
		method = url.rsplit("/", 1)[-1]
		params = json.loads(data) if isinstance(data, str) else dict(data or {})
		self.calls.append((method, params))
		queued = self.errors.get(method)
		if queued:
			return FakeResponse({"ok": False, "error": queued.pop(0)})
		return FakeResponse(getattr(self, method.replace(".", "_"), self.ok)(params))

	def ok(self, params):
		return {"ok": True}

	def conversations_list(self, params):
		return {"ok": True, "channels": self.channels}

	def conversations_create(self, params):
		channel = {"id": f"C0NEW{len(self.channels)}", "name": params["name"], "is_member": True}
		self.channels.append(channel)
		return {"ok": True, "channel": channel}

	def chat_postMessage(self, params):
		self.ts += 1
		return {"ok": True, "ts": f"{self.ts}.000100", "channel": params["channel"]}

	def users_info(self, params):
		return {"ok": True, "user": {"id": params["user"], "real_name": "Sam Stranger"}}

	def named(self, method):
		return [params for name, params in self.calls if name == method]


class TestDocumentThreads(SlackBridgeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_workspace()
		ensure_user(AGENT)
		ensure_slack_user(AGENT, AGENT_SLACK_ID)

	def setUp(self):
		frappe.set_user("Administrator")
		# The outbox commits when it defers or gives up; on a real site that would leave
		# this suite's rows (and the enabled test workspace) behind.
		commits = patch.object(frappe.db, "commit")
		commits.start()
		self.addCleanup(commits.stop)
		frappe.db.set_value("Slack Workspace", WORKSPACE, "enabled", 1)
		frappe.clear_document_cache("Slack Workspace", WORKSPACE)
		frappe.db.set_single_value("Slack Bridge Settings", {"enabled": 1, "default_workspace": WORKSPACE})
		frappe.cache().delete_value(outbox.PACING_CACHE)
		RECEIVED.clear()
		self.token = frappe.generate_hash(length=8).lower()
		self.channel = f"customer_{self.token}"
		self.todo = make_todo(f"Thread {self.token}")
		self.api = FakeSlackApi()
		patcher = patch("slack_bridge.slack.client.requests.post", side_effect=self.api)
		patcher.start()
		self.addCleanup(patcher.stop)

	def post(self, text="A new ticket", **kwargs):
		return threads.post("ToDo", self.todo.name, self.channel, text, **kwargs)

	def log(self, name):
		return frappe.get_doc("Slack Message Log", name)

	def deliver_root(self, **kwargs):
		name = self.post(**kwargs)
		outbox.deliver(name)
		return self.log(name)

	# ------------------------------------------------------------------ channel

	def test_the_first_message_creates_the_channel_and_invites_the_team(self):
		name = self.post(invite=[AGENT, "nobody@example.com"], purpose="Tickets of Acme")
		queued = self.log(name)
		self.assertEqual((queued.status, queued.thread_role, queued.channel_id), ("Queued", "Root", None))
		self.assertEqual(self.api.calls, [], "nothing talks to Slack before the delivery")

		outbox.deliver(name)

		created = self.api.named("conversations.create")
		self.assertEqual([(c["name"], c["is_private"]) for c in created], [(self.channel, False)])
		self.assertEqual(self.api.named("conversations.setPurpose")[0]["purpose"], "Tickets of Acme")
		self.assertEqual(self.api.named("conversations.invite")[0]["users"], AGENT_SLACK_ID)
		sent = self.log(name)
		self.assertEqual(sent.status, "Sent")
		self.assertEqual(sent.channel_id, created and self.api.channels[0]["id"])
		self.assertEqual(self.api.named("chat.postMessage")[0]["channel"], sent.channel_id)
		self.assertTrue(sent.message_ts)
		row = frappe.db.get_value(
			"Slack Channel",
			{"workspace": WORKSPACE, "channel_name": self.channel},
			["channel_id", "is_member"],
			as_dict=True,
		)
		self.assertEqual((row.channel_id, row.is_member), (sent.channel_id, 1))

		# the next document of the same customer uses the channel that now exists
		other = make_todo(f"Thread {self.token} second")
		second = threads.post("ToDo", other.name, self.channel, "Another ticket")
		frappe.cache().delete_value(outbox.PACING_CACHE)
		outbox.deliver(second)
		self.assertEqual(len(self.api.named("conversations.create")), 1)
		self.assertEqual(self.log(second).channel_id, sent.channel_id)

	def test_a_channel_made_by_hand_is_joined_not_created(self):
		self.api.channels.append({"id": "C0BYHAND", "name": self.channel, "is_member": False})
		log = self.deliver_root()
		self.assertEqual(self.api.named("conversations.create"), [])
		self.assertEqual(self.api.named("conversations.join"), [{"channel": "C0BYHAND"}])
		self.assertEqual((log.status, log.channel_id), ("Sent", "C0BYHAND"))

	def test_a_channel_created_at_the_same_moment_is_used(self):
		def create_loses(params):
			self.api.channels.append({"id": "C0RACE", "name": params["name"], "is_member": False})
			return {"ok": False, "error": "name_taken"}

		self.api.conversations_create = create_loses
		log = self.deliver_root()
		self.assertEqual((log.status, log.channel_id), ("Sent", "C0RACE"))

	def test_a_missing_channel_is_not_created_when_told_so(self):
		name = self.post(create_channel=False)
		outbox.deliver(name)
		log = self.log(name)
		self.assertEqual(log.status, "Dead")  # nothing a retry could change
		self.assertIn("no Slack channel", log.last_error)
		self.assertEqual(self.api.named("conversations.create"), [])

	def test_channel_names_are_made_valid(self):
		self.assertEqual(
			threads.clean_channel_name("#Customer_CT Distribution GmbH"), "customer_ct-distribution-gmbh"
		)
		self.assertEqual(len(threads.clean_channel_name("x" * 200)), 80)

	# --------------------------------------------------------------------- root

	def test_a_second_post_rewrites_the_same_message(self):
		log = self.deliver_root(text="Open")
		again = self.post(text="Taken by Jane")
		self.assertEqual(again, log.name)
		self.assertEqual(frappe.db.count("Slack Message Log", {"reference_name": self.todo.name}), 1)

		threads.push_root_update(log.name)
		update = self.api.named("chat.update")[0]
		self.assertEqual((update["channel"], update["ts"]), (log.channel_id, log.message_ts))
		self.assertEqual(update["text"], "Taken by Jane")

	def test_a_rewrite_before_delivery_is_what_gets_delivered(self):
		name = self.post(text="Open")
		self.post(text="Taken by Jane")
		outbox.deliver(name)
		self.assertEqual([m["text"] for m in self.api.named("chat.postMessage")], ["Taken by Jane"])
		self.assertEqual(self.api.named("chat.update"), [])

	def test_a_root_that_was_given_up_on_is_revived_by_the_next_post(self):
		self.api.errors["conversations.create"] = ["missing_scope"]
		name = self.post()
		outbox.deliver(name)
		self.assertEqual(self.log(name).status, "Dead")
		self.assertIsNone(threads.reply("ToDo", self.todo.name, text="lost"))

		self.assertEqual(self.post(text="Second try"), name)
		self.assertEqual(self.log(name).status, "Queued")
		outbox.deliver(name)
		self.assertEqual(self.log(name).status, "Sent")

	def test_no_workspace_means_nothing_is_queued(self):
		frappe.db.set_single_value("Slack Bridge Settings", "enabled", 0)
		self.assertIsNone(self.post())
		self.assertFalse(threads.has_thread("ToDo", self.todo.name))

	# ------------------------------------------------------------------ replies

	def test_a_reply_waits_for_the_root_and_lands_in_its_thread(self):
		root = self.post()
		reply = threads.reply(
			"ToDo",
			self.todo.name,
			html="<p>Call <b>back</b> &amp; <a href='https://example.com/x'>see here</a></p><ul><li>one</li><li>two</li></ul>",
			author=AGENT,
			key="note-1",
		)
		self.assertEqual(self.log(reply).thread_role, "Reply")

		outbox.deliver(reply)
		waiting = self.log(reply)
		self.assertEqual(waiting.status, "Queued")
		self.assertTrue(waiting.next_attempt_at)
		self.assertEqual(waiting.attempts or 0, 0, "waiting for the root costs no attempt")
		self.assertEqual(self.api.named("chat.postMessage"), [])

		outbox.deliver(root)
		frappe.cache().delete_value(outbox.PACING_CACHE)
		outbox.deliver(reply)
		sent_root, sent_reply = self.log(root), self.log(reply)
		self.assertEqual(sent_reply.status, "Sent")
		message = self.api.named("chat.postMessage")[1]
		self.assertEqual(
			(message["channel"], message["thread_ts"]), (sent_root.channel_id, sent_root.message_ts)
		)
		text = message["blocks"][0]["text"]["text"]
		self.assertEqual(
			text,
			f"*{frappe.utils.get_fullname(AGENT)}:* Call *back* &amp; <https://example.com/x|see here>\n\n• one\n• two",
		)

		# the same note is never posted twice; the same words under another key are
		self.assertIsNone(threads.reply("ToDo", self.todo.name, html="<p>again</p>", key="note-1"))
		self.assertTrue(threads.reply("ToDo", self.todo.name, text="ok", key="note-2"))
		self.assertTrue(threads.reply("ToDo", self.todo.name, text="ok", key="note-3"))

	def test_without_a_thread_there_is_nothing_to_reply_to(self):
		self.assertIsNone(threads.reply("ToDo", self.todo.name, text="hello"))
		self.assertFalse(threads.set_reactions("ToDo", self.todo.name, ["raised_hand"]))

	# ---------------------------------------------------------------- reactions

	def test_reactions_follow_the_wanted_state(self):
		log = self.deliver_root(reactions=["raised_hand"])
		self.assertEqual([r["name"] for r in self.api.named("reactions.add")], ["raised_hand"])
		self.assertEqual(self.log(log.name).reactions_applied, "raised_hand")

		threads.set_reactions("ToDo", self.todo.name, [":white_check_mark:", "raised_hand"])
		threads.apply_reactions(log.name)
		self.assertEqual(
			[r["name"] for r in self.api.named("reactions.add")], ["raised_hand", "white_check_mark"]
		)
		added = self.api.named("reactions.add")[-1]
		self.assertEqual((added["channel"], added["timestamp"]), (log.channel_id, log.message_ts))

		threads.set_reactions("ToDo", self.todo.name, [])
		threads.apply_reactions(log.name)
		self.assertEqual(
			sorted(r["name"] for r in self.api.named("reactions.remove")), ["raised_hand", "white_check_mark"]
		)
		self.assertFalse(self.log(log.name).reactions_applied)

	def test_reactions_set_before_delivery_arrive_with_the_message(self):
		name = self.post()
		threads.set_reactions("ToDo", self.todo.name, ["raised_hand"])
		self.assertEqual(self.api.named("reactions.add"), [])
		outbox.deliver(name)
		self.assertEqual([r["name"] for r in self.api.named("reactions.add")], ["raised_hand"])

	def test_a_reaction_slack_already_has_counts_as_set_and_a_rate_limit_is_retried(self):
		log = self.deliver_root()
		self.api.errors["reactions.add"] = ["already_reacted"]
		threads.set_reactions("ToDo", self.todo.name, ["raised_hand"])
		threads.apply_reactions(log.name)
		self.assertEqual(self.log(log.name).reactions_applied, "raised_hand")

		threads.set_reactions("ToDo", self.todo.name, ["raised_hand", "white_check_mark"])
		limited = FakeResponse({"ok": False}, status_code=429, headers={"Retry-After": "1"})
		with patch("slack_bridge.slack.client.requests.post", return_value=limited):
			threads.apply_reactions(log.name)
		self.assertEqual(self.log(log.name).reactions_applied, "raised_hand")

		threads.reconcile()  # the outbox cron tries again
		self.assertEqual(self.log(log.name).reactions_applied, "raised_hand,white_check_mark")

	# ------------------------------------------------------------------ inbound

	def reply_event(self, log, **overrides):
		event = {
			"type": "message",
			"user": AGENT_SLACK_ID,
			"channel": log.channel_id,
			"ts": "1800000000.000200",
			"thread_ts": log.message_ts,
			"text": "Customer called &lt;again&gt;, <@" + AGENT_SLACK_ID + "> takes it\n• first\n• second",
			"event_ts": "1800000000.000200",
		}
		event.update(overrides)
		return event

	def with_handler(self, path="slack_bridge.tests.test_threads.record_reply"):
		original = frappe.get_hooks

		def hooks(hook=None, *args, **kwargs):
			if hook == threads.THREAD_REPLY_HOOK:
				return {"ToDo": [path]}
			return original(hook, *args, **kwargs) if hook else original()

		return patch.object(frappe, "get_hooks", side_effect=hooks)

	def test_a_reply_in_the_thread_reaches_the_owning_app_as_the_linked_user(self):
		log = self.deliver_root()
		frappe.db.set_value(
			"Slack User",
			{"slack_user_id": AGENT_SLACK_ID, "workspace": WORKSPACE},
			"slack_real_name",
			"Thread Agent",
		)
		with self.with_handler():
			result = events.handle_message(WORKSPACE, self.reply_event(log))

		self.assertIn(self.todo.name, result)
		self.assertEqual(len(RECEIVED), 1)
		got = RECEIVED[0]
		self.assertEqual((got["doctype"], got["name"], got["session_user"]), ("ToDo", self.todo.name, AGENT))
		message = got["message"]
		self.assertEqual(message["user"], AGENT)
		self.assertEqual(message["sender_name"], frappe.utils.get_fullname(AGENT))
		self.assertEqual(
			message["text"], "Customer called <again>, @Thread Agent takes it\n• first\n• second"
		)
		self.assertEqual(
			message["html"],
			"<div>Customer called &lt;again&gt;, @Thread Agent takes it</div><ul><li>first</li><li>second</li></ul>",
		)
		self.assertEqual(frappe.session.user, "Administrator", "the session is put back")

	def test_a_reply_from_an_unlinked_person_carries_the_slack_name(self):
		log = self.deliver_root()
		event = self.reply_event(
			log,
			user=STRANGER_SLACK_ID,
			text="see file",
			subtype="file_share",
			files=[{"name": "quote.pdf", "permalink": "https://slack.example/files/quote.pdf"}],
		)
		with self.with_handler():
			events.handle_message(WORKSPACE, event)
		message = RECEIVED[0]["message"]
		self.assertIsNone(message["user"])
		self.assertEqual(message["sender_name"], "Sam Stranger")
		self.assertEqual(
			message["files"], [{"name": "quote.pdf", "url": "https://slack.example/files/quote.pdf"}]
		)
		self.assertEqual(RECEIVED[0]["session_user"], "Administrator")

	def test_only_replies_in_document_threads_are_taken(self):
		log = self.deliver_root()
		reply = self.reply_event(log)
		for label, event in {
			"a top-level channel message": self.reply_event(log, thread_ts=None),
			"the root itself": self.reply_event(log, ts=log.message_ts),
			"a bot message": self.reply_event(log, bot_id="B0BOT"),
			"an edit": self.reply_event(log, subtype="message_changed"),
			"a thread that is no document thread": self.reply_event(log, thread_ts="1600000000.000100"),
			"another channel": self.reply_event(log, channel="C0ELSEWHERE"),
		}.items():
			self.assertFalse(threads.wants_event(WORKSPACE, event), label)
		self.assertTrue(threads.wants_event(WORKSPACE, reply))

	def test_channel_chatter_leaves_no_trace_at_the_endpoint(self):
		log = self.deliver_root()
		workspace = frappe.get_doc("Slack Workspace", WORKSPACE)
		before = frappe.db.count("Slack Interaction Log")

		chatter = {
			"type": "event_callback",
			"event_id": f"Ev{self.token}a",
			"event": self.reply_event(log, thread_ts=None),
		}
		slack_request(json.dumps(chatter).encode(), workspace)
		events.handle()
		self.assertEqual(frappe.db.count("Slack Interaction Log"), before)

		ours = {"type": "event_callback", "event_id": f"Ev{self.token}b", "event": self.reply_event(log)}
		slack_request(json.dumps(ours).encode(), workspace)
		events.handle()
		self.assertEqual(frappe.db.count("Slack Interaction Log"), before + 1)

	def test_a_reply_the_app_could_not_take_is_flagged_in_slack(self):
		log = self.deliver_root()
		with self.with_handler("slack_bridge.tests.test_threads.failing_reply"):
			with self.assertRaises(frappe.ValidationError):
				events.handle_message(WORKSPACE, self.reply_event(log))
		flagged = self.api.named("reactions.add")[-1]
		self.assertEqual((flagged["name"], flagged["timestamp"]), ("warning", "1800000000.000200"))
		self.assertEqual(frappe.session.user, "Administrator")

	def test_a_doctype_nobody_listens_to_is_ignored(self):
		log = self.deliver_root()
		self.assertIn("No handler", events.handle_message(WORKSPACE, self.reply_event(log)))

	# ---------------------------------------------------------------- formatting

	def test_html_becomes_mrkdwn(self):
		self.assertEqual(
			threads.html_to_mrkdwn("plain &lt;text&gt; &amp; more"), "plain &lt;text&gt; &amp; more"
		)
		self.assertEqual(threads.html_to_mrkdwn("Tom & Jerry"), "Tom &amp; Jerry")
		self.assertEqual(
			threads.html_to_mrkdwn("<div>One<br>two</div><p><em>soft</em> and <code>x = 1</code></p>"),
			"One\ntwo\n\n_soft_ and `x = 1`",
		)
		self.assertEqual(
			threads.html_to_mrkdwn("<ol><li>first</li><li>second<ul><li>inner</li></ul></li></ol>"),
			"1. first\n2. second\n\n    • inner",
		)
		self.assertEqual(
			threads.html_to_mrkdwn(
				'<p>See <a href="javascript:alert(1)">this</a> <img src="/files/a.png"></p>'
			),
			"See this _(image)_",
		)

	def test_the_manifest_asks_for_what_threads_need(self):
		for scope in ("channels:history", "channels:manage", "reactions:write"):
			self.assertIn(scope, BOT_SCOPES)
		self.assertIn("message.channels", SUBSCRIBED_EVENTS)
