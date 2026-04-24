# training/

Drop **real** `.eml` files here for plugin authoring. Contents are gitignored — only this README and `.gitkeep` get committed.

## Layout

Organize by vendor / plugin name:

```
training/
├── united_airlines/
│   ├── confirmation-2024-06.eml
│   └── boarding-pass.eml
├── marriott/
│   └── reservation.eml
└── eventbrite/
    └── ticket.eml
```

The subdirectory name is the plugin name — it becomes the `name` attribute on the extractor class and the directory under `tests/fixtures/emails/` that receives redacted copies.

## Getting `.eml` files out of your mail client

- **Gmail / Fastmail / most webmail:** open the message → "Show original" / "Download original" → save as `.eml`.
- **Apple Mail:** File → Save As → Raw Message Source.
- **Thunderbird:** right-click message → Save As.
- **Outlook:** File → Save As → `.eml` (not `.msg`).

One file per message. Multiple variants of the same vendor (confirmation / itinerary change / cancellation) are useful — plugins need to handle more than one template.

## From raw drop → committed fixture

After dropping files here:

```
python -m email_concierge export-fixtures --plugin=<vendor> --from-training
```

This reads `training/<vendor>/*.eml`, runs each through `email_concierge.redaction`, and writes safe-to-commit copies to `tests/fixtures/emails/<vendor>/`. A skeleton `expected.json` is written alongside each fixture — review it, correct the ground-truth event, then commit.

**Before committing a redacted fixture, eyeball it for leaked PII.** Redaction is best-effort: it rewrites email addresses, phone numbers, confirmation codes, and card numbers, but it does not catch names, street addresses, or free-form personal context. If in doubt, edit the redacted file by hand before committing.

## What does not get committed

Everything in `training/` except `.gitkeep` and this `README.md`. The `.gitignore` entry is:

```
training/*
!training/.gitkeep
!training/README.md
```
