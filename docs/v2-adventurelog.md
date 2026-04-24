# emailConcierge — v2.0 Supplement: AdventureLog Integration

**Do not implement any of this until v1.5 has shipped and been stable in production for a meaningful window.** This document exists so the v2.0 design can be captured while it's fresh, but Claude Code should not read or act on it while working on v1.0/v1.5. The main project plan (`email-concierge-project-plan.md`) is the operative document for the initial build.

The purpose of this supplement is to extend Concierge with the ability to recognize travel-related email (flights, hotels, activities) and populate corresponding resources in [AdventureLog](https://github.com/seanmorley15/AdventureLog) — reproducing the "Google Trips" experience that was lost when Google retired Trips.

---

## 1. Prerequisites

Before starting v2.0:

- v1.5 is tagged and running in production.
- The read-only IMAP guarantees from the main plan (section 4 of `email-concierge-project-plan.md`) have not been weakened. Everything in v2.0 continues to respect those guarantees; the only new write surface added in v2.0 is against AdventureLog's REST API.
- **Verify AdventureLog supports POST / PATCH on the resources this feature needs.** The public Swagger only documents GET on `/api/transportations/`, `/api/lodging/`, `/api/notes/`, `/api/locations/`, `/api/visits/`. Test against a real instance (ten-minute curl exercise) before committing to the build. If POST is missing, file an upstream issue and pause v2.0 until it's addressed.

If the prerequisite verification fails, **stop**. The rest of this document is predicated on AdventureLog being writable via its REST API.

---

## 2. Scope

### In scope

- Trip-aware classification: extend the stage-3 classifier to produce `event | trip_item | neither` instead of `event | neither`.
- Extend `ParsedEvent` into `ParsedEvent | ParsedTripItem` union; router handles either.
- Plugin updates for travel-related vendors to emit `ParsedTripItem` where appropriate.
- AdventureLog sink:
  - Collection lookup by date range and destination; create if no match.
  - POST Transportation records for flights/trains.
  - POST Lodging records for hotels/rentals.
  - POST Location + Visit records for activities (concerts, tours, museum bookings, etc.).
  - Trip-level Note records for things like visa confirmations or travel insurance.
- Dual-write: a parsed trip item produces both a CalDAV event AND the corresponding AdventureLog resource, linked by Message-ID in the local SQLite.
- Confirmation-number-based dedup in AdventureLog (same flight re-sent should update, not duplicate).

### Out of scope

- Editing or removing AdventureLog records the user has manually adjusted. v2.0 only creates new records; once a record exists, user edits are preserved unless the source confirmation number is re-sent (which triggers an update of the fields that came from the email, not fields the user changed manually).
- Image extraction or attachment forwarding to AdventureLog.
- Itinerary day generation. The user continues to manage AdventureLog itinerary structure manually.
- Two-way sync. AdventureLog is a sink, not a source.

---

## 3. Architecture changes

The pipeline from v1.5 gains a second sink:

```
                ┌───────────────────┐
                │ Router            │
                └──┬──────────────┬─┘
                   │              │
                   ▼              ▼
          ┌──────────────┐  ┌──────────────┐
          │ CalDAV sink  │  │ AdventureLog │
          │              │  │    sink      │
          └──────────────┘  └──────────────┘
```

Dispatch rule:

- `ParsedEvent` → CalDAV only.
- `ParsedTripItem` → CalDAV (as an event for visibility) AND AdventureLog (as a structured resource).

The dual-write to CalDAV exists because users want travel appearing on their calendar even once AdventureLog has the structured data. It's opt-outable via `EMAIL_CONCIERGE_TRIP_ITEMS_TO_CALDAV=false`.

---

## 4. Data model extensions

### New Pydantic model

```python
class ParsedTripItem(BaseModel):
    item_type: Literal["transportation", "lodging", "activity"]
    confirmation_number: str | None

    # transportation
    transport_type: Literal["plane", "train", "bus", "car", "boat", "other"] | None
    flight_number: str | None
    from_location: str | None
    to_location: str | None
    from_lat: float | None
    from_lon: float | None
    to_lat: float | None
    to_lon: float | None
    start_code: str | None   # IATA for planes, station code for trains
    end_code: str | None

    # lodging
    lodging_type: Literal["hotel", "hostel", "resort", "bnb", "campground",
                          "cabin", "apartment", "house", "villa", "motel", "other"] | None
    check_in: datetime | None
    check_out: datetime | None
    address: str | None

    # activity
    activity_name: str | None
    activity_start: datetime | None
    activity_end: datetime | None

    # shared
    title: str
    description: str | None
    timezone: str | None       # IANA name; required if datetimes are naive
```

### ExtractionResult update

```python
class ExtractionResult(BaseModel):
    handled_by_stage: int
    handled_by_name: str
    confidence: float
    parsed: ParsedEvent | ParsedTripItem   # changed from just ParsedEvent
    latency_ms: int
    notes: list[str] = Field(default_factory=list)
```

### New SQLite table

```sql
CREATE TABLE IF NOT EXISTS trip_items (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id                   TEXT NOT NULL,
    adventurelog_collection_id   TEXT NOT NULL,
    adventurelog_resource_type   TEXT NOT NULL,  -- 'transportation' | 'lodging' | 'location' | 'note'
    adventurelog_resource_id     TEXT NOT NULL,
    confirmation_number          TEXT,
    created_at                   TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES processed_messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_ti_collection ON trip_items(adventurelog_collection_id);
CREATE INDEX IF NOT EXISTS idx_ti_confirmation ON trip_items(confirmation_number);

-- Queue for retries when AdventureLog is unavailable
CREATE TABLE IF NOT EXISTS pending_adventurelog_writes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT NOT NULL,
    parsed_json         TEXT NOT NULL,
    last_error          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_retry_at       TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
```

---

## 5. AdventureLog mapping

Based on the Swagger at `backend.travel.kohlmeyer.me`:

| Email type | AdventureLog resource | Key fields |
|---|---|---|
| Flight confirmation | `Transportation` (type=plane) | `flight_number`, `from_location`, `to_location`, `date`, `end_date`, `start_timezone`, `end_timezone`, `origin_lat/lon`, `destination_lat/lon`, `start_code` (IATA), `end_code` (IATA), `collection` |
| Train booking | `Transportation` (type=train) | as above minus `flight_number` |
| Hotel booking | `Lodging` | `name`, `type=hotel`, `check_in`, `check_out`, `timezone`, `reservation_number`, `latitude`, `longitude`, `location` (address), `collection` |
| Activity / tour / concert ticket | `Location` + `Visit` | Location: `name`, `latitude`, `longitude`, `tags`, `category`. Visit: `start_date`, `end_date`, `timezone` |
| Trip-level note | `Note` | `name`, `content`, `collection` |

### Collection matching

1. Extract destination and dates from the parsed item.
2. GET `/api/collections/all/`. Find any Collection where the item's date falls within `[start_date, end_date]`.
3. Of the matches, prefer the one whose `name` contains the destination string.
4. If none match, create a new Collection using `EMAIL_CONCIERGE_ADVENTURELOG_DEFAULT_COLLECTION_NAME_FORMAT`.

### Update semantics

A re-sent confirmation (same confirmation number, possibly different times) should:

1. Look up the prior `trip_items` row by `confirmation_number`.
2. If found, PATCH the corresponding AdventureLog resource with the updated fields from the new email.
3. If not found, create as normal.

Fields PATCH'd on update are restricted to the ones the email originated: times, flight number, gate/terminal if available. User-editable fields like rating, description, or custom tags are **never** overwritten by an update.

---

## 6. Configuration additions

```bash
# AdventureLog (v2.0)
EMAIL_CONCIERGE_ADVENTURELOG_URL=https://adventurelog.home.nebriv.com
EMAIL_CONCIERGE_ADVENTURELOG_USERNAME=ben
EMAIL_CONCIERGE_ADVENTURELOG_PASSWORD=app-password
EMAIL_CONCIERGE_ADVENTURELOG_DEFAULT_COLLECTION_NAME_FORMAT="Trip to {destination} ({start_date:%b %Y})"

# Dual-write behavior
EMAIL_CONCIERGE_TRIP_ITEMS_TO_CALDAV=true

# Retry queue
EMAIL_CONCIERGE_ADVENTURELOG_RETRY_INTERVAL_SECONDS=300
EMAIL_CONCIERGE_ADVENTURELOG_MAX_RETRIES=20
```

If `ADVENTURELOG_URL` is empty, the sink is disabled and `ParsedTripItem` values are treated as `ParsedEvent` (CalDAV only). This is the behavior users get if they haven't enabled v2.0.

---

## 7. Phased implementation

### Phase 7 — AdventureLog read-side (half day)

**Start here.** Only proceed to Phase 8 if this completes cleanly.

- `sinks/adventurelog.py`: HTTP Basic auth client using `httpx`.
- Read-only methods: `list_collections()`, `find_collection_for(date, destination)`, `list_lodging_in(collection_id)`, `list_transportations_in(collection_id)`.
- Smoke test: `python -m email_concierge adventurelog-test` — connects, lists collections, logs them.

**Done when:** the service can list Collections and correctly match a given date to an existing Collection.

### Phase 8 — Classifier + parser extensions (one day)

- Update classifier training to produce three classes (`event | trip_item | neither`).
- Retrain on existing `training_examples` — labels need to be re-examined to split the old `event` bucket into `event` and `trip_item` based on whether the extracted output has travel fields. This can be bootstrapped from the LLM.
- Update LLM prompts to emit a `ParsedTripItem` when the email is travel-related.
- Update stage 2 plugins for travel vendors (airlines, hotels, Airbnb, Amtrak) to return `ParsedTripItem`.
- Stage 3 heuristic assembler extended with travel-specific rules.

**Done when:** a test flight-confirmation email produces a `ParsedTripItem` (not a `ParsedEvent`) via stages 2, 3, and 4 all three.

### Phase 9 — AdventureLog write-side (one to two days)

- Sink methods: `find_or_create_collection`, `create_transportation`, `create_lodging`, `create_location_and_visit`, `create_note`.
- PATCH support keyed on prior `trip_items` row, with field-restriction rules from section 5.
- Dual-write routing.
- Confirmation-number dedup.

**Done when:**
1. A flight confirmation creates a Collection (if none matches), attaches a Transportation, AND creates a CalDAV event. The `trip_items` table has a row linking Message-ID → Collection → resource.
2. A subsequent hotel confirmation for the same dates attaches Lodging to the same Collection, not a new one.
3. Re-sending the flight confirmation with a different departure time updates the existing Transportation record.

### Phase 10 — Retry queue + v2.0 release (half day)

- `pending_adventurelog_writes` table populated when AdventureLog is unreachable.
- Retry worker reads the queue on an interval.
- README additions: AdventureLog setup, what v2.0 does, how to disable it.
- Tag v2.0.

---

## 8. Testing

- Mock AdventureLog server using FastAPI for integration tests, covering: list, create (for each resource type), patch, auth failures.
- Per-vendor fixture updates: existing plugin fixtures for airlines/hotels need `expected.json` updated to reflect `ParsedTripItem` instead of `ParsedEvent`.
- End-to-end test: replay a flight-then-hotel sequence, assert one Collection, one Transportation, one Lodging, two CalDAV events.

---

## 9. Open questions to resolve before Phase 7

1. **POST endpoint availability and shape.** The published Swagger only shows GET. Verify against a live instance with actual POST calls before beginning the build. If POST bodies differ from GET responses in unexpected ways, document the mapping.
2. **CSRF.** Django REST Framework with SessionAuthentication requires CSRF tokens. BasicAuthentication typically does not, but some deployments layer CSRF on everything. Confirm Basic works on POST.
3. **Timezone handling at AdventureLog's end.** Every timezone field in the Swagger is an enum of full IANA names. Emails often contain short codes (PST, EDT). The parser must resolve these to full IANA names before handing to the sink, and the sink must validate they're in the enum.
4. **Activities are the hardest case.** A "concert at Madison Square Garden" needs a `Location` record. Does the user already have one, or should the sink create one? Proposal: always create a new Location per activity; don't try to dedupe against existing ones. Users can consolidate later.
5. **Collection naming when destinations differ across items.** A trip with flights to Tokyo, a stopover hotel in Seoul, and then a Kyoto hotel — does that belong in one Collection or several? Proposal: always attach to whichever Collection covers the date. If there is none, use the first-seen destination to name a new one, and the user can rename. Don't try to be clever.

---

## 10. Out-of-scope reminders for v2.0

- Do not edit AdventureLog records the user has modified manually. PATCH is scoped to the fields that originated from the email.
- Do not create AdventureLog records for non-travel events. A birthday dinner on OpenTable is a calendar event, not a trip item.
- Do not attempt to infer trip structure beyond what's in the email. If a flight confirmation doesn't mention a hotel, the sink doesn't go looking for one. Each email stands alone.
- Do not weaken the read-only IMAP guarantees from the main plan. Nothing in v2.0 changes the fact that IMAP access is strictly read-only.