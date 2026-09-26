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
- **Order status is stored; loan status is derived.** `Order.status` is persisted because
  it has real transitions (pending → paid/cancelled). Loan status is never written to the
  database: it depends on "now", so it's computed at read time from `returned_at`/`due_at`.
  The strict overdue rule lives once, as `Loan.is_overdue`, and is reused by loan status,
  borrowing checks and member stats.
- **All-or-nothing stock reservation.** Order creation validates every item (existence,
  restriction, then stock) before mutating any row, so a `409` on item 2 of 2 never leaves
  item 1's stock decremented. Loan creation follows the same check-then-mutate pattern.
- **Concurrency: pessimistic row locks.** Because the deployed database is Postgres
  (READ COMMITTED), a check-then-decrement is racy without help. Orders load the books
  `FOR UPDATE` in id order (no deadlocks between multi-book orders); pay/cancel lock the
  order row, so stock is restored once; borrowing locks the member (serializing their
  limit/duplicate checks) and the book; returning locks the loan. SQLite ignores
  `FOR UPDATE`, so the local suite can't exercise this — I confirmed the lock is emitted
  in the SQL sent to Postgres, but I did not load-test a real race. Unique-constraint
  races (ISBN, email) are caught as `IntegrityError` and returned as 409, not 500.
- **Database constraints as a backstop.** `books.price_cents` and `books.stock` have CHECK
  constraints, so even a bug can't persist negatives.
- **Where shared rules live.** The tier tables sit beside the code that uses them (as the
  starter laid them out): discounts in `orders`, loan limits in `loans`, tier ranking and the
  restricted-book rule in `members`. `orders` and `loans` import `get_member` /
  `ensure_can_access_restricted` from `members`; the dependency only points one way. A
  dedicated `policies` module would be the next step if the tier rules keep growing.
- **Loan services return `LoanOut`, other services return ORM objects.** A loan's status
  needs "now", so it can't be a plain attribute the way order fields are; the service builds
  the response model instead of leaking the clock into the router.
- **Schema-level validation over service-level.** Where a rule is purely about input shape
  (empty items, duplicate book ids, quantity ≥ 1, ISBN checksum), it's enforced in
  Pydantic (`app/schemas.py`) so FastAPI returns 422 automatically before any service code
  or database access runs, keeping routers and services free of that plumbing.

## Trade-offs / things I'd revisit with more time

- Member stats and the borrowing checks load a member's loans into Python and apply
  `Loan.is_overdue` there (borrowing loads only *unreturned* loans). That keeps the overdue
  rule in one place; at a larger scale I'd push counts into SQL aggregates instead.
- **No migrations.** Tables come from `create_all` at startup, which never alters an
  existing table. The CHECK constraints and the `Loan` columns therefore exist only in
  databases created after those model changes; an already-deployed database needs an
  `ALTER TABLE` (or Alembic, which I'd add next). The deployed Supabase database predates
  the CHECK constraints, so it does not have them yet.
- The locking path is unverified under real concurrency (see above); a proper test would run
  parallel requests against Postgres.
- The optional `GET /members` list endpoint wasn't added.
- The provided test run prints two deprecation warnings from Starlette/anyio internals
  (`httpx`/`BlockingPortal`). They come from pinned dependencies and `tests/conftest.py`,
  neither of which I could change under the ground rules.

## Spec points I found ambiguous

- **`null` in a PATCH body.** SPEC says omitted fields are unchanged but is silent on an
  explicit `null`. I reject it with 422 (a title or price can't be null), rather than
  treating it as "unchanged".
- **`stock` via PATCH.** It's writable, so it can be edited while units are reserved by
  pending orders or out on loan; the spec doesn't say whether that should be allowed. I
  followed the spec literally (any non-negative value).
- **Late fee price.** The spec says the book's price *at return*, so a later price edit
  changes the fee, unlike order line prices which are snapshotted. I implemented it as
  written.
- **Overdue members and the loan limit.** Both "overdue" and "at limit" return 409; the
  spec's check order decides which message a member sees, so overdue wins.
- **Mixed-case title sorting** is explicitly unspecified; I use the database's default
  collation, so SQLite and Postgres may order such titles differently.

## Deployment / database decisions

- **Backend**: Render (free web service), deployed via the `render.yaml` blueprint in the
  repo root. Build: `pip install uv && uv sync --frozen --no-dev`; start:
  `uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Render was chosen over Vercel
  because it's a plain long-running container — no ASGI adapter or serverless cold-start
  handling needed for a stateful SQLAlchemy connection pool.
- **Database**: Supabase Postgres (free tier), since Render's free plan has no persistent
  disk and the app's default SQLite file would reset on every deploy/restart. Connected
  through Supabase's **session pooler** endpoint (`*.pooler.supabase.com`), not the direct
  `db.*.supabase.co` host — the direct host is IPv6-only and failed DNS resolution from my
  IPv4-only machine (`getaddrinfo failed`); the pooler works over IPv4.
- **Free-tier cold starts.** Render's free web service spins down after ~15 minutes without
  traffic, so the first request afterwards can take 30–60 seconds. Data is unaffected: it
  lives in Supabase, not on the instance.
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
