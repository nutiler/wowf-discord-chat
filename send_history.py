"""Explicit-click sender for locally reviewed guild-chat history. No live relay."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib import error, parse, request

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
CLASS_COLORS = {
    "WARRIOR": 0xC79C6E, "PALADIN": 0xF58CBA, "HUNTER": 0xABD473,
    "ROGUE": 0xFFF569, "PRIEST": 0xFFFFFF, "DEATHKNIGHT": 0xC41F3B,
    "SHAMAN": 0x0070DE, "MAGE": 0x69CCF0, "WARLOCK": 0x9482C9,
    "MONK": 0x00FF96, "DRUID": 0xFF7D0A, "DEMONHUNTER": 0xA330C9,
    "EVOKER": 0x33937F,
}
RACES = {name: name for name in (
    "human", "orc", "dwarf", "nightelf", "scourge", "tauren", "gnome", "troll"
)}
RACES["undead"] = "scourge"
UNKNOWN_SENDERS = {"unknown", "unknown guild member", "unknown sender"}
HORDE_RACES = ("orc", "scourge", "tauren", "troll")
RACE_ICONS = tuple((race, sex) for race in HORDE_RACES for sex in (2, 3))
MAX_DESCRIPTION = 4096


class SenderError(Exception):
    """A deliberately sanitized user-facing error; never include credentials."""


@dataclass(frozen=True)
class Entry:
    id: int
    sender: str
    text: str
    class_name: str | None = None
    race: str | None = None
    sex: int | None = None
    review_status: str | None = None
    unresolved: tuple[str, ...] = ()

    def digest(self):
        content = {"id": self.id, "sender": self.sender, "text": self.text,
                   "class": self.class_name, "race": self.race, "sex": self.sex}
        return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()


def load_history(path, *, with_digest=False):
    result, seen = [], set()
    try:
        raw = Path(path).read_bytes()
        for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                identity = row["id"]
                sender, text = row["sender"], row["text"]
                if type(identity) is not int or identity < 0 or identity in seen:
                    raise ValueError()
                if not isinstance(sender, str) or not sender.strip() or len(sender) > 200:
                    raise ValueError()
                if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                    raise ValueError()
                for key in ("class", "race"):
                    if row.get(key) is not None and not isinstance(row[key], str):
                        raise ValueError()
                sex = row.get("sex")
                if sex is not None and (type(sex) is not int or sex not in (1, 2, 3)):
                    raise ValueError()
                review_status = row.get("reviewStatus")
                unresolved = row.get("unresolved", [])
                if review_status is not None and not isinstance(review_status, str):
                    raise ValueError()
                if not isinstance(unresolved, list) or any(not isinstance(flag, str) for flag in unresolved):
                    raise ValueError()
                if re.search(r"[\x00-\x1f\x7f]", sender):
                    raise ValueError()
                sender.encode("utf-8")
                text.encode("utf-8")
                entry = Entry(identity, sender, text, row.get("class"), row.get("race"), sex,
                              review_status, tuple(unresolved))
                make_payload(entry)  # Validate exact final Discord length before any send.
                result.append(entry)
                seen.add(identity)
            except (KeyError, TypeError, ValueError, SenderError):
                raise SenderError(f"History row {number} is invalid or too long; review it before sending.") from None
    except UnicodeError:
        raise SenderError("The history file must contain valid UTF-8 text.") from None
    except OSError:
        raise SenderError("Could not open the selected history file.") from None
    if not result:
        raise SenderError("The history file has no messages yet.")
    entries = tuple(result)
    return (entries, hashlib.sha256(raw).hexdigest()) if with_digest else entries


@dataclass(frozen=True)
class ReviewApproval:
    entries_digest: str
    allow_unknown_senders: bool


def entries_digest(entries):
    # Includes review annotations as well as content; the approved in-memory
    # preview must not be replaced before the worker starts.
    rows = [{"content": entry.digest(), "reviewStatus": entry.review_status,
             "unresolved": entry.unresolved} for entry in entries]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def validate_review(entries, approval):
    if not isinstance(approval, ReviewApproval) or approval.entries_digest != entries_digest(entries):
        raise SenderError("This history has not passed the capture review. Load the final reviewed file before sending.")
    for entry in entries:
        if entry.review_status is not None and entry.review_status != "reviewed":
            raise SenderError(f"Entry {entry.id} is still marked as an OCR draft; review it before sending.")
        if any("unreviewed" in flag or flag.endswith("requires_review") for flag in entry.unresolved):
            raise SenderError(f"Entry {entry.id} still has unresolved OCR review flags.")
        if entry.sender.strip().casefold() in UNKNOWN_SENDERS:
            if not approval.allow_unknown_senders:
                raise SenderError("The capture includes a blank or unreadable author. Review its source and explicitly allow unknown sender labels.")
            if entry.class_name is not None or entry.race is not None or entry.sex is not None:
                raise SenderError(f"Unknown author entry {entry.id} must keep class, race and sex unset.")


def authorize_history(path, entries, loaded_sha256):
    path = Path(path)
    try:
        current_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = json.loads(path.with_suffix(".review.json").read_text(encoding="utf-8"))
        valid = (type(manifest.get("schema")) is int and manifest["schema"] == 1
                 and manifest.get("readyToSend") is True
                 and manifest.get("captureCompletenessEstablished") is True
                 and manifest.get("reviewed") is True
                 and type(manifest.get("messageCount")) is int
                 and manifest["messageCount"] == len(entries)
                 and current_digest == loaded_sha256 == manifest.get("sha256"))
    except (OSError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise SenderError("Sending is blocked until the complete capture is reviewed and its matching .review.json manifest is ready. Reload the final file if it changed.")
    approval = ReviewApproval(entries_digest(entries), manifest.get("allowUnknownSenders") is True)
    validate_review(entries, approval)
    return approval


def validate_webhook(url):
    try:
        parts = parse.urlsplit(url.strip())
        if parts.scheme != "https" or parts.hostname not in {"discord.com", "canary.discord.com", "ptb.discord.com"}:
            raise ValueError()
        if parts.netloc != parts.hostname or parts.query or parts.fragment:
            raise ValueError()
        match = re.fullmatch(r"/api(?:/v10)?/webhooks/([0-9]+)/([A-Za-z0-9._-]{20,})/?", parts.path)
        if not match:
            raise ValueError()
        identity, token = match.groups()
        return identity, f"https://{parts.hostname}/api/v10/webhooks/{identity}/{token}?wait=true"
    except (ValueError, TypeError, AttributeError):
        raise SenderError("Paste a complete HTTPS Discord webhook URL for the destination channel.") from None


def plain_wow(text):
    # Escaped literal pipes must survive the removal of native formatting tags.
    sentinel = "\ufdd0"
    while sentinel in text:
        sentinel += "\ufdd0"
    text = text.replace("||", sentinel)
    text = re.sub(r"\|H[^|]*\|h(.*?)\|h", r"\1", text, flags=re.S)
    text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text)
    text = re.sub(r"\|[TA].*?\|[ta]", "", text)
    return text.replace(sentinel, "|")


def discord_text(text):
    text = plain_wow(text)
    text = re.sub(r"@(everyone|here)", "@\u200b\\1", text, flags=re.I)
    # Also neutralize explicit user/role mention markup and every markdown control.
    return re.sub(r"([\\`*_{}\[\]()<>|~])", r"\\\1", text)


def load_avatar_overrides(path=BUILD / "avatar-overrides.json"):
    """Read local presentation preferences without changing captured entry hashes."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data.get("schema")) is not int or data["schema"] != 1 or not isinstance(data.get("players"), dict):
            raise ValueError()
        result = {}
        for name, details in data["players"].items():
            if (not isinstance(name, str) or not name.strip() or len(name) > 200
                    or re.search(r"[\x00-\x1f\x7f]", name) or name.strip().casefold() in UNKNOWN_SENDERS
                    or not isinstance(details, dict) or not isinstance(details.get("race"), str)):
                raise ValueError()
            race = RACES.get(re.sub(r"[^a-z]", "", details["race"].lower()))
            sex = details.get("sex")
            if race not in HORDE_RACES or type(sex) is not int or sex not in (2, 3):
                raise ValueError()
            result[name] = (race, sex)
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        raise SenderError("Local race-icon preferences need review before sending.") from None


# Fixed for this sender process, including its preview and any active request.
# Restart the sender after changing the local preferences file.
AVATAR_OVERRIDES = load_avatar_overrides()


def avatar_url(entry):
    """Unknown metadata gets a stable decorative choice, never a guessed fact."""
    known_race, known_sex = AVATAR_OVERRIDES.get(entry.sender, (entry.race, entry.sex))
    race = RACES.get(re.sub(r"[^a-z]", "", (known_race or "").lower()))
    choice = int.from_bytes(hashlib.sha256(entry.sender.strip().casefold().encode("utf-8")).digest(), "big")
    if race in HORDE_RACES:
        sex = known_sex if known_sex in (2, 3) else (2, 3)[choice % 2]
    else:
        race, sex = RACE_ICONS[choice % len(RACE_ICONS)]
    gender = "male" if sex == 2 else "female"
    return f"https://wow.zamimg.com/images/wow/icons/large/race_{race}_{gender}.jpg"


def description_length(text):
    # Count UTF-16 units conservatively, including astral emoji as two units.
    return len(text.encode("utf-16-le")) // 2


def entry_description(entry):
    # Sanitize each source message separately: malformed WoW tags must not
    # consume the next source message when messages are combined.
    description = discord_text(" ".join(entry.text.splitlines()).strip())
    if not description.strip() or description_length(description) > MAX_DESCRIPTION:
        raise SenderError(f"History entry {entry.id} exceeds Discord's message size; split it before sending.")
    return description


def group_identity(entry):
    identity = (entry.sender, entry.class_name, avatar_url(entry))
    if entry.sender.strip().casefold() in UNKNOWN_SENDERS:
        return (*identity, entry.id)  # Blank native authors may be different people.
    return identity


def group_pending_entries(entries, pending):
    """Combine only adjacent pending messages with the same visible identity."""
    wanted = {(entry.id, entry.digest()) for entry in pending}
    groups, current, identity, size = [], [], None, 0
    for entry in entries:
        if (entry.id, entry.digest()) not in wanted:
            if current:
                groups.append(tuple(current))
            current, identity, size = [], None, 0
            continue
        next_identity = group_identity(entry)
        next_size = description_length(entry_description(entry))
        if current and (next_identity != identity or size + 1 + next_size > MAX_DESCRIPTION):
            groups.append(tuple(current))
            current, size = [], 0
        size += next_size + (1 if current else 0)
        current.append(entry)
        identity = next_identity
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def make_group_payload(entries):
    if not entries or any(group_identity(entry) != group_identity(entries[0]) for entry in entries):
        raise SenderError("Combined messages must have the same sender, class and avatar.")
    description = " ".join(entry_description(entry) for entry in entries)
    if description_length(description) > MAX_DESCRIPTION:
        raise SenderError("Combined messages exceed Discord's message size.")
    entry = entries[0]
    sender = plain_wow(entry.sender).replace("\r", " ").replace("\n", " ")
    sender = re.sub(r"[\x00-\x1f\x7f]", "", sender)
    sender = sender.replace("@", "@\u200b")
    username = sender[:80]
    payload = {"username": username, "allowed_mentions": {"parse": [], "users": [], "roles": [], "replied_user": False},
               "embeds": [{"description": description,
                           "color": CLASS_COLORS.get((entry.class_name or "").upper(), 0x78D66B)}]}
    payload["avatar_url"] = avatar_url(entry)
    return payload


def make_payload(entry):
    return make_group_payload((entry,))


def display_group_text(entries):
    return " ".join(plain_wow(" ".join(entry.text.splitlines()).strip()) for entry in entries)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, directory=BUILD):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sent_path = self.directory / "sent.jsonl"
        self.flight_path = self.directory / "inflight.jsonl"

    @staticmethod
    def _read(path):
        if not path.exists():
            return []
        try:
            # A truncated audit line cannot safely be assumed unsent.
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            raise SenderError("The send journal is unreadable. Review it before sending anything else.") from None

    @staticmethod
    def _key(webhook_id, entry):
        return f"{webhook_id}:{entry.id}:{entry.digest()}"

    @staticmethod
    def _valid_record(row, receipt=False):
        if (not isinstance(row, dict) or not isinstance(row.get("webhook_id"), str)
                or not row["webhook_id"].isdigit() or type(row.get("row_id")) is not int
                or row["row_id"] < 0 or not isinstance(row.get("hash"), str)
                or re.fullmatch(r"[0-9a-f]{64}", row["hash"]) is None
                or row.get("key") != f'{row["webhook_id"]}:{row["row_id"]}:{row["hash"]}'):
            raise ValueError()
        if receipt:
            if not isinstance(row.get("message_id"), str) or not row["message_id"].isdigit():
                raise ValueError()
        elif row.get("state") not in {"started", "ambiguous", "sent", "rejected"}:
            raise ValueError()
        return row

    @staticmethod
    def _append(path, data):
        import os
        try:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise SenderError("Could not save the send journal. Stop and review Discord before retrying.") from None

    def pending(self, webhook_id, entries):
        unresolved = {}
        try:
            sent = {self._valid_record(row, receipt=True)["key"] for row in self._read(self.sent_path)}
            for raw in self._read(self.flight_path):
                row = self._valid_record(raw)
                if row["webhook_id"] != webhook_id:
                    continue
                if row["state"] in ("started", "ambiguous") or (row["state"] == "sent" and row["key"] not in sent):
                    unresolved[row["key"]] = row
                elif row["state"] in ("sent", "rejected"):
                    unresolved.pop(row["key"], None)
        except (KeyError, TypeError, ValueError):
            raise SenderError("The send journal has invalid records. Review it before sending.") from None
        if any(key not in sent for key in unresolved):
            raise SenderError("A prior send has an unknown outcome. Check the Discord channel and in-flight journal before retrying; automatic retry is blocked.")
        return tuple(entry for entry in entries if self._key(webhook_id, entry) not in sent)

    def event(self, webhook_id, entry, state, **extra):
        self._append(self.flight_path, {"key": self._key(webhook_id, entry), "webhook_id": webhook_id,
                     "row_id": entry.id, "hash": entry.digest(), "state": state, "at": utc_now(), **extra})

    def success(self, webhook_id, entry, message_id):
        self._append(self.sent_path, {"key": self._key(webhook_id, entry), "webhook_id": webhook_id,
                     "row_id": entry.id, "hash": entry.digest(), "message_id": message_id, "at": utc_now()})
        self.event(webhook_id, entry, "sent", message_id=message_id)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def duration(value, default=1):
    try:
        seconds = float(value)
        return max(0.1, seconds) if math.isfinite(seconds) and seconds >= 0 else default
    except (ValueError, TypeError):
        return default


def send_entries(url, entries, journal, stop, notify, opener=None, approval=None):
    validate_review(entries, approval)
    webhook_id, endpoint = validate_webhook(url)
    pending = journal.pending(webhook_id, entries)
    groups = group_pending_entries(entries, pending)
    opener = opener or request.build_opener(NoRedirect())
    confirmed = 0
    for group in groups:
        if stop.is_set():
            return "Stopped. Confirmed sends are recorded; remaining messages were not sent."
        encoded = json.dumps(make_group_payload(group), ensure_ascii=False).encode("utf-8")

        def record(state, **extra):
            for entry in group:
                journal.event(webhook_id, entry, state, **extra)

        record("started")
        while True:
            req = request.Request(endpoint, data=encoded, headers={"Content-Type": "application/json",
                                  "User-Agent": "DandyGuildHistory/0.1"}, method="POST")
            try:
                with opener.open(req, timeout=20) as response:
                    code = response.status
                    body = response.read(65536)
                    headers = response.headers
            except error.HTTPError as exc:
                code, headers = exc.code, exc.headers
                try:
                    body = exc.read(65536)
                except OSError:
                    body = b""
                finally:
                    exc.close()
            except (OSError, error.URLError, TimeoutError):
                record("ambiguous")
                raise SenderError("The connection ended with an unknown send outcome. Check Discord before retrying; this entry is blocked to prevent duplicates.") from None
            if code == 429:
                try:
                    retry = duration(json.loads(body).get("retry_after"), duration(headers.get("Retry-After")))
                except (ValueError, AttributeError):
                    retry = duration(headers.get("Retry-After"))
                notify(f"Discord rate limit: waiting {retry:.1f}s. Confirmed {confirmed}/{len(pending)}.")
                if stop.wait(retry):
                    record("rejected", status=429)
                    return "Stopped during a rate-limit wait; the pending entry was not accepted."
                continue
            if code >= 500 or (200 <= code < 300 and code != 200):
                record("ambiguous", status=code)
                raise SenderError("Discord returned an uncertain result. Check the channel before retrying; automatic retry is blocked.")
            if code != 200:
                record("rejected", status=code)
                raise SenderError(f"Discord rejected the send (HTTP {code}). No retry was attempted.")
            try:
                message_id = json.loads(body)["id"]
                if not isinstance(message_id, str) or not message_id.isdigit():
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                record("ambiguous", status=200)
                raise SenderError("Discord did not return a message receipt. Check the channel before retrying; automatic retry is blocked.") from None
            for entry in group:
                journal.success(webhook_id, entry, message_id)
            confirmed += len(group)
            notify(f"Confirmed {confirmed}/{len(pending)} source messages.")
            if headers.get("X-RateLimit-Remaining") == "0":
                if stop.wait(duration(headers.get("X-RateLimit-Reset-After"))):
                    return "Stopped. Confirmed sends are recorded; remaining messages were not sent."
            break
    return f"Finished: {len(pending)} newly confirmed source messages in {len(groups)} Discord posts. Previously confirmed entries were skipped."


class InstanceLock:
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def acquire(self):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise SenderError("Another guild-history sender is already open.") from None
        return self

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title("Dandy Guild History → Discord")
    root.geometry("920x620")
    root.minsize(740, 480)
    try:
        lock = InstanceLock(BUILD / "sender.lock").acquire()
    except SenderError as exc:
        root.withdraw()
        messagebox.showerror("Sender already open", str(exc))
        root.destroy()
        return
    entries, preview_groups, loaded_path, loaded_sha256 = (), (), None, None
    busy, events, stop = False, queue.Queue(), threading.Event()
    path = tk.StringVar(value=str(BUILD / "history.jsonl"))
    webhook = tk.StringVar()
    status = tk.StringVar(value="Load and review the captured history. Sending starts only when you click Send pending.")
    pane = ttk.Frame(root, padding=16)
    pane.pack(fill="both", expand=True)
    ttk.Label(pane, text="Send captured guild history", font=("Segoe UI", 16, "bold")).pack(anchor="w")
    ttk.Label(pane, text="Class colors · Race avatars · Consecutive messages from one player become one paragraph").pack(anchor="w", pady=(3, 14))
    file_row = ttk.Frame(pane)
    file_row.pack(fill="x")
    path_input = ttk.Entry(file_row, textvariable=path)
    path_input.pack(side="left", fill="x", expand=True)
    preview_frame = ttk.Frame(pane)
    preview = ttk.Treeview(preview_frame, columns=("id", "sender", "text"), show="headings", height=12)
    vertical = ttk.Scrollbar(preview_frame, orient="vertical", command=preview.yview)
    horizontal = ttk.Scrollbar(preview_frame, orient="horizontal", command=preview.xview)
    preview.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    preview.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    preview_frame.columnconfigure(0, weight=1)
    preview_frame.rowconfigure(0, weight=1)
    for column, title, width in (("id", "#", 70), ("sender", "Sender", 160), ("text", "Message", 600)):
        preview.heading(column, text=title)
        preview.column(column, width=width, minwidth=40, stretch=column == "text")

    def load():
        nonlocal entries, preview_groups, loaded_path, loaded_sha256
        if busy:
            return
        try:
            loaded, digest = load_history(path.get(), with_digest=True)
        except SenderError as exc:
            messagebox.showerror("History needs review", str(exc))
            return
        entries = loaded
        loaded_path, loaded_sha256 = Path(path.get()).resolve(), digest
        preview.delete(*preview.get_children())
        preview_groups = group_pending_entries(entries, entries)
        for group in preview_groups:
            span = str(group[0].id) if len(group) == 1 else f"{group[0].id}-{group[-1].id}"
            preview.insert("", "end", values=(span, group[0].sender, display_group_text(group)))
        status.set(f"Loaded {len(entries)} source messages in file order ({len(preview_groups)} combined posts before sent entries are skipped).")

    def choose():
        chosen = filedialog.askopenfilename(initialdir=BUILD, filetypes=[("History JSON Lines", "*.jsonl"), ("All files", "*.*")])
        if chosen:
            path.set(chosen)
            load()

    choose_button = ttk.Button(file_row, text="Choose…", command=choose)
    choose_button.pack(side="left", padx=(8, 0))
    load_button = ttk.Button(file_row, text="Load preview", command=load)
    load_button.pack(side="left", padx=(8, 0))
    ttk.Label(pane, text="Channel webhook URL (kept in memory only)").pack(anchor="w", pady=(12, 4))
    url_input = ttk.Entry(pane, textvariable=webhook, show="●")
    url_input.pack(fill="x")
    ttk.Label(pane, text="Discord channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL").pack(anchor="w", pady=(4, 12))
    preview_frame.pack(fill="both", expand=True)
    detail = tk.Text(pane, height=4, wrap="word", state="disabled", font=("Segoe UI", 10))
    detail.pack(fill="x", pady=(8, 0))

    def show_selected(_event=None):
        selected = preview.selection()
        if not selected:
            return
        group = preview_groups[preview.index(selected[0])]
        span = str(group[0].id) if len(group) == 1 else f"{group[0].id}-{group[-1].id}"
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", f"#{span} · {group[0].sender}\n{display_group_text(group)}")
        detail.configure(state="disabled")

    preview.bind("<<TreeviewSelect>>", show_selected)
    controls = ttk.Frame(pane)
    controls.pack(fill="x", pady=(12, 8))
    ttk.Label(pane, textvariable=status, wraplength=850).pack(fill="x", anchor="w")

    def set_busy(value):
        nonlocal busy
        busy = value
        for widget in (choose_button, load_button, path_input, url_input, send_button):
            widget.configure(state="disabled" if value else "normal")
        stop_button.configure(state="normal" if value else "disabled")

    def send():
        if busy:
            return
        if not entries:
            messagebox.showinfo("Load history", "Load the history preview before sending.")
            return
        try:
            if Path(path.get()).resolve() != loaded_path:
                raise SenderError("The selected file changed. Load its preview before sending.")
            approval = authorize_history(loaded_path, entries, loaded_sha256)
            validate_webhook(webhook.get())
            journal = Journal()
            identity, _ = validate_webhook(webhook.get())
            pending = journal.pending(identity, entries)
        except SenderError as exc:
            messagebox.showerror("Cannot send", str(exc))
            return
        if not pending:
            status.set("Every loaded entry already has a confirmed receipt for this webhook.")
            return
        local_url, local_entries = webhook.get(), entries
        stop.clear()
        set_busy(True)
        groups = group_pending_entries(local_entries, pending)
        status.set(f"Sending {len(pending)} pending messages in {len(groups)} combined posts…")

        def work():
            try:
                result = send_entries(local_url, local_entries, journal, stop,
                                      lambda message: events.put(("status", message)), approval=approval)
                events.put(("done", result))
            except SenderError as exc:
                events.put(("error", str(exc)))
            except Exception:
                # Avoid exposing a URL through urllib exception representations.
                events.put(("error", "Sending stopped unexpectedly. Review Discord and the send journal before retrying."))

        threading.Thread(target=work, daemon=True).start()

    send_button = ttk.Button(controls, text="Send pending to Discord", command=send)
    send_button.pack(side="left")
    stop_button = ttk.Button(controls, text="Stop", command=stop.set, state="disabled")
    stop_button.pack(side="left", padx=8)

    def poll():
        try:
            while True:
                kind, message = events.get_nowait()
                status.set(message)
                if kind in ("done", "error"):
                    set_busy(False)
                if kind == "error":
                    messagebox.showerror("Sending stopped", message)
        except queue.Empty:
            pass
        root.after(150, poll)

    def close():
        if busy:
            stop.set()
            messagebox.showinfo("Stopping", "Stopping after the current request. Close the window once sending has stopped.")
            return
        webhook.set("")
        lock.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(150, poll)
    if Path(path.get()).is_file():
        load()
    root.mainloop()


if __name__ == "__main__":
    main()
