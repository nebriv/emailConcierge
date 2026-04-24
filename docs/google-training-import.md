# Importing training data from Google Calendar + Gmail

`email-concierge` can harvest labeled `(email, event)` pairs from your
own Google account to bootstrap the Phase 5 classifier without manual
annotation. Google Calendar has, for years, been auto-extracting events
from booking emails (flights, hotels, restaurants, concerts) — the
events labeled *"Automatically added from Gmail"*. Each such event
links back to the source Gmail message. Pairing them gives us a few
hundred high-quality positive training examples in one shot.

**This is strictly read-only.** The only scopes requested are
`calendar.readonly` and `gmail.readonly`. Nothing is written to Google
Calendar, nothing is modified in Gmail. The code enforces this at two
layers:

1. A scope allowlist in `integrations/google/auth.py` raises
   `ScopeNotAllowed` if any other scope is requested.
2. A ruff TID rule forbids importing `googleapiclient`,
   `google.oauth2`, or `google_auth_oauthlib` anywhere outside
   `integrations/google/*` — the same pattern that locks down
   `imap_tools` to the `ReadOnlyMailbox` wrapper.

---

## One-time setup: create a Google Cloud OAuth client

You need to create your own OAuth client — we deliberately don't ship a
shared one so every self-hoster uses their own quota and nobody's
inbox is behind our creds.

1. Open the [Google Cloud Console](https://console.cloud.google.com/)
   and create a new project (or reuse one you have).
2. In **APIs & Services → Library**, enable both:
   - *Google Calendar API*
   - *Gmail API*
3. In **APIs & Services → OAuth consent screen**:
   - User type: **External** (required for personal Google accounts).
   - Add your own email as a test user under *Test users*. While the
     app is in Testing mode only test users can authorize — that's
     fine, we only ever authorize one user.
   - Scopes: you can leave these blank on the consent screen page;
     the scopes are requested at consent time by the library.
4. In **APIs & Services → Credentials**:
   - Click **Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**.
   - Name it anything (e.g. `email-concierge`).
   - Click **Create**, then **Download JSON**. You now have a
     `client_secret_*.json` file.

## Supplying the secrets

You have two options. Pick one.

### Option A — inline environment variable (no file on disk)

Paste the full JSON content as a single-line value in your `.env`:

```
GOOGLE_CALENDAR_OAUTH_JSON={"installed":{"client_id":"...","client_secret":"...",...}}
```

Either that name or the fully-prefixed
`EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON` is accepted.

### Option B — file path

```
EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_PATH=/data/google_client_secrets.json
```

and place the downloaded JSON at that path.

If both are set, the inline env var wins.

## Running the import

```bash
python -m email_concierge import-training --from-google
```

On first invocation:
- A browser tab opens pointing at Google's consent screen.
- You pick your Google account and click through.
- The library captures the redirect on a local loopback port and
  persists the resulting token to `EMAIL_CONCIERGE_GOOGLE_TOKEN_PATH`
  (default `/data/google_token.json`, stored with `0o600` on POSIX).

Subsequent invocations reuse the cached token and refresh it as needed
— no browser prompt after the first time.

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--since YYYY-MM-DD` | 2 years ago (first run only) | Only import events starting after this date. Overrides the stored cursor. |
| `--limit N` | unlimited | Stop after N new rows are written. Useful for a first sanity check. |

### What gets written

Each paired event produces:

- One row in `processed_messages` with `status='imported_from_google'`
  and `handled_by_name='google_calendar_import'`.
- One row in `training_examples` with `label='event'`,
  `label_source='google'`. The `extracted_json` column holds the
  Google-parsed event (title, start, end, location) plus the Gmail
  internal message ID for later re-fetch.

Re-runs are incremental: the `google_sync_state` table persists an
`updatedMin` cursor that advances to the max `event.updated` seen.
Already-imported messages are skipped via the `UNIQUE(message_id)`
constraint on `training_examples` — safe to re-run at any time.

### Sanity-check the output

```bash
sqlite3 /data/email-concierge.db \
  "SELECT sender, subject,
          json_extract(extracted_json, '$.event.title') AS extracted_title,
          json_extract(extracted_json, '$.event.location') AS extracted_loc
   FROM training_examples
   WHERE label_source = 'google'
   LIMIT 10"
```

## Revoking access

If you uninstall or rotate:

1. Visit [Google Account → Third-party
   access](https://myaccount.google.com/permissions) and revoke the
   OAuth client.
2. Delete `google_token.json` (whichever path you configured).
3. In Google Cloud Console, you can also delete the OAuth client
   entirely if you don't plan to use it again.
