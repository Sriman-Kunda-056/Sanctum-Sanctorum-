# Notes

**Live URL:** https://sanctum-sanctorum-ugtq.onrender.com/ — UI at `/`, API at the root
(e.g. `/books`, `/reports/top-books`), Swagger docs at `/docs`. No login required; seeded
members (ids 1–4, tiers supreme/master/adept/apprentice) and 12 seeded books are already
in the database.

## What's finished

All five areas from ASSIGNMENT.md are implemented and the full test suite passes
(`uv run pytest -q`, 0 failures / 0 errors):

- **Books** — ISBN-13 checksum validation and normalization (digits only, hyphens/spaces
  stripped), duplicate-ISBN 409, `PATCH /books/{id}` (partial update, `isbn` and unknown
  fields silently ignored, validation runs before any mutation so a 422 never leaves a
  book half-updated), and `GET /books` filtering (title/author substring, `restricted`,
  inclusive `min_price`/`max_price`), sorting (`title`/`-title`/`price`/`-price` with an
  id tie-break), and pagination with a `total` computed before `limit`/`offset`.
- **Members** — name/email stripping, email lowercasing + format validation, duplicate
  email rejected case-insensitively with 409, `created_at` taken from the injected clock.
  Fixed a real bug in `tier_at_least`: the index comparison was strict (`>`), which meant
  a `master` member couldn't access `master`-gated restricted books — their own tier.
  Implemented `GET /members/{id}/stats` (paid order count/total, active and overdue loan
  counts, sum of late fees on returned loans).
- **Orders** — schema-level validation for empty item lists and duplicate `book_id`s (422).
  `create_order` checks member/book existence (404), restricted-book access (403), then
  stock (409) in that order, before mutating anything — a failed order never partially
  decrements stock. Tier + bulk discounts combine and floor-divide correctly. Unit prices
  are snapshotted onto each `OrderItem` at creation time, so later price changes don't
  affect existing orders. `cancel_order` restores stock for every item; `pay_order` leaves
  stock untouched (it was already reserved at creation).
- **Loans** — added the missing `due_at`, `returned_at`, `late_fee_cents` columns to the
  `Loan` model. Implemented the full borrowing check order (member/book 404, restricted
  403, overdue-loan block, duplicate-active-loan block, tier limit, out-of-stock 409),
  return handling (409 on double-return, stock restored exactly once, late fee computed
  and persisted), and the member loan listing with an optional status filter. Status is
  always computed at read time, never stored, so it can't go stale; the due-at boundary
  is strict (`active`, not `overdue`, exactly at `due_at`).
- **Reports** — `GET /reports/top-books` joins order items through paid orders only,
  groups by book, and sorts by copies sold descending with a title-ascending tie-break.
  The inner join naturally excludes books with zero paid sales, so no extra filtering
  is needed.

## Architectural decisions

- **Reused tier-access logic across services.** `ensure_can_access_restricted` and
  `get_member` live in `app.services.members` and are imported by `orders.py` and
  `loans.py` rather than re-implemented — the restricted-book rule and the member-lookup
  404 only need to be correct in one place.
- **Status is computed, never stored.** Both `Order.status` (persisted, since it has
  real transitions: pending → paid/cancelled) and loan status (derived: `returned` /
  `overdue` / `active`) follow the model in SPEC.md — loan status specifically is never
  written to the database because "now" changes it, so it's computed in
  `loan_status`/`to_loan_out` at read time from `returned_at`/`due_at`.
- **All-or-nothing stock reservation.** Order creation validates every item (existence,
  restriction, then stock) in separate passes before mutating any row, so a `409` on
  item 2 of 2 never leaves item 1's stock decremented. The same pattern (check-then-
  mutate, nothing committed until every check passes) is used for loan creation.
- **Schema-level validation over service-level.** Where a rule is purely about input shape
  (empty items, duplicate book ids, quantity ≥ 1, ISBN checksum), it's enforced in
  Pydantic (`app/schemas.py`) so FastAPI returns 422 automatically before any service code
  or database access runs, keeping routers and services free of that plumbing.

## Trade-offs / things I'd revisit with more time

- Loan/member lookups in `create_loan` load a member's full loan history into Python and
  filter in-memory rather than pushing the overdue/duplicate/tier-limit checks into SQL
  aggregates. Fine at this data volume; would push to `COUNT`/`EXISTS` queries for a
  larger library.
- No handling yet for concurrent orders racing on the last copy of a book (the assignment
  lists this as an optional extra) — SQLite's locking behavior means it's unlikely to
  actually double-sell in this deployment, but nothing enforces it explicitly.
- The optional `GET /members` list endpoint wasn't added — not required by tests or the
  core assignment.

## Spec points I found ambiguous

- None required a judgment call beyond what SPEC.md already states explicitly (e.g. the
  strict `due_at` boundary and the "ceil of partial days" late-fee rule are both spelled
  out precisely enough that the tests and the spec text agree).

## Deployment / database decisions

- **Backend**: Render (free web service), deployed via the `render.yaml` blueprint in the
  repo root. Build: `pip install uv && uv sync --frozen --no-dev`; start:
  `uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Render was chosen over Vercel
  because it's a plain long-running container — no ASGI adapter or serverless cold-start
  handling needed for a stateful SQLAlchemy connection pool.
- **Database**: Supabase Postgres (free tier), since Render's free plan has no persistent
  disk and the app's default SQLite file would reset on every deploy/restart. Connected
  through Supabase's **session pooler** endpoint (`*.pooler.supabase.com`), not the direct
  `db.*.supabase.co` host — the direct host is IPv6-only now and didn't resolve from
  either my machine or Render's build environment; the pooler works over IPv4.
- **Driver**: added `psycopg[binary]` as a dependency and made `SANCTUM_DATABASE_URL` use
  the `postgresql+psycopg://` scheme. `app/db.py`'s `connect_args={"check_same_thread": False}`
  is SQLite-only, so it's now only passed when the URL scheme is `sqlite://` — passing it
  to psycopg would raise. This is the one code change the SQLite→Postgres switch needed;
  everything else (models, queries) was portable as-is.
- The database URL itself is set as a Render environment variable (not in the repo).
  `uv run pytest` still runs entirely against the default local SQLite file, so the "tests
  must pass with no external services" rule holds.

## AI usage

I used Claude Code (Sonnet 5) throughout this assignment, both to scaffold each feature's
implementation against SPEC.md and to walk through Postgres/Supabase/Render deployment
step by step. Concretely:
- Read ASSIGNMENT.md/SPEC.md and the existing partial code, then implemented each service
  module (books, members, orders, loans, reports) one at a time, running the matching
  test file after each change and committing per feature.
- Used it to catch a real bug during implementation: the `tier_at_least` helper's strict
  `>` comparison, which silently broke `master`-tier access to `master`-gated restricted
  books — an easy one to miss by eye since the tests for `master` still passed most other
  restricted-book cases.
- One place it got something wrong and needed correcting: it initially tried the Supabase
  **direct connection** hostname (`db.<ref>.supabase.co`), which failed DNS resolution
  (`getaddrinfo failed`) because that host is IPv6-only on this network. I had to point it
  at the session pooler hostname instead before the connection worked — worth knowing if
  you hit the same error with Supabase.
- I reviewed and understand every line committed; the check-order and all-or-nothing stock
  logic in particular were verified by hand against SPEC.md's specified check sequence,
  not just by the tests passing.
