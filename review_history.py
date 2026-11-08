"""Record an explicit review of a complete JSONL capture without changing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from send_history import (
    ReviewApproval,
    SenderError,
    entries_digest,
    load_history,
    validate_review,
)


def _file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_history(path, *, reviewed_complete_capture=False, allow_unknown_senders=False):
    """Save a hash-bound attestation; the caller must have reviewed completeness."""
    if reviewed_complete_capture is not True:
        raise SenderError(
            "Pass --reviewed-complete-capture only after reviewing the entire capture. "
            "This tool cannot establish capture completeness."
        )
    source = Path(path)
    entries, digest = load_history(source, with_digest=True)
    approval = ReviewApproval(entries_digest(entries), allow_unknown_senders is True)
    validate_review(entries, approval)
    manifest_path = source.with_suffix(".review.json")
    temporary_path = None
    try:
        if source.resolve() == manifest_path.resolve() or (
            manifest_path.exists() and os.path.samefile(source, manifest_path)
        ):
            raise SenderError("The review sidecar must be separate from the history file.")
        manifest = {
            "schema": 1,
            "readyToSend": True,
            "reviewed": True,
            "captureCompletenessEstablished": True,
            "sha256": digest,
            "messageCount": len(entries),
            "allowUnknownSenders": allow_unknown_senders is True,
        }
        payload = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        if _file_digest(source) != digest:
            raise SenderError("The capture changed during review. Review the current file again.")
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=manifest_path.parent,
            prefix="." + manifest_path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _file_digest(source) != digest:
            raise SenderError("The capture changed before saving. Review the current file again.")
        os.replace(temporary_path, manifest_path)
        temporary_path = None
    except OSError:
        raise SenderError("Could not save the review sidecar; the capture was not changed.") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return manifest_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="The completeness flag records your review; it does not verify or repair the capture. No messages are sent.",
    )
    parser.add_argument("history", type=Path, help="The reviewed UTF-8 JSONL history file")
    parser.add_argument(
        "--reviewed-complete-capture", action="store_true",
        help="Explicitly attest that you reviewed this entire capture and established completeness",
    )
    parser.add_argument(
        "--allow-unknown-senders", action="store_true",
        help="Allow reviewed blank-author labels with class, race and sex unset",
    )
    args = parser.parse_args(argv)
    try:
        output = review_history(
            args.history, reviewed_complete_capture=args.reviewed_complete_capture,
            allow_unknown_senders=args.allow_unknown_senders,
        )
    except SenderError as exc:
        parser.exit(2, f"Review not saved: {exc}\n")
    print(f"Saved review sidecar: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
