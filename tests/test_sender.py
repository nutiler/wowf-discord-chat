"""Deterministic sender checks using fake HTTP responses. Never contacts Discord."""
from pathlib import Path
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from urllib import error

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "send_history.py"
spec = importlib.util.spec_from_file_location("dandy_send_history", SOURCE)
sender = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sender
spec.loader.exec_module(sender)
URL = "https://discord.com/api/webhooks/123456/" + "example_TOKEN_12345678901234567890"


class Response:
    def __init__(self, status=200, body=b'{"id":"999"}', headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, maximum):
        return self.body[:maximum]


class Opener:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def open(self, req, timeout):
        self.calls.append(req)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Stop:
    def __init__(self, stop_on_wait=False):
        self.waits, self.stop_on_wait = [], stop_on_wait

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stop_on_wait


class SenderTests(unittest.TestCase):
    def setUp(self):
        temporary = HERE.parent / "build" / "check-temp"
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = sender.Journal(self.root)
        self.entries = (sender.Entry(1, "A-Forever", "first", "MAGE", "Gnome", 3),
                        sender.Entry(2, "B-Forever", "second"))

    def run_send(self, opener, entries=None, stop=None):
        entries = entries or self.entries
        approval = sender.ReviewApproval(sender.entries_digest(entries), False)
        return sender.send_entries(URL, entries, self.journal, stop or Stop(), lambda text: None, opener, approval)

    def test_url_validation_is_strict_and_canonical(self):
        identity, endpoint = sender.validate_webhook(URL)
        self.assertEqual(identity, "123456")
        self.assertTrue(endpoint.endswith("?wait=true"))
        self.assertIn("/api/v10/webhooks/", endpoint)
        for invalid in (URL.replace("https:", "http:"), URL.replace("discord.com", "discord.com.evil.test"),
                        URL.replace("discord.com", "discord.com:443"), URL + "?wait=false",
                        URL.replace("discord.com", "user:secret@discord.com"), URL + "#secret",
                        URL.replace("/api/", "/api/v9/"), URL.replace("discord.com", "discordapp.com")):
            with self.subTest(url=invalid), self.assertRaises(sender.SenderError):
                sender.validate_webhook(invalid)

    def test_payload_escapes_markup_mentions_and_native_links(self):
        entry = sender.Entry(3, "Mage", '|cff123456*hello*|r @everyone <@123> |Hitem:123|h[Item]|h', "MAGE", "Troll", 3)
        payload = sender.make_payload(entry)
        self.assertEqual(payload["embeds"][0]["color"], 0x69CCF0)
        text = payload["embeds"][0]["description"]
        self.assertIn(r"\*hello\*", text)
        self.assertIn("@\u200beveryone", text)
        self.assertIn(r"\<@123\>", text)
        self.assertIn(r"\[Item\]", text)
        self.assertNotIn("|Hitem", text)
        self.assertEqual(payload["allowed_mentions"]["parse"], [])
        self.assertTrue(payload["avatar_url"].endswith("race_troll_female.jpg"))
        self.assertNotIn("timestamp", payload["embeds"][0])
        self.assertIn("avatar_url", sender.make_payload(self.entries[1]))
        self.assertEqual(payload["username"], "Mage")

    def test_history_is_frozen_preserves_order_and_rejects_duplicate_ids(self):
        path = self.root / "history.jsonl"
        rows = [{"id": 8, "sender": "A", "text": "first"}, {"id": 2, "sender": "B", "text": "second"}]
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        entries = sender.load_history(path)
        self.assertEqual([entry.id for entry in entries], [8, 2])
        with self.assertRaises(AttributeError):
            entries[0].text = "edited"
        rows[1]["id"] = 8
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        with self.assertRaises(sender.SenderError):
            sender.load_history(path)

    def test_success_order_receipts_and_idempotent_restart(self):
        opener = Opener(Response(body=b'{"id":"100"}'), Response(body=b'{"id":"101"}'))
        self.run_send(opener)
        self.assertEqual([json.loads(req.data)["embeds"][0]["description"] for req in opener.calls], ["first", "second"])
        receipts = self.journal._read(self.journal.sent_path)
        self.assertEqual([row["row_id"] for row in receipts], [1, 2])
        self.assertEqual([row["message_id"] for row in receipts], ["100", "101"])
        again = Opener()
        self.run_send(again)
        self.assertEqual(again.calls, [])
        audit = self.journal.sent_path.read_text() + self.journal.flight_path.read_text()
        self.assertNotIn("example_TOKEN", audit)
        self.assertNotIn("discord.com", audit)
        self.assertNotIn("first", audit)

    def test_content_hash_and_webhook_id_define_distinct_entries(self):
        self.run_send(Opener(Response()), entries=(self.entries[0],))
        edited = sender.Entry(1, "A-Forever", "corrected", "MAGE", "Gnome", 3)
        self.assertEqual(self.journal.pending("123456", (edited,)), (edited,))
        self.assertEqual(self.journal.pending("999", (self.entries[0],)), (self.entries[0],))

    def test_429_and_response_headers_control_waits(self):
        limited = error.HTTPError(URL, 429, "rate limited", {"Retry-After": "5"}, io.BytesIO(b'{"retry_after":2.5}'))
        opener = Opener(limited, Response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.25"}))
        stop = Stop()
        self.run_send(opener, entries=(self.entries[0],), stop=stop)
        self.assertEqual(stop.waits, [2.5, 1.25])
        self.assertEqual(len(self.journal._read(self.journal.sent_path)), 1)

    def test_stopping_after_429_leaves_entry_retryable(self):
        limited = error.HTTPError(URL, 429, "rate limited", {}, io.BytesIO(b'{"retry_after":1}'))
        self.run_send(Opener(limited), entries=(self.entries[0],), stop=Stop(True))
        self.assertEqual(self.journal.pending("123456", (self.entries[0],)), (self.entries[0],))

    def test_network_ambiguity_blocks_retry_after_restart(self):
        opener = Opener(error.URLError("connection lost"))
        with self.assertRaises(sender.SenderError):
            self.run_send(opener, entries=(self.entries[0],))
        journal = sender.Journal(self.root)
        with self.assertRaisesRegex(sender.SenderError, "unknown outcome"):
            journal.pending("123456", self.entries)
        self.assertEqual(len(opener.calls), 1)

    def test_server_errors_or_missing_receipt_are_not_retried(self):
        for reply in (Response(500), Response(200, b'{}'), Response(204, b"")):
            with self.subTest(status=reply.status):
                directory = self.root / str(id(reply))
                journal = sender.Journal(directory)
                opener = Opener(reply)
                with self.assertRaises(sender.SenderError):
                    entries = (self.entries[0],)
                    approval = sender.ReviewApproval(sender.entries_digest(entries), False)
                    sender.send_entries(URL, entries, journal, Stop(), lambda text: None, opener, approval)
                with self.assertRaises(sender.SenderError):
                    journal.pending("123456", self.entries)
                self.assertEqual(len(opener.calls), 1)

    def test_definite_rejection_is_recorded_without_unknown_outcome(self):
        opener = Opener(Response(403, b'{}'))
        with self.assertRaisesRegex(sender.SenderError, "HTTP 403"):
            self.run_send(opener, entries=(self.entries[0],))
        self.assertEqual(self.journal.pending("123456", self.entries), self.entries)

    def test_truncated_journal_blocks_sending(self):
        self.journal.flight_path.write_text('{"key":', encoding="utf-8")
        with self.assertRaisesRegex(sender.SenderError, "unreadable"):
            self.journal.pending("123456", self.entries)

    def test_redirect_handler_never_forwards_secret(self):
        redirect = sender.NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, "moved", {}, "https://evil.test/"))

    def test_single_instance_lock(self):
        first = sender.InstanceLock(self.root / "sender.lock").acquire()
        self.addCleanup(first.close)
        with self.assertRaises(sender.SenderError):
            sender.InstanceLock(self.root / "sender.lock").acquire()
        first.close()
        second = sender.InstanceLock(self.root / "sender.lock").acquire()
        second.close()

    def reviewed_file(self, rows, **overrides):
        path = self.root / "history.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        entries, digest = sender.load_history(path, with_digest=True)
        manifest = {"schema": 1, "readyToSend": True, "captureCompletenessEstablished": True,
                    "reviewed": True, "messageCount": len(entries), "sha256": digest, **overrides}
        path.with_suffix(".review.json").write_text(json.dumps(manifest), encoding="utf-8")
        return path, entries, digest

    def test_missing_or_incomplete_manifest_blocks_before_network(self):
        rows = [{"id": 1, "sender": "A", "text": "first"}]
        path, entries, digest = self.reviewed_file(rows, captureCompletenessEstablished=False)
        with self.assertRaisesRegex(sender.SenderError, "complete capture"):
            sender.authorize_history(path, entries, digest)
        path.with_suffix(".review.json").unlink()
        with self.assertRaises(sender.SenderError):
            sender.authorize_history(path, entries, digest)
        opener = Opener()
        with self.assertRaises(sender.SenderError):
            sender.send_entries(URL, entries, self.journal, Stop(), lambda text: None, opener)
        self.assertEqual(opener.calls, [])

    def test_review_manifest_is_bound_to_exact_loaded_bytes(self):
        path, entries, digest = self.reviewed_file([{"id": 1, "sender": "A", "text": "first"}])
        approval = sender.authorize_history(path, entries, digest)
        sender.validate_review(entries, approval)
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with self.assertRaises(sender.SenderError):
            sender.authorize_history(path, entries, digest)
        with self.assertRaises(sender.SenderError):
            sender.validate_review((sender.Entry(1, "A", "edited"),), approval)

    def test_ocr_drafts_and_unreviewed_flags_cannot_send(self):
        for details in ({"reviewStatus": "unreviewed_ocr_draft"}, {"unresolved": ["text_ocr_unreviewed"]}):
            with self.subTest(details=details):
                path, entries, digest = self.reviewed_file([{"id": 1, "sender": "A", "text": "first", **details}])
                with self.assertRaises(sender.SenderError):
                    sender.authorize_history(path, entries, digest)

    def test_actual_blank_native_author_requires_review_and_neutral_metadata(self):
        row = {"id": 1, "sender": "Unknown guild member", "text": "visible text", "reviewStatus": "reviewed",
               "unresolved": ["native_sender_empty_or_unreadable", "race_not_observed"]}
        path, entries, digest = self.reviewed_file([row])
        with self.assertRaises(sender.SenderError):
            sender.authorize_history(path, entries, digest)
        path, entries, digest = self.reviewed_file([row], allowUnknownSenders=True)
        sender.authorize_history(path, entries, digest)
        path, entries, digest = self.reviewed_file([{**row, "class": "MAGE"}], allowUnknownSenders=True)
        with self.assertRaisesRegex(sender.SenderError, "keep class"):
            sender.authorize_history(path, entries, digest)

    def test_malformed_author_or_utf8_is_a_clear_load_error(self):
        for author in ("", "A\nB", None):
            path = self.root / "bad.jsonl"
            path.write_text(json.dumps({"id": 1, "sender": author, "text": "content"}), encoding="utf-8")
            with self.assertRaises(sender.SenderError):
                sender.load_history(path)
        path.write_bytes(b'\xff')
        with self.assertRaises(sender.SenderError):
            sender.load_history(path)

    def test_invalid_audit_states_and_missing_sent_receipts_block_retry(self):
        self.journal.event("123456", self.entries[0], "sent", message_id="100")
        with self.assertRaisesRegex(sender.SenderError, "unknown outcome"):
            self.journal.pending("123456", self.entries)
        self.journal.flight_path.unlink()
        self.journal.event("123456", self.entries[0], "invalid")
        with self.assertRaisesRegex(sender.SenderError, "invalid records"):
            self.journal.pending("123456", self.entries)

    def test_random_fallback_is_stable_decoration_and_keeps_metadata_unknown(self):
        entry = sender.Entry(21, "Unknown race player", "one", "MAGE")
        another = sender.Entry(22, entry.sender, "two", "MAGE")
        icon = sender.make_payload(entry)["avatar_url"]
        self.assertEqual(icon, sender.make_payload(another)["avatar_url"])
        self.assertIn(icon, {
            f"https://wow.zamimg.com/images/wow/icons/large/race_{race}_{gender}.jpg"
            for race in sender.HORDE_RACES for gender in ("male", "female")
        })
        self.assertIsNone(entry.race)
        self.assertIsNone(entry.sex)
        known = sender.Entry(23, entry.sender, "three", "MAGE", "Tauren", 2)
        self.assertTrue(sender.make_payload(known)["avatar_url"].endswith("race_tauren_male.jpg"))

    def test_adjacent_sender_messages_become_one_paragraph_and_one_post(self):
        entries = (sender.Entry(1, "Warrior Example", "Hello team", "WARRIOR", "Tauren", 2),
                   sender.Entry(2, "Warrior Example", "Ready for\nthe dungeon", "WARRIOR", "Tauren", 2),
                   sender.Entry(3, "Warlock Example", "Ready too", "WARLOCK"))
        opener = Opener(Response(body=b'{"id":"100"}'), Response(body=b'{"id":"101"}'))
        self.run_send(opener, entries)
        payloads = [json.loads(call.data) for call in opener.calls]
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["username"], "Warrior Example")
        self.assertEqual(len(payloads[0]["embeds"]), 1)
        self.assertEqual(payloads[0]["embeds"][0]["description"], "Hello team Ready for the dungeon")
        self.assertEqual(payloads[0]["embeds"][0]["color"], 0xC79C6E)
        receipts = self.journal._read(self.journal.sent_path)
        self.assertEqual([(r["row_id"], r["message_id"]) for r in receipts], [(1, "100"), (2, "100"), (3, "101")])
        self.assertEqual(self.journal.pending("123456", entries), ())

    def test_confirmed_middle_rows_break_groups_and_old_hashes_still_skip(self):
        entries = tuple(sender.Entry(i, "A", f"body {i}", "MAGE") for i in (1, 2, 3))
        self.journal.success("123456", entries[1], "90")
        opener = Opener(Response(), Response())
        self.run_send(opener, entries)
        self.assertEqual([json.loads(c.data)["embeds"][0]["description"] for c in opener.calls], ["body 1", "body 3"])
        self.assertEqual(self.journal._read(self.journal.sent_path)[0]["hash"], entries[1].digest())

    def test_unknown_authors_and_different_classes_or_icons_never_merge(self):
        entries = (sender.Entry(1, "Unknown guild member", "one"),
                   sender.Entry(2, "Unknown guild member", "two"),
                   sender.Entry(3, "Unknown sender", "three"),
                   sender.Entry(4, "A", "four", "MAGE", "Orc", 2),
                   sender.Entry(5, "A", "five", "WARLOCK", "Orc", 2),
                   sender.Entry(6, "A", "six", "WARLOCK", "Tauren", 2))
        self.assertEqual([len(g) for g in sender.group_pending_entries(entries, entries)], [1]*6)

    def test_group_limits_are_checked_after_escaping_and_utf16_counting(self):
        entries = tuple(sender.Entry(i, "A", "*"*1024) for i in (1, 2, 3))
        groups = sender.group_pending_entries(entries, entries)
        self.assertEqual([len(g) for g in groups], [1, 1, 1])
        exact = (sender.Entry(1, "A", "a"*2047), sender.Entry(2, "A", "b"*2048), sender.Entry(3, "A", "c"))
        groups = sender.group_pending_entries(exact, exact)
        self.assertEqual([len(g) for g in groups], [2, 1])
        self.assertEqual(sender.description_length(sender.make_group_payload(groups[0])["embeds"][0]["description"]), 4096)
        with self.assertRaises(sender.SenderError):
            sender.make_payload(sender.Entry(1, "A", "😀"*2049))

    def test_native_formatting_cannot_span_combined_source_messages(self):
        entries = (sender.Entry(1, "A", "before |Hitem:1"), sender.Entry(2, "A", "middle |hlabel|h after"))
        payload = sender.make_group_payload(entries)
        self.assertEqual(payload["embeds"][0]["description"], " ".join(sender.entry_description(e) for e in entries))
        self.assertIn("before", payload["embeds"][0]["description"])
        self.assertIn("middle", payload["embeds"][0]["description"])

    def test_group_ambiguity_blocks_every_member_after_restart(self):
        entries = (sender.Entry(1, "A", "one"), sender.Entry(2, "A", "two"))
        opener = Opener(error.URLError("connection lost"))
        with self.assertRaises(sender.SenderError):
            self.run_send(opener, entries)
        events = self.journal._read(self.journal.flight_path)
        self.assertEqual([(e["row_id"], e["state"]) for e in events], [(1, "started"), (2, "started"), (1, "ambiguous"), (2, "ambiguous")])
        with self.assertRaisesRegex(sender.SenderError, "unknown outcome"):
            sender.Journal(self.root).pending("123456", entries)
        self.assertEqual(len(opener.calls), 1)

    def test_group_stop_after_429_marks_all_members_retryable(self):
        entries = (sender.Entry(1, "A", "one"), sender.Entry(2, "A", "two"))
        limited = error.HTTPError(URL, 429, "rate limited", {}, io.BytesIO(b'{"retry_after":1}'))
        self.run_send(Opener(limited), entries, Stop(True))
        self.assertEqual(self.journal.pending("123456", entries), entries)

    def test_partial_group_receipt_failure_does_not_resend_accepted_group(self):
        entries = (sender.Entry(1, "A", "one"), sender.Entry(2, "A", "two"))
        real_success = self.journal.success

        def fail_second(webhook, entry, message_id):
            if entry.id == 2:
                raise sender.SenderError("Disk unavailable")
            real_success(webhook, entry, message_id)

        self.journal.success = fail_second
        opener = Opener(Response(body=b'{"id":"100"}'))
        with self.assertRaises(sender.SenderError):
            self.run_send(opener, entries)
        self.assertEqual(len(opener.calls), 1)
        with self.assertRaisesRegex(sender.SenderError, "unknown outcome"):
            sender.Journal(self.root).pending("123456", entries)

    def test_all_avatars_are_horde_even_for_unknown_or_alliance_metadata(self):
        allowed = {f"race_{race}_{gender}.jpg" for race in sender.HORDE_RACES for gender in ("male", "female")}
        for index, race in enumerate((None, "Human", "Dwarf", "Night Elf", "Gnome", "unknown", "Undead", "Orc", "Tauren", "Troll")):
            with self.subTest(race=race):
                entry = sender.Entry(index, f"Player {index}", "hello", race=race, sex=3)
                self.assertIn(sender.avatar_url(entry).rsplit("/", 1)[1], allowed)

    def test_confirmed_player_avatar_preferences_preserve_source_receipt_hashes(self):
        desired = {"Player One":("tauren", 3), "Player Two":("tauren", 3),
                   "Player Three":("scourge", 2), "Player Four":("troll", 3)}
        original = sender.AVATAR_OVERRIDES
        sender.AVATAR_OVERRIDES = desired
        self.addCleanup(setattr, sender, "AVATAR_OVERRIDES", original)
        for name, (race, sex) in desired.items():
            with self.subTest(name=name):
                entry = sender.Entry(1, name, "hello")
                digest = entry.digest()
                payload = sender.make_payload(entry)
                gender = "male" if sex == 2 else "female"
                self.assertTrue(payload["avatar_url"].endswith(f"race_{race}_{gender}.jpg"))
                self.assertEqual(entry.digest(), digest)
                self.assertIsNone(entry.race)
                self.assertIsNone(entry.sex)

    def test_avatar_preferences_validate_horde_metadata_and_reject_unknown_authors(self):
        path = self.root / "avatars.json"
        path.write_text(json.dumps({"schema":1,"players":{"A":{"race":"Undead","sex":2}}}), encoding="utf-8")
        self.assertEqual(sender.load_avatar_overrides(path), {"A":("scourge", 2)})
        for player, value in (("A", {"race":"Human","sex":2}), ("A", {"race":"Orc","sex":True}),
                              ("Unknown guild member", {"race":"Tauren","sex":3})):
            path.write_text(json.dumps({"schema":1,"players":{player:value}}), encoding="utf-8")
            with self.assertRaises(sender.SenderError):
                sender.load_avatar_overrides(path)


if __name__ == "__main__":
    unittest.main()
