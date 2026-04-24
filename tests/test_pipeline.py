from __future__ import annotations

import json

from email_concierge.pipeline import process_email


def test_successful_extraction_writes_event_and_training_example(
    tmp_db, make_email, make_result, stub_extractor, recording_sink
):
    email = make_email(message_id="<a@example.com>", sender="bookings@vendor.com")
    ext = stub_extractor("ics", stage=1, result=make_result(stage=1, name="ics"))
    sink = recording_sink()

    status = process_email(email, tmp_db, [ext], sink)
    assert status == "processed"
    assert len(sink.writes) == 1

    pm = tmp_db.execute("SELECT * FROM processed_messages").fetchall()
    assert len(pm) == 1
    assert pm[0]["status"] == "processed"
    assert pm[0]["handled_by_stage"] == 1

    te = tmp_db.execute("SELECT * FROM training_examples").fetchall()
    assert len(te) == 1
    assert te[0]["label"] == "event"
    assert te[0]["label_source"] == "auto"
    assert json.loads(te[0]["extracted_json"])["title"] == "Test Event"


def test_no_extraction_still_records_negative_training_example(
    tmp_db, make_email, stub_extractor, recording_sink
):
    email = make_email(message_id="<b@example.com>")
    ext = stub_extractor("none", stage=1, result=None)
    sink = recording_sink()

    status = process_email(email, tmp_db, [ext], sink)
    assert status == "no_extraction"
    assert sink.writes == []

    pm = tmp_db.execute("SELECT * FROM processed_messages").fetchone()
    assert pm["status"] == "no_extraction"
    assert pm["handled_by_stage"] is None

    te = tmp_db.execute("SELECT label, label_source FROM training_examples").fetchone()
    assert te["label"] == "neither"
    assert te["label_source"] == "auto"


def test_idempotent_dedup_on_message_id(
    tmp_db, make_email, make_result, stub_extractor, recording_sink
):
    email = make_email(message_id="<dupe@example.com>")
    ext = stub_extractor("ics", stage=1, result=make_result(stage=1))
    sink = recording_sink()

    first = process_email(email, tmp_db, [ext], sink)
    second = process_email(email, tmp_db, [ext], sink)
    assert first == "processed"
    assert second == "skipped_dedup"
    assert len(sink.writes) == 1  # second call didn't hit the sink


def test_sender_deny_list_skips_without_hitting_extractors(
    tmp_db, make_email, make_result, stub_extractor, recording_sink, monkeypatch
):
    monkeypatch.setenv("EMAIL_CONCIERGE_SENDER_DENY", "noreply@spam.com")
    from email_concierge.config import settings
    settings.cache_clear()  # type: ignore[attr-defined]

    email = make_email(message_id="<spam@x>", sender="NOREPLY@spam.com")
    ext_spy = stub_extractor("ics", stage=1, result=make_result(stage=1))
    sink = recording_sink()

    try:
        status = process_email(email, tmp_db, [ext_spy], sink)
    finally:
        settings.cache_clear()  # type: ignore[attr-defined]

    assert status == "skipped_filter"
    assert sink.writes == []
    row = tmp_db.execute("SELECT status FROM processed_messages").fetchone()
    assert row["status"] == "skipped_filter"


def test_sender_allow_list_excludes_non_matching(
    tmp_db, make_email, make_result, stub_extractor, recording_sink, monkeypatch
):
    monkeypatch.setenv("EMAIL_CONCIERGE_SENDER_ALLOW", "@united.com,@marriott.com")
    from email_concierge.config import settings
    settings.cache_clear()  # type: ignore[attr-defined]

    email = make_email(message_id="<other@x>", sender="random@example.com")
    sink = recording_sink()

    try:
        status = process_email(
            email,
            tmp_db,
            [stub_extractor("ics", stage=1, result=make_result(stage=1))],
            sink,
        )
    finally:
        settings.cache_clear()  # type: ignore[attr-defined]

    assert status == "skipped_filter"
    assert sink.writes == []


def test_sink_exception_marks_failed(
    tmp_db, make_email, make_result, stub_extractor
):
    class ExplodingSink:
        def write(self, result, message_id):
            raise RuntimeError("caldav down")

    email = make_email(message_id="<err@x>")
    status = process_email(
        email,
        tmp_db,
        [stub_extractor("ics", stage=1, result=make_result(stage=1))],
        ExplodingSink(),
    )
    assert status == "failed"
    row = tmp_db.execute("SELECT status, error FROM processed_messages").fetchone()
    assert row["status"] == "failed"
    assert "caldav down" in row["error"]
