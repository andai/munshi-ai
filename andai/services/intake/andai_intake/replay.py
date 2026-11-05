"""Replay captured mail into the notetaker mailbox over SMTP (D-54).

    python -m andai_intake.replay --host 127.0.0.1 --port 18325 \\
        --mail-from organiser@example.test --rcpt notetaker@munshi.test FILE.eml [FILE.eml ...]

Each file is sent as one message with the given envelope sender and recipients. Line endings are
normalised to CRLF and a final CRLF is added when missing, which is what a mail server puts on the
wire, so the receiver stores exactly those bytes. The headers and body are otherwise untouched (a
captured DKIM signature still verifies). Prints one line per file with the server's answer and
exits 1 if any message was refused. Message content is never printed.
"""

from __future__ import annotations

import argparse
import re
import smtplib
import sys
from pathlib import Path

_EOL = re.compile(rb"\r\n|\r|\n")


def wire_bytes(raw: bytes) -> bytes:
    """`raw` as a mail server transmits it: CRLF line endings, ending in CRLF."""
    data = _EOL.sub(b"\r\n", raw)
    return data if data.endswith(b"\r\n") else data + b"\r\n"


def send(
    host: str,
    port: int,
    mail_from: str,
    rcpt: list[str],
    raw: bytes,
    *,
    helo: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """Send one message; returns the server's final (code, text): the refusal of MAIL FROM, of
    every recipient, or the answer to DATA."""
    data = wire_bytes(raw)
    with smtplib.SMTP(host, port, local_hostname=helo, timeout=timeout) as client:
        client.ehlo()
        options = [f"SIZE={len(data)}"] if client.has_extn("size") else []
        code, text = client.mail(mail_from, options=options)
        if code != 250:
            return code, text.decode(errors="replace")
        accepted = 0
        last: tuple[int, bytes] = (0, b"")
        for address in rcpt:
            last = client.rcpt(address)
            accepted += last[0] in (250, 251)
        if not accepted:
            client.rset()
            return last[0], last[1].decode(errors="replace")
        code, text = client.data(data)
        return code, text.decode(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andai_intake.replay", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18325)
    parser.add_argument("--mail-from", required=True, help="envelope sender (MAIL FROM)")
    parser.add_argument(
        "--rcpt", action="append", required=True, help="envelope recipient (repeatable)"
    )
    parser.add_argument("--helo", default=None, help="name to greet with (default: local host)")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)

    failed = 0
    for path in args.files:
        try:
            code, text = send(
                args.host, args.port, args.mail_from, args.rcpt, path.read_bytes(), helo=args.helo
            )
        except (OSError, smtplib.SMTPException) as exc:
            code, text = 0, type(exc).__name__
        ok = code == 250
        failed += not ok
        print(f"{path.name}: {code} {text}".rstrip())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
