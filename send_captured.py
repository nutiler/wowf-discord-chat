"""Send a reviewed capture, receiving the webhook only through standard input."""
import json
import getpass
import argparse
import sys
import threading
from urllib import request, error

from send_history import (BUILD, InstanceLock, Journal, NoRedirect, SenderError,
                          authorize_history, load_history, send_entries, validate_webhook)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exclude-channel', action='append', default=[], help='Refuse delivery to this prior channel ID.')
    args = parser.parse_args()
    lock = InstanceLock(BUILD / 'sender.lock').acquire()
    try:
        print('Waiting for the channel webhook on standard input.', flush=True)
        if sys.stdin.isatty():
            webhook = getpass.getpass('Channel webhook (hidden): ')
        else:
            line = sys.stdin.readline()
            if not line.strip():
                raise SenderError('No webhook was supplied. No messages were sent.')
            config = json.loads(line)
            webhook = config.pop('webhook')
        entries, digest = load_history(BUILD / 'history.jsonl', with_digest=True)
        approval = authorize_history(BUILD / 'history.jsonl', entries, digest)
        identity, endpoint = validate_webhook(webhook)
        opener = request.build_opener(NoRedirect())
        try:
            with opener.open(request.Request(endpoint.split('?')[0], method='GET',
                    headers={'User-Agent':'DandyGuildHistory/0.1'}), timeout=20) as response:
                metadata = json.loads(response.read(65536))
            if metadata.get('id') != identity or not metadata.get('channel_id'):
                raise ValueError()
        except error.HTTPError as exc:
            raise SenderError(f'Discord rejected webhook verification (HTTP {exc.code}). No messages were sent.') from None
        except error.URLError as exc:
            raise SenderError(f'Could not reach Discord ({type(exc.reason).__name__}). No messages were sent.') from None
        except Exception as exc:
            raise SenderError(f'Could not verify the destination webhook ({type(exc).__name__}). No messages were sent.') from None
        print(json.dumps({'webhookName':metadata.get('name'), 'channelId':metadata['channel_id'],
                          'guildId':metadata.get('guild_id'), 'reviewedMessages':len(entries)}), flush=True)
        if str(metadata['channel_id']) in args.exclude_channel:
            raise SenderError('This webhook points to a prior channel. No messages were sent.')
        print(send_entries(webhook, entries, Journal(), threading.Event(),
              lambda message: print(message, flush=True), opener=opener, approval=approval), flush=True)
    except SenderError as exc:
        print(str(exc), flush=True)
        return 1
    except Exception:
        print('Sending stopped unexpectedly. Review the receipts before retrying.', flush=True)
        return 1
    finally:
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
