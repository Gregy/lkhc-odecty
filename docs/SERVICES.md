# Services, domain API and shared UI – contract for the reports and capture phases

Everything here is owned by the foundation phase. Import services from `app.services`
(all names below are re-exported there) and pure calculations from `app.domain`.
All service functions take an open SQLAlchemy `Session` (`db`), use only
**confirmed** readings, and never write to the DB.

## Conventions

- **Datetimes** are naive local time (Europe/Prague). Manual/imported readings are stored at 00:00 of
  the reading day. A `date` argument means 00:00 of that day.
- **kWh** are `Decimal`. DB values are exact to 3 decimal places; computed values may carry more
  decimals – round only for display (`|num`, `|kwh`, `|qvalue` filters).
- **Two calculation modes**
  - **Aligned (PRIMARY)** – common boundaries for all meters (calendar months by default, main-meter reading days, or
    dates, or any explicit boundaries). Counter state at a boundary = `value_at` with quality:
    `exact` (reading on that day, or fixed by definition before the first reading / after removal),
    `estimated` (interpolated), `provisional` (extrapolated at the open end, from the average daily usage
    of the last interval extended back to at least 28 days). A meter removed *without* a final reading on
    its removal day is extrapolated up to the removal and flat afterwards – `estimated` (it can never be
    refined by a later reading); periods entirely after the removal are `exact` 0. Values are `QValue`s.
    Used by dashboard, tenant/point pages, charts and settlements.
  - **Raw per reading day (secondary)** – the spreadsheet's "carry to next reading" table (`Decimal | None`,
    None = no data); rows are the days with a confirmed reading (for the import exactly the sheet rows).
    Used for the `/spotreba` "surově po dnech odečtů" view and the XLSX oracle. There is no stored round
    ("obchůzka") entity any more (migration 0003).
- Pass `net=load_network(db)` to several calls in one request to load the data only once.
- Registers are `"T1"` and `"T2"`; meters without T2 simply have no T2 values (aligned tables
  return `QValue(0)` for them).

## Core value types (`app.domain`)

| Type | Meaning |
|---|---|
| `Quality` (StrEnum) | `EXACT` / `ESTIMATED` / `PROVISIONAL`; `.rank`; `worst(iterable)` |
| `QValue(value, quality, estimated_kwh, parts, anomalies)` | computed kWh with quality; `.share_estimated` (0..1); supports `+`, `-`, `.scaled(f)`; `qsum(iterable)`; `QZERO` |
| `Provenance(register, start, end, meter_id, serial, meter_point_id, factor, value)` | one component of a `QValue` (`parts`), start/end are `BoundaryValue`s – use for tooltips/protocols |
| `BoundaryValue(t, cumulative, display, quality, method, before, after, span_days)` | counter state at `t`; `display` = value as on the meter (None without data); `method` ∈ `reading`, `interpolated`, `extrapolated`, `before_first`, `after_removal`, `no_data`; `before`/`after` are the readings used |
| `ReadingPoint(taken_at, value, reading_id, source)` | minimal reading used by domain functions |
| `Period(start: date, end: date)` | raw reading-day period `(start, end]`, `.label` like the sheet (`"1.1.2024-13.1.2024"`) |
| `PeriodCell(value, prev, curr, carried, rollover, anomalies, first_reading)` | raw per-day cell; `value None` = no reading closed a segment in that period |
| `ExpectedRange(last, days, daily_rate, low, expected, high, digits)` | plausibility hint for a new reading |
| `PlausibilityIssue(code, severity, message)` | `code` ∈ `decrease`, `jump` (severity `error`), `high`, `magnitude`, `before_last` (severity `warning`, confirmable); `message` is Czech |
| `Anomaly(kind, prev, curr, delta, daily_rate, typical_rate)` | `kind` ∈ `decrease`, `jump`, `spike`; `.message` Czech |
| `Staleness(last, days_since, usual_interval_days, overdue)` | per register "missing reading" status |

Useful pure functions: `register_delta(prev, curr, digits)`, `value_at(readings, t, digits, *, installed_on, removed_on, interpolator, register) -> BoundaryValue`,
`RegisterSeries(readings, digits, register=…, installed_on=…, removed_on=…)` with `.value_at(t)`, `.consumption(t0, t1) -> QValue`, `.aligned(boundaries) -> list[QValue]`,
`month_boundaries(start, end)`, `check_reading(value, at, readings, digits)`, `expected_range(...)`, `detect_anomalies(...)`, `staleness(...)`.

Interpolation strategies (`app.domain.interpolation`): `Interpolator` protocol with
`share(register, seg_start, seg_end, t) -> Decimal` and
`extrapolate(register, ref_start, ref_end, ref_consumption, last_t, t) -> Decimal`;
`LINEAR` (default) and `ProfileInterpolator(profiles)` (spread proportionally to a reference meter;
build it with `services.profile_interpolator(net)`; off by default). Every aligned service accepts `interpolator=`.

## Services (`app.services`)

### Network snapshot – `network.py`

| Signature | Meaning | Returns |
|---|---|---|
| `load_network(db, *, include_pending=False) -> Network` | all tenants, points, meters, allocation rules and readings in memory | `Network`: `.tenants: list[TenantInfo]` (sort order), `.points: list[PointInfo]` (walk order), `.meters: dict[id, MeterInfo]`, `.rules`, `.series: dict[meter_id, MeterSeries]`, `.points_by_id`, `.tenants_by_id`, `.main_point_ids`, `.point_meters(pid)`, `.point_registers(pid)`, `.columns() -> list[MeterColumn]` |

`TenantInfo(id, name, is_remainder, color, color_dark, sort_order, active)`;
`PointInfo(id, name, description, role, sort_order, parent_point_id, active, meter_ids)`;
`MeterInfo(id, meter_point_id, serial, registers, digits, decimals_shown, installed_on, removed_on, source_type, note, display_note, .active)`;
`MeterColumn(meter_id, register, serial, meter_point_id, point_name)` – one meter × register column like the sheet.

### Periods – `periods.py`

| Signature | Meaning | Returns |
|---|---|---|
| `reading_dates(db, start=None, end=None)` | days with at least one confirmed reading | `list[date]` |
| `main_reading_dates(db, start=None, end=None)` | days with a confirmed main-meter reading | `list[date]` |
| `month_spans(start, end)` | calendar months from `start`'s month up to `end`'s month start (exclusive): `(2025-01-01, 2026-01-01)` → 12 months | `list[PeriodSpan]` |
| `main_reading_spans(db, start=None, end=None)` | aligned periods between consecutive main-meter reading days | `list[PeriodSpan]` |
| `default_month_range(db, today=None)` | `(first month start, month start after the latest reading)` capped at next month of `today`; None without data | `tuple[date, date] \| None` |
| `settlement_month_boundaries(start, end)` | `start`, every 1st of a month inside, `end` | `list[datetime]` |
| `suggest_settlement_bounds(db, year)` | 1.1.–1.1. on aligned months + the main-meter reading days nearest to those dates (e.g. 2.1.2025–4.1.2026) | `SettlementBounds(start, end, boundaries, main_near_start, main_near_end)` |

`PeriodSpan(start: datetime, end: datetime, label, kind)`: `kind` ∈ `month|main|custom`, `.start_date`,
`.end_date`, `.days`, `.key` (`"2025-01"` for months, `"2025-01-02_2025-02-01"` otherwise),
labels `"leden 2025"` / `"2.1.2025–1.2.2025"`.

### Aligned consumption (PRIMARY) – `consumption.py`

All take `granularity="month"|"main"`, `start`/`end` (dates; default = all data via `default_month_range`)
or explicit `boundaries=[...]`, plus `interpolator=None`, `net=None`.

| Signature | Meaning | Returns |
|---|---|---|
| `resolve_spans(db, *, granularity, start, end, boundaries)` | the periods the tables below use | `list[PeriodSpan]` |
| `point_consumption_table(db, ...)` | consumption per meter point × register × period, summed over the point's meters (replacements) | `PointPeriodTable(periods, points, values[point_id][register] -> list[QValue])`, `.value(pid, reg, i)`, `.total(pid, i)` |
| `tenant_consumption_table(db, ...)` | per tenant × register × period; remainder tenant = main − Σ others (per register; present it only as T1+T2 – single-tariff sub-meters count fully into T1); quality = worst component, `estimated_kwh` summed | `TenantPeriodTable(periods, tenants, values[tenant_id][register] -> list[QValue], main[register] -> list[QValue], points: PointPeriodTable)`, `.value(tid, reg, i)`, `.total(tid, i)` (T1+T2), `.period_sum(tid, reg=None)`, `.negative_remainders() -> [(i, reg)]` |
| `consumption_over_interval(db, start, end, *, interpolator=None, net=None)` | one arbitrary interval (settlements): per meter, point and tenant | `IntervalResult(start, end, points[pid][reg], tenants[tid][reg], main[reg], meters: list[MeterIntervalRow], tenant_infos, point_infos, tenant_points[tid] -> [pid])`, `.tenant_total(tid)`, `.quality` |
| `control_balances(table: PointPeriodTable, idx=None)` | control point vs Σ children, and main vs Σ top-level sub/control points, for period `idx` or summed over all | `list[ControlBalanceRow(control_point_id, child_point_ids, register, control, children, difference)]` (QValues) |
| `profile_interpolator(net, reference_point_id=None)` | interpolator following the main (or given) point's meter | `ProfileInterpolator` |

`MeterIntervalRow(meter_id, serial, meter_point_id, register, start: BoundaryValue, end: BoundaryValue, consumption: QValue, installed_on, removed_on, note)` –
the per-tenant protocol: initial/final state, consumption, replacements (`installed_on`/`removed_on`).

Allocation validity (`Allocation.valid_from` inclusive / `valid_to` exclusive) is applied by evaluating the point
only on the part of the interval where the allocation is valid (no time pro-rating).

### Raw per-day view – `raw_table.py`

| Signature | Meaning | Returns |
|---|---|---|
| `raw_consumption_table(db, start=None, end=None, *, net=None)` | sheet `Spotřeba`: meters × registers per reading-day period; `start/end` filter by period *end* date | `RawConsumptionTable(periods: list[Period], columns: list[MeterColumn], cells[i][j]: PeriodCell)`, `.cell(i, meter_id, reg)`, `.point_value(i, point_id, reg)` |
| `raw_allocation_table(db, start=None, end=None, *, net=None)` | sheets `Rozpočítání se/bez sazby` | `RawAllocationTable(periods, tenants, values[i][tenant_id][reg or "total"] -> Decimal \| None, results[i][reg] -> AllocationResult)`, `.value(i, tid, reg="total")`, `.negative_remainders()` |

In this view a period with a partially valid allocation is pro-rated by days (only relevant if
allocations change between two reading days).

### Readings – `readings.py`

| Signature | Meaning | Returns |
|---|---|---|
| `latest_readings(db, meter_ids=None, *, before=None)` | latest confirmed reading per meter/register | `dict[(meter_id, register), ReadingInfo]` |
| `meter_expected_range(db, meter_id, register, at)` | hint for a new reading: last value, typical kWh/day, `low/expected/high` | `ExpectedRange \| None` |
| `check_new_reading(db, meter_id, register, value, at, *, exclude_reading_id=None)` | validate a new or edited value against neighbours (rollover-aware) | `list[PlausibilityIssue]` (empty = OK) |
| `meter_hints(db, at=None, *, net=None)` | active meters in walk order with last readings and expected ranges (for the walk page and extractor hints) | `list[MeterHint(meter_id, meter_point_id, point_name, serial, registers, digits, decimals_shown, display_note, sort_order, last[reg], expected[reg])]` |
| `meter_staleness(db, now=None, *, net=None)` | days since last reading vs. that register's usual interval (active meters), most overdue first | `list[MeterStaleness(meter, point_name, register, staleness)]` |
| `meter_anomalies(db, *, since=None, net=None)` | decreases / jumps / usage spikes, newest first | `list[MeterAnomaly(meter, point_name, register, anomaly)]` |

`ReadingInfo(id, meter_id, register, taken_at, value, source, status, photo_id, reported_by, note)`.

### Alerts – `alerts.py`

| Signature | Meaning | Returns |
|---|---|---|
| `alerts(db, now=None, *, months_back=12, anomaly_days=365, net=None)` | stale meters, anomalies, negative remainder (on the remainder tenant's T1+T2 total, not per register) & negative control balance (aligned months of the last `months_back` months), photos awaiting review, pending readings; most severe first | `list[Alert(kind, severity, message, meter_id, point_id, tenant_id, at, link)]`; `kind` ∈ `stale_meter`, `anomaly`, `negative_remainder`, `control_balance`, `pending_photos`, `pending_readings`; `severity` ∈ `info`, `warning`, `error` |
| `last_reading_info(db, today=None)` | for the dashboard header: newest confirmed reading and newest main-meter reading | `LastReadingInfo(newest, newest_days, main, main_days)` |

### Settlements – `settlement.py`

| Signature | Meaning | Returns |
|---|---|---|
| `settlement_boundaries(start, end)` | period boundaries: `start`, month starts inside, `end` | `list[datetime]` |
| `compute_settlement(db, start, end, *, interpolator=None)` | full result: per period × tenant × register, whole-interval per tenant/point/meter, finalize check | `SettlementResult(start, end, mode, periods, table: TenantPeriodTable, interval: IntervalResult, check: FinalizeCheck)` |
| `settlement_check(db, start, end, *, interpolator=None, net=None, interval=None)` | can it be finalized? No `provisional` boundary value on main points or points allocated to non-remainder tenants; `estimated` allowed but listed | `FinalizeCheck(can_finalize, provisional: list[BoundaryIssue], estimated: list[BoundaryIssue])`; `BoundaryIssue(meter_id, serial, point_name, register, boundary).message` (Czech) |
| `settlement_snapshot(result)` | JSON-serializable dict for `Settlement.snapshot` (version 1: periods, tenants with per-period and total values + quality, main, meters with start/end states, check messages) | `dict` |

Forcing finalization despite `provisional` values: set `Settlement.finalize_forced=True` and
`finalize_note`, and record it with `app.audit`.

### Reports helpers – `reports.py`, `exports.py` (added by the reports phase; not re-exported from `app.services`)

| Signature | Meaning | Returns |
|---|---|---|
| `reports.data_coverage(net)` | how far the data reaches: first month, latest reading, latest main-meter reading, `end` = month start on/before the latest main reading (periods after it would be provisional for the main meter / the remainder) | `Coverage(first, last_reading, last_main_reading, end)`, `.last_month -> (start, end) \| None` |
| `reports.year_range(year, cov, *, include_provisional=False, today=None)` | aligned month range of a year capped at the coverage end (or at the current month start) | `(start, end) \| None` |
| `reports.data_years(cov, today=None)` | years offered in filters | `list[int]` |
| `reports.qjson(q, decimals=1)` / `qdict(q)` | QValue → chart JSON `{v, q, t}` / snapshot dict `{value, quality, estimated_kwh, title}` (`title` = provenance text, like `|quality_title`) | `dict` |
| `reports.boundary_title(b)` | Czech provenance of a `BoundaryValue` ("odhad k 1.1.2026: interpolace mezi odečty … (N dní)") | `str` |
| `reports.quality_label(quality)` / `QUALITY_LEGEND` | text of export "Kvalita" columns: `"předběžně"` for provisional, `""` for exact / interpolated; the single legend entry | `str` |
| `reports.settlement_document(result, net)` | `settlement_snapshot(result)` (version 1, unchanged keys) **plus** display data under extra keys: `reports` (=1), `generated_at`, period `days` and month labels, per value `title`, tenant `color`/`period_totals`/`total`/`allocations` (point × factor × validity window with values), remainder breakdown `remainder` (`main`, `others`, `unallocated`), `main_total`, `period_main`, meter `decimals_shown`/`point_role`/`source_type` and boundary `span_days`/`title`, overall `quality`. Stored as `Settlement.snapshot` at finalization; drafts render a fresh document | `dict` (JSON-safe) |
| `reports.document_tenant(doc, tid)`, `document_tenant_meters(doc, tenant)` | protocol helpers over a document (remainder → main point meters) | |
| `exports.*` | openpyxl/CSV helpers: `write_q` (value; provisional ones get a fill + provenance comment), `write_legend`, `sheet_title`, `csv_bytes` (UTF‑8 BOM, `;`, decimal comma) | |

## Auth, DB session, audit (for routers)

```python
from fastapi import APIRouter, Depends, Request
from app.deps import DbSession, CurrentUser, AdminUser, require_user, require_admin, get_db
from app.templating import templates, flash
from app import audit

router = APIRouter(prefix="/odecty", dependencies=[Depends(require_user)])


@router.post("/{reading_id}")
def edit(request: Request, reading_id: int, db: DbSession, user: AdminUser):
    reading = db.get(Reading, reading_id)
    changes = audit.apply_changes(reading, {"value": new_value})
    if changes:
        audit.record_update(db, user, reading, changes)
    db.commit()
    flash(request, "Uloženo.", "success")
```

- `require_user` → anonymous HTML GET is redirected (303) to `/login?next=…`; HTMX requests get
  401 + `HX-Redirect`. `require_admin` → 403 page for readers.
- `audit.record(db, user, action, entity_type, entity_id, changes, note)`, `record_create`,
  `record_update(db, user, obj, {field: (old, new)})`, `record_delete`, `apply_changes(obj, values)`.

## CSRF

All POST/PUT/PATCH/DELETE requests need the session token (403 otherwise):

- **HTMX**: nothing to do – `app.js` adds `X-CSRF-Token` to every HTMX request (and `<body hx-headers>` does too).
- **fetch()**: nothing to do for same-origin requests – `app.js` wraps `window.fetch` and adds the header.
  Outside page context (service worker / IndexedDB sync), read `lkhc.csrfToken()` (or
  `<meta name="csrf-token">`) in the page and send it as `X-CSRF-Token`; the token is per session.
- **Plain `<form method="post">`**: `app.js` injects a hidden `csrf_token` input; to work without JS
  add `{{ ui.csrf_field() }}` (import macros `with context`). Multipart uploads accept the hidden field too.
- **Tests**: logged-in fixture clients already send the header; `client.csrf_token` holds the value.

## Templates and static

- Pages extend `base.html` and set `page_title` and `active_nav` (= nav URL). Main navigation (`app.templating.NAV`):
  Přehled, Odečty, Spotřeba, Obchůzka, Dávka (admin), Kontrola, Vyúčtování, Správa. `/mista`, `/najemci`, `/fotky`
  are linked from the Správa landing page (`sprava.overview_router`, open to readers, who see only those links;
  the administration sections stay `require_admin`) and set `active_nav = "/sprava"`. Blocks: `head` (extra CSS,
  e.g. `reports.css` / `capture.css`), `content`, `scripts`, `body_class`.
- Context available everywhere: `current_user`, `csrf_token`, `nav`, `tenant_palette`
  (`[{id, name, light, dark, is_remainder}]`), `settings`, `flashes`.
- Formatting lives in `app.formatting` (pure; also used for Czech messages built in `app.domain`):
  `format_number`, `format_reading` (as read: up to 3 decimals, no trailing zeros), `format_kwh`,
  `format_date` (`3.7.2026`, `3.7.` without the year – never with spaces), `format_month` (`7.2026`),
  `format_datetime` (`3.7.2026 14:05`); quality marks: `is_marked(quality)` / `quality_mark(quality)` – only
  `provisional` gets the mark `≈`, interpolated (`estimated`) values are shown like exact ones.
- Filters: `|num(decimals)`, `|kwh(decimals)` (cs-CZ: NBSP thousands, decimal comma, "–" for None),
  `|reading`, `|decimals` (number of decimals actually stored),
  `|date_cs`, `|month_cs`, `|datetime_cs`, `|qvalue(decimals, unit=False)` (≈ + italics + tap tooltip for
  provisional values only; others plain with a hover title), `|qmark`, `|quality_title`. Globals: `Quality`,
  `QUALITY_LABELS`, `REMAINDER_TARIFF_NOTE`.
- `{% import "_macros.html" as ui with context %}`: `csrf_field`, `num`, `kwh`, `qv`, `reg_qv(value, is_remainder)`
  + `remainder_note()` (the remainder tenant is shown only as T1+T2: its T1/T2 cells are "–" with an explanation;
  `reports/_macros.html` has `reg_qd` for snapshot dicts), `badge`, `quality_badge` (provisional only), `source_badge`, `tenant`, `table`, `field`, `select`, `textarea`, `checkbox`,
  `page_header` (call-block for actions), `alert_box`.
- Tenant colours: CSS variables `--tenant-<id>` (switch with `prefers-color-scheme`); in JS
  `lkhc.tenantColors()` → `{ "<id>": {name, color, light, dark} }`. Other JS helpers:
  `lkhc.fmtNumber(v, decimals)`, `lkhc.fmtKwh`, `lkhc.fmtDate` / `fmtDateTime` / `fmtMonth` (same formats as the
  server filters; never `toLocale*String("cs-CZ")`, which adds spaces), `lkhc.isDark()`, `lkhc.cssVar(name)`.
- Vendored: `/static/vendor/htmx-2.0.4.min.js`, `alpinejs-3.14.8.min.js` (both loaded by base.html),
  `chart-4.4.7.umd.min.js` (load it in your page's `scripts` block).
- `app.templating.render_stub(request, title, active_nav, description)` renders the placeholder page.

## Test fixtures (`tests/conftest.py`)

`db_url` / `db` (empty migrated DB), `imported_db_url` / `imported_db` (per-test copy of a DB loaded
by the importer), `make_settings(**overrides)`, `make_app(db_url, **overrides)`, `app`,
`imported_app`, `anon_client`, `admin_client`, `reader_client`, `imported_admin_client`,
`imported_reader_client`, `login_client(app, role="admin", email=None)` (default users `<role>@example.org`,
allowed domain `example.org`), `xlsx_path` / `mapping_path` / `import_mapping` (the **synthetic** workbook and
mapping of `tests/synthetic.py`: fake tenants "Nájemce A"…, fake serials, generated readings, every quirk of the
original sheet; its constants `syn.*_SERIAL`, `syn.*_POINT`, `syn.value(col, day)` are what tests assert on),
`real_private` (the real XLSX + mapping from the private dir; skipped with a reason when absent).
Oracle helpers for the XLSX: `tests/xlsx_oracle.py`; `tests/test_import_oracle.py` runs `[synthetic]` always
and `[real]` only with the private data. No test needs `private/`.

## Live sources

`app.sources`: `ReadingSource` protocol (`kind`, `fetch(meter, since) -> list[Reading]`),
`register_source`, `get_source(meter)` (by `meter.source_config["kind"]`), `available_sources()`.
The module docstring describes how to add an integration. No implementation/scheduler in the MVP.

## Operations (finalize phase)

- `python -m app.migrate` – schema upgrade to head; snapshot `DATA_DIR/backups/pre-migrate-<rev>-<ts>.db`
  before upgrading an existing database. Run by `docker-entrypoint.sh` before uvicorn; the app itself only
  logs an error at start when the DB revision is not head.
- `python -m app.backup [--with-photos] [--dest DIR] [--keep N]` – consistent SQLite snapshot (online backup
  API, WAL-safe), optionally a tar.gz with `photos/`.
- `python -m app.importer FILE|- [--private-dir DIR] [--mapping FILE] [--force] [--bez-oprav]` – the column
  mapping (tenants, serials, corrections) is private installation data, `<private dir>/import-mapping.toml`
  (`app.private`: `--private-dir` > `LKHC_PRIVATE_DIR` > `./private`; format: `app/importer/mapping.py`,
  example `app/importer/import-mapping.example.toml`; `import_workbook(db, path, mapping=...)`). It refuses to run on a database with app-entered data (user actions
  in the audit log, readings not from the import) unless `--force`. Grid corrections of imported readings
  switch their `source` to `manual`, so even a forced re-import keeps them.
- Anomalies: segments shorter than 7 days never raise a usage `spike` (noise / date typos).
- Allocations: `/sprava` refuses an allocation that would allocate a point by more than 100 % in an
  overlapping validity window (or the same tenant twice); the remainder tenant's own rules do not count.
- Confirmed source-data corrections: `[[date_corrections]]` of the private mapping (`DateCorrection` in
  `app/importer/corrections.py`), applied by default and
  listed in the import report / audit; `import_workbook(..., corrections=False)` / `--bez-oprav` imports the
  sheet as exported. The test fixture `imported_db` is the sheet as exported (the XLSX oracle compares every
  cell against it); `imported_corrected_db` has the corrections applied.
- `python -m app.tools.move_reading_date --from D --to D [--dry-run]` – moves all readings and the point skips of a
  day (snapshot first, audited, refuses on conflicts with the target day).
- Photo review (`app/capture/review.py`): photos within 60 s form a *display* group only. A readout is tied to
  a meter by its own evidence (`exact`/`fuzzy` serial, `walk` point, `manual`, or `values` = fits exactly one
  active meter); without it the row is unassigned with ranked `candidate_meter_ids`. `Row.strongly_identified`
  = some photo of the row is `exact`/`walk`/`manual`; bulk confirm requires it for every row. A walk photo whose
  serial belongs to another meter than the tapped point becomes `walk_mismatch` (unassigned, both suggested);
  the walk API `/obchuzka/api/foto/{uuid}` returns the warning in `mismatch` and prefills nothing for it.
- Extraction evaluation: `python -m app.extraction.truth DAY --out FILE` (meter truth of one day from the DB),
  `python -m app.extraction.eval DIR|ZIP --expected FILE [--out report.json] [--verify|--no-verify]` (see the
  module docstring; the verify default is `EXTRACTOR_VERIFY`; `--replay` re-applies a stored second read).
- Second read (`EXTRACTOR_VERIFY=1`, default off, **doubles the per-photo model cost**): `OpenAICompatExtractor`
  re-reads every photo whose first read has usable digits (an independent request, same image and prompt).
  `compare_reads` pairs meters by normalized serial, then the leftover meters in order when both reads left the
  same number (`only` / `position`); a register value counts as reproduced when the paired meter shows the same
  digits (leading zeros ignored; a decimal point present in both reads must match) for the same register or
  an unlabelled one. Not reproduced, or the second request failed: confidence capped at 0.5, flag
  `low_confidence` (a review doubt: blocks bulk confirm, needs an ack; walk prefill shows it as a flag) and
  `RegisterValue.second_read` = what the second read showed (`""` = nothing). The photo is never an error
  because of the second read. `raw.usage` = both calls summed (`raw.calls = 2`), `raw.verify` = second answer,
  its usage, error and the per-register comparison.
