"""Tests for the `label` command (manual training_examples relabeling)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from email_concierge.commands.label import label_command


@pytest.fixture
def training_rows(tmp_db):
    """Seed training_examples with a pair of rows we can flip."""
    rows = [
        ("<fp1@x>", "promo@eff.org", "EFF Benefit Night", "Join us...", "event", "auto"),
        ("<fp2@x>", "lyft@lyft.com", "Thanks for your ride", "You rode...", "event", "auto"),
        ("<ok@x>", "united@united.com", "UA123 SFO→JFK", "Boarding...", "event", "auto"),
    ]
    now = datetime.now(tz=UTC).isoformat()
    for mid, sender, subject, body, label, source in rows:
        tmp_db.execute(
            """
            INSERT INTO processed_messages
                (message_id, received_at, sender, subject, status, processed_at)
            VALUES (?, ?, ?, ?, 'processed', ?)
            """,
            (mid, now, sender, subject, now),
        )
        tmp_db.execute(
            """
            INSERT INTO training_examples
                (message_id, sender, subject, body_preview,
                 label, label_source, extracted_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mid, sender, subject, body, label, source, None, now),
        )
    tmp_db.commit()
    return tmp_db


def _run_with_db(conn, **kwargs):
    """Patch the command's DB resolver to reuse our test connection."""
    with patch("email_concierge.commands.label.db.connect", return_value=conn), \
         patch("email_concierge.commands.label.db.init_schema"):
        return label_command(**kwargs)


def test_label_flips_single_row(training_rows):
    rc = _run_with_db(
        training_rows,
        message_ids=["<fp1@x>"],
        label="neither",
        reason="mass-mail announcement",
    )
    assert rc == 0

    row = training_rows.execute(
        "SELECT label, label_source FROM training_examples WHERE message_id = ?",
        ("<fp1@x>",),
    ).fetchone()
    assert row["label"] == "neither"
    assert row["label_source"] == "manual"


def test_label_batch_update(training_rows):
    rc = _run_with_db(
        training_rows,
        message_ids=["<fp1@x>", "<fp2@x>"],
        label="neither",
    )
    assert rc == 0

    rows = training_rows.execute(
        "SELECT message_id, label, label_source FROM training_examples "
        "WHERE message_id IN ('<fp1@x>', '<fp2@x>')"
    ).fetchall()
    assert {r["label"] for r in rows} == {"neither"}
    assert {r["label_source"] for r in rows} == {"manual"}


def test_label_untouched_rows_remain(training_rows):
    _run_with_db(
        training_rows,
        message_ids=["<fp1@x>"],
        label="neither",
    )
    row = training_rows.execute(
        "SELECT label, label_source FROM training_examples WHERE message_id = ?",
        ("<ok@x>",),
    ).fetchone()
    assert row["label"] == "event"
    assert row["label_source"] == "auto"


def test_label_dry_run_does_not_write(training_rows):
    rc = _run_with_db(
        training_rows,
        message_ids=["<fp1@x>"],
        label="neither",
        dry_run=True,
    )
    assert rc == 0
    row = training_rows.execute(
        "SELECT label, label_source FROM training_examples WHERE message_id = ?",
        ("<fp1@x>",),
    ).fetchone()
    assert row["label"] == "event"
    assert row["label_source"] == "auto"


def test_label_missing_message_id_is_warning_not_failure(training_rows):
    rc = _run_with_db(
        training_rows,
        message_ids=["<does-not-exist@x>"],
        label="neither",
    )
    assert rc == 0  # absence is a warning, not an error — idempotent replays


def test_label_rejects_invalid_label(training_rows):
    rc = _run_with_db(
        training_rows,
        message_ids=["<fp1@x>"],
        label="maybe",
    )
    assert rc == 2
