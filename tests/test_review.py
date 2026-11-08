"""Offline review-CLI checks using synthetic captures only."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
try:
    import review_history as review
    import send_history as sender
finally:
    sys.path.pop(0)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = HERE.parent / "build" / "check-temp"
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "synthetic.jsonl"

    def capture(self, **changes):
        row = {"id": 1, "sender": "Example-Forever", "text": "Synthetic example.", **changes}
        raw = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        self.path.write_bytes(raw)
        return raw

    def approve(self, **options):
        return review.review_history(self.path, reviewed_complete_capture=True, **options)

    def test_no_flag_does_not_approve_or_touch_capture(self):
        original = self.capture()
        with self.assertRaisesRegex(sender.SenderError, "reviewed-complete-capture"):
            review.review_history(self.path)
        result = subprocess.run(
            [sys.executable, "-B", str(HERE.parent / "review_history.py"), str(self.path)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("reviewed-complete-capture", result.stderr)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.path.with_suffix(".review.json").exists())

    def test_draft_and_unresolved_flags_block_without_rewriting_them(self):
        for annotations in ({"reviewStatus": "unreviewed_ocr_draft"},
                            {"unresolved": ["text_ocr_unreviewed"]},
                            {"unresolved": ["display_wrap_requires_review"]}):
            with self.subTest(annotations=annotations):
                original = self.capture(**annotations)
                with self.assertRaises(sender.SenderError):
                    self.approve()
                self.assertEqual(self.path.read_bytes(), original)
                self.assertFalse(self.path.with_suffix(".review.json").exists())

    def test_cli_explicit_flags_write_only_the_review_sidecar(self):
        original = self.capture(sender="Unknown guild member")
        result = subprocess.run(
            [sys.executable, "-B", str(HERE.parent / "review_history.py"), str(self.path),
             "--reviewed-complete-capture", "--allow-unknown-senders"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Saved review sidecar", result.stdout)
        self.assertEqual(self.path.read_bytes(), original)
        entries, digest = sender.load_history(self.path, with_digest=True)
        sender.authorize_history(self.path, entries, digest)
        self.assertEqual({path.name for path in self.root.iterdir()},
                         {self.path.name, self.path.with_suffix(".review.json").name})

    def test_unknown_requires_explicit_allow_and_neutral_metadata(self):
        original = self.capture(sender="Unknown guild member")
        with self.assertRaisesRegex(sender.SenderError, "explicitly allow"):
            self.approve()
        manifest_path = self.approve(allow_unknown_senders=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIs(manifest["allowUnknownSenders"], True)
        self.assertEqual(self.path.read_bytes(), original)
        original = self.capture(sender="Unknown guild member", **{"class": "MAGE"})
        previous = manifest_path.read_bytes()
        with self.assertRaisesRegex(sender.SenderError, "keep class"):
            self.approve(allow_unknown_senders=True)
        self.assertEqual(manifest_path.read_bytes(), previous)
        self.assertEqual(self.path.read_bytes(), original)

    def test_manifest_matches_exact_unchanged_bytes_and_authorizes_sender(self):
        row = {"id": 1, "sender": "Example-Forever", "text": "Synthetic café.",
               "reviewStatus": "reviewed", "unresolved": ["race_not_observed"]}
        original = b"\xef\xbb\xbf" + (json.dumps(row, ensure_ascii=False) + "\r\n\r\n").encode("utf-8")
        self.path.write_bytes(original)
        manifest_path = self.approve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest_path, self.path.with_suffix(".review.json"))
        self.assertEqual(manifest["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(manifest["messageCount"], 1)
        for flag in ("readyToSend", "reviewed", "captureCompletenessEstablished"):
            self.assertIs(manifest[flag], True)
        self.assertEqual(self.path.read_bytes(), original)
        entries, digest = sender.load_history(self.path, with_digest=True)
        sender.authorize_history(self.path, entries, digest)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_change_before_temp_write_does_not_approve(self):
        self.capture()
        original_validation = review.validate_review

        def change_capture(entries, approval):
            original_validation(entries, approval)
            self.capture(text="Changed during validation.")

        with mock.patch.object(review, "validate_review", side_effect=change_capture):
            with self.assertRaisesRegex(sender.SenderError, "changed during review"):
                self.approve()
        self.assertFalse(self.path.with_suffix(".review.json").exists())

    def test_change_before_replace_keeps_previous_manifest_and_cleans_temp(self):
        self.capture()
        target = self.path.with_suffix(".review.json")
        target.write_bytes(b"previous manifest")
        original_digest = review._file_digest
        calls = 0

        def change_before_commit(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.capture(text="Changed before commit.")
            return original_digest(path)

        with mock.patch.object(review, "_file_digest", side_effect=change_before_commit):
            with self.assertRaisesRegex(sender.SenderError, "changed before saving"):
                self.approve()
        self.assertEqual(target.read_bytes(), b"previous manifest")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_atomic_replace_failure_preserves_existing_manifest(self):
        original = self.capture()
        target = self.path.with_suffix(".review.json")
        target.write_bytes(b"previous manifest")
        with mock.patch.object(review.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(sender.SenderError, "Could not save"):
                self.approve()
        self.assertEqual(target.read_bytes(), b"previous manifest")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
