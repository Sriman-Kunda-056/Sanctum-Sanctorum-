# Notes

**Live URL:** https://sanctum-sanctorum-ugtq.onrender.com/ — UI at `/`, API at the root
(e.g. `/books`, `/reports/top-books`), Swagger docs at `/docs`. No password: the UI "signs
in" by member id. The database already holds 12 seeded books and these four members:

| Member id | Name | Email | Tier | Good for testing |
|---|---|---|---|---|
| 1 | Wong Li | wong@example.com | supreme | Restricted books allowed, 15% discount, unlimited loans |
| 2 | Christine Palmer | christine@example.com | master | Restricted books allowed, 10% discount, up to 5 loans |
| 3 | Jonathan Pangborn | jonathan@example.com | adept | Restricted books blocked (403), 5% discount, up to 3 loans |
| 4 | Sara Lin | sara@example.com | apprentice | Restricted books blocked, no discount, 1 loan (a second borrow is 409) |

Useful books (ids and stock as seeded; stock changes as people order and borrow):
- **Restricted:** book 3, *Darkhold* (stock 1), and book 11, *Principles of Celestial
  Mechanics* (stock 3). These show the difference between members 1–2 and 3–4.
- **Out of stock:** *Darkhold* has one copy, so a second order or loan of it returns 409.
- **Bulk discount:** book 5, *Meditations on First Principles* (stock 12). Ordering 10 or
  more copies adds 5% on top of the tier discount.

A quick run-through in the UI: sign in as member 4 and borrow a book, then try a second
one (refused: apprentice limit). Sign in as member 2, order *Darkhold*, pay for it, and
check the top-books report.

The first request after a quiet period can take 30–60 seconds (free-tier cold start).

## What's finished

All five areas from ASSIGNMENT.md are implemented and all 202 provided tests pass
(`uv run pytest -q`):

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
  limit/duplicate checks) and the book; returning locks the loan. Single rows are locked
  through the services' `get_*(…, lock=True)` lookups; the multi-row case is
  `orders._lock_books`. Unique-constraint races (ISBN, email) are caught as
  `IntegrityError` and returned as 409, not 500.
  SQLite ignores `FOR UPDATE`, so the provided suite can't exercise this. I checked it
  separately against Supabase Postgres, in a throwaway schema dropped afterwards: ten
  simultaneous orders for the last copy produced one 201 and nine 409s, and the same for
  ten simultaneous borrows. No request reached the database's stock constraint, so the
  locks did the work. That script is a one-off and isn't in the repo.
- **Database constraints as a backstop.** The model declares CHECK constraints on
  `books.price_cents` and `books.stock`, so a database created from these models can't
  hold negatives even if a bug slips past validation. The deployed database predates them
  and doesn't have them yet (see "No migrations" below).
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
- `PATCH /books/{id}` sets `stock` to an absolute value without taking a lock, so an edit
  racing an order can overwrite that order's decrement. Stock adjustments should really be
  relative (`+n`/`-n`) or go through the same row lock.
- The concurrency check above was a one-off script. With more time it would become a
  Postgres-backed test in CI.
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
- **Mixed-case title sorting** is explicitly unspecified; I use the database's default
  collation, so SQLite and Postgres may order such titles differently.

## Deployment / database decisions

- **Backend**: Render (free web service), deployed via the `render.yaml` blueprint in the
  repo root. Build: `pip install uv && uv sync --frozen --no-dev`; start:
  `uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port $PORT` (`--no-sync` stops
  `uv run` from reinstalling the dev dependencies at boot). I chose Render over Vercel
  because it runs the app as an ordinary long-lived process: FastAPI and the frontend it
  serves run unchanged, with no serverless ASGI adapter, and the SQLAlchemy connection
  pool survives between requests.
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
- **Checking that the deployment really uses Postgres.** If `SANCTUM_DATABASE_URL` is
  missing in Render's Environment settings, the app silently falls back to SQLite on
  Render's temporary disk, which is wiped on every deploy and idle restart. That is easy
  to miss, because the seed data looks identical either way. The app now logs
  `Using postgresql database` (or `sqlite`) at startup, visible in Render's logs. From
  outside, a member's `created_at` that changes after a redeploy means the data is not
  persistent.
- The database URL itself is set as a Render environment variable (not in the repo).
  `uv run pytest` still runs entirely against the default local SQLite file, so the "tests
  must pass with no external services" rule holds.

## AI usage

**Tool.** Claude Code in VS Code: Sonnet 5 for the implementation and deployment, and
Opus 5.5 for a later examiner-style review and the clean-up that followed.

**How much and when.** The AI wrote most of the code in `app/`, and the whole thing was
done in one extended session on 26–27 September, not spread across the week. I worked
from a task checklist, gave it the order to work in, made the platform and database
choices (Render, Supabase), set up those accounts myself, and decided what to keep or
cut. One example of cutting: it added an extra edge-case test file, and I had it removed
so the provided suite stays the acceptance criteria.

**What it did.**
- Read SPEC.md and the stubbed services, then implemented books, members, orders, loans
  and reports one at a time, running that area's tests before committing.
- Fixed the `tier_at_least` off-by-one (`>` instead of `>=`), which my task checklist had
  already flagged. The provided `master` restricted-book tests failed on it at the start.
- Walked me through Supabase and Render, including the switch to the psycopg driver and
  the SQLite-only `check_same_thread` argument.
- Reviewed the finished work against the grading rubric, then made the follow-up fixes:
  row locking, CHECK constraints, one shared overdue rule, and the `--no-sync` start
  command.

**Where it was wrong, and how it was caught.**
- **No protection against concurrent orders.** The first version had none. After the
  move to Postgres, its notes still said "SQLite's locking" made overselling unlikely,
  which no longer applied to the deployed app. The review caught this; the fix was
  pessimistic row locks, then the 10-way race run against Postgres described above.
- **Untested claims written as fact.** Its first draft of these notes said the direct
  Supabase host "didn't resolve from Render's build environment", which it had never
  tested; the failure was only ever seen on my machine. The same draft credited it with
  finding the `tier_at_least` bug and said the `master` tests had still passed. All of
  that was false, and all of it is removed.

**How the result was checked.** The 202 provided tests, plus the one-off Postgres run
above: 20 checks across every write flow, and the two races.
