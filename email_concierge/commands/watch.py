"""watch — tail recent pipeline activity from the local DB.

Live-inspection tool. Queries `processed_messages` for rows within a
time window and prints one compact line per message. In `--follow`
mode, polls periodically and prints new rows as they appear.

Useful during the post-ship watch period: confirm validators fire on
the right things, spot senders drifting back into LLM stage, surface
failures that otherwise hide in JSON-log noise.

Pure read-only against SQLite. Safe to run alongside the listener.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from email_concierge import db
from email_concierge.config import settings
from email_concierge.log import get_logger

log = get_logger(__name__)

_REL_RE = re.compile(r"^\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*$")

_STATUS_LABEL = {
    "processed": "OK  ",
    "rejected": "REJ ",
    "no_extraction": "MISS",
    "skipped_filter": "SKIP",
    "skipped_dedup": "DUPE",
    "failed": "FAIL",
}

_VALID_STATUSES = set(_STATUS_LABEL)


def watch_command(
    *,
    since: str = "15m",
    status: str | None = None,
    stage: int | None = None,
    follow: bool = False,
    interval: float = 5.0,
    summary: bool = False,
    output: TextIO | None = None,
) -> int:
    """Print recent pipeline activity. Returns 0 on clean exit."""
    out = output if output is not None else sys.stdout

    if status is not None and status not in _VALID_STATUSES:
        print(
            f"watch: unknown --status '{status}'. "
            f"Valid: {sorted(_VALID_STATUSES)}",
            file=sys.stderr,
        )
        return 2

    try:
        cutoff = _parse_since(since)
    except ValueError as e:
        print(f"watch: {e}", file=sys.stderr)
        return 2

    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    try:
        if summary:
            _print_summary(conn, cutoff, status=status, stage=stage, out=out)
            return 0

        last_seen = _print_rows(
            conn, cutoff, status=status, stage=stage, out=out,
        )
        if not follow:
            return 0

        while True:
            time.sleep(interval)
            last_seen = _print_rows(
                conn, last_seen, status=status, stage=stage, out=out,
            )
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


def _parse_since(spec: str) -> datetime:
    """Accept '15m', '2h30m', '1d', or ISO-8601. Return a UTC datetime."""
    now = datetime.now(tz=UTC)
    spec = spec.strip()
    if not spec:
        raise ValueError("empty --since")

    # Try relative first (cheap, common case).
    m = _REL_RE.match(spec)
    if m and any(m.groups()):
        d, h, mi, s = (int(g) if g else 0 for g in m.groups())
        delta = timedelta(days=d, hours=h, minutes=mi, seconds=s)
        if delta.total_seconds() <= 0:
            raise ValueError(f"invalid --since '{spec}' (zero duration)")
        return now - delta

    # Fall back to ISO-8601.
    try:
        dt = datetime.fromisoformat(spec.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"could not parse --since '{spec}' as relative (e.g. '15m', '2h') "
            f"or ISO-8601 datetime"
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _print_rows(
    conn: sqlite3.Connection,
    cutoff: datetime,
    *,
    status: str | None,
    stage: int | None,
    out: TextIO,
) -> datetime:
    """Fetch rows with processed_at > cutoff, print them, return the new cutoff.

    The returned cutoff is the max processed_at seen in this batch, so the
    next call yields strictly new rows. If no rows were fetched, the cutoff
    is unchanged.
    """
    rows = _fetch_rows(conn, cutoff, status=status, stage=stage)
    if not rows:
        return cutoff
    for row in rows:
        out.write(_format_row(row) + "\n")
    out.flush()
    # `processed_at` is ISO-8601 UTC — comparable as-is.
    new_cutoff = max(_parse_ts(r["processed_at"]) for r in rows)
    return new_cutoff


def _fetch_rows(
    conn: sqlite3.Connection,
    cutoff: datetime,
    *,
    status: str | None,
    stage: int | None,
) -> list[sqlite3.Row]:
    sql = [
        "SELECT message_id, received_at, sender, subject, handled_by_stage,",
        "       handled_by_name, confidence, status, error, processed_at",
        "  FROM processed_messages",
        " WHERE processed_at > ?",
    ]
    args: list[Any] = [cutoff.isoformat()]
    if status is not None:
        sql.append(" AND status = ?")
        args.append(status)
    if stage is not None:
        sql.append(" AND handled_by_stage = ?")
        args.append(stage)
    sql.append(" ORDER BY processed_at ASC")
    return conn.execute("\n".join(sql), args).fetchall()


def _print_summary(
    conn: sqlite3.Connection,
    cutoff: datetime,
    *,
    status: str | None,
    stage: int | None,
    out: TextIO,
) -> None:
    rows = _fetch_rows(conn, cutoff, status=status, stage=stage)
    total = len(rows)
    out.write(f"Window: since {cutoff.isoformat()}  total={total}\n")
    if total == 0:
        out.flush()
        return

    statuses: Counter[str] = Counter(r["status"] for r in rows)
    out.write("By status:\n")
    for s, n in statuses.most_common():
        pct = 100 * n / total
        out.write(f"  {s:16s} {n:5d}  {pct:5.1f}%\n")

    reject_reasons = Counter(
        (r["error"] or "unknown").split(" ", 1)[0]
        for r in rows if r["status"] == "rejected"
    )
    if reject_reasons:
        out.write("Rejection reasons:\n")
        for reason, n in reject_reasons.most_common():
            out.write(f"  {reason:32s} {n:5d}\n")

    handled = Counter(
        (r["handled_by_stage"], r["handled_by_name"])
        for r in rows if r["handled_by_name"] is not None
    )
    if handled:
        out.write("Handled by:\n")
        for (stg, name), n in handled.most_common():
            stg_str = f"stage {stg}" if stg is not None else "(no stage)"
            out.write(f"  {stg_str:10s} {name:24s} {n:5d}\n")

    out.flush()


def _format_row(row: sqlite3.Row) -> str:
    ts = _parse_ts(row["processed_at"]).astimezone().strftime("%H:%M:%S")
    label = _STATUS_LABEL.get(row["status"], row["status"][:4].upper())
    stage = row["handled_by_stage"]
    stage_str = f"s{stage}" if stage is not None else "  "
    name = _truncate(row["handled_by_name"] or "-", 18)
    conf = row["confidence"]
    conf_str = f"{conf:.2f}" if conf is not None else "-   "
    sender = _truncate(row["sender"] or "", 28)
    subject = _truncate(row["subject"] or "", 50)
    line = f"{ts} {label} {stage_str} {name:18s} {conf_str:4s} {sender:28s} {subject}"
    # For rejected rows, surface the reason on the same line.
    if row["status"] == "rejected" and row["error"]:
        line += f"  [{row['error']}]"
    elif row["status"] == "failed" and row["error"]:
        line += f"  [{_truncate(row['error'], 60)}]"
    return line


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__ = ["watch_command"]
