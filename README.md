# WoWF Discord Chat

A small desktop sender for copying reviewed World of Warcraft: Forever guild-chat history into a Discord webhook channel.

Consecutive messages from the same player become one paragraph with a class-colored embed bar. Player names appear without a guild suffix. Race avatars are Horde-only: Orc, Troll, Tauren, and Undead. When race details are unknown, a consistent decorative avatar is chosen for that player.

This is a manual history sender. It does not connect to the game, capture chat automatically, or provide a continuous live relay. A reviewed JSON Lines history file is required. No guild conversations, screenshots, webhook credentials, or player preferences are included in this repository.

## Requirements

- Python 3.10 or newer. The sender uses the Python standard library.
- Tcl/Tk support for the desktop interface. On Windows, include Tcl/Tk when installing Python. On Linux, your distribution may provide it as a separate `python3-tk` package.
- A Discord channel webhook that you control. Creating one requires the channel's Manage Webhooks permission.

Check the desktop dependency with `python -m tkinter`. No additional Python packages are required.

## Prepare a history file

Create a local `build` directory beside `send_history.py`. Put your reviewed history in `build/history.jsonl`, with one JSON object per line, in the original chronological order:

```json
{"id":1,"sender":"Warrior Example","text":"Hello team!","class":"WARRIOR","race":"Tauren","sex":2,"reviewStatus":"reviewed"}
{"id":2,"sender":"Warrior Example","text":"Ready for the dungeon?","class":"WARRIOR","race":"Tauren","sex":2,"reviewStatus":"reviewed"}
```

IDs must be unique, non-negative integers. Keep IDs and source text stable across retries. `class`, `race`, and `sex` may be `null`; use `2` for male and `3` for female when known. Use `Unknown guild member` for a genuinely missing author, with class/race/sex unset. Unknown authors stay separate rather than being combined into a guessed identity.

The files in `examples/` contain fictional data for learning the format. They are not a guild capture and are not approved for sending on startup.

After manually checking the entire intended capture for accuracy, completeness, and order, record that review locally:

```powershell
python review_history.py build/history.jsonl --reviewed-complete-capture
```

Add `--allow-unknown-senders` only if you reviewed the entries with missing authors. This writes `history.review.json` beside the input, bound to the exact file SHA-256 and message count. The tool does not establish capture completeness, correct OCR, or remove unresolved draft flags. Changing the input requires a new review.

## Send to Discord

```powershell
python send_history.py
```

On Windows, `./Start.ps1` also locates a Python installation with Tkinter.

1. Load the reviewed file and inspect the combined-message preview.
2. Open the destination Discord channel's settings, then **Integrations → Webhooks → New Webhook** and copy its URL.
3. Paste the URL into the masked field.
4. Click **Send pending to Discord** and keep the app open until it finishes.

Opening the app or loading a preview sends nothing. The webhook URL stays in memory and is not written to history, receipts, or command arguments. Mention notifications are disabled and chat text is escaped for Discord.

An optional terminal sender prompts for the URL without showing it:

```powershell
python send_captured.py
```

The terminal sender uses the reviewed `build/history.jsonl`. `--exclude-channel CHANNEL_ID` can guard against replaying a new webhook into a prior channel.

## Avatars and grouping

Known Horde race/sex details select the corresponding icon. Missing or unsupported race details use a stable random Horde avatar; this is decoration and does not change the captured metadata. To supply known details without changing source-entry hashes, create `build/avatar-overrides.json` using the format in `examples/avatar-overrides.example.json`, then restart the sender.

Only consecutive pending messages with the same named sender, class, and avatar combine. Different players, already-confirmed source entries, and Discord's size limit break the group. Each source message is sanitized independently before joining with spaces. Existing Discord posts are not automatically replaced when the format changes.

Race icons are referenced from Wowhead's public image CDN. World of Warcraft artwork belongs to its respective owners; no icon files are bundled. The app is not affiliated with Blizzard Entertainment, Discord, or Wowhead.

## Delivery records

Keep `build/sent.jsonl` and `build/inflight.jsonl`. They store webhook IDs, source-entry IDs/hashes, and Discord message receipts, without the webhook token. Multiple source entries can share one combined post while retaining individual receipts. Confirmed entries for the same webhook are skipped on restart. A different webhook is treated as a separate destination.

Discord rate limits pause the sender automatically. A lost connection, server error, or missing receipt stops delivery with an uncertain outcome. Automatic retry is blocked until that outcome is resolved against the Discord channel. Do not delete the journals to force a retry: an accepted post could be duplicated.

The `build/` directory is private local data and must stay out of Git. It can contain complete guild conversations and preferences even though delivery receipts do not contain the webhook token.

## Tests

```powershell
python -B -m unittest discover -s tests -p "test_*.py"
```

Public tests use fictional fixtures and fake HTTP responses. They never contact Discord.

## Code rights

Source is published for viewing with all rights reserved; see [LICENSE](LICENSE). Third-party names and artwork retain their owners' rights.
