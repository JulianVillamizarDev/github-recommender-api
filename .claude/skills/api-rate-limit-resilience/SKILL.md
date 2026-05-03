---
name: api-rate-limit-resilience
description: Use when calling rate-limited third-party HTTP APIs — retry/backoff with jitter, request coalescing, response caching, circuit breakers, concurrency bounding, and budget-aware fan-out. Activates on imports of `httpx`, `requests`, `aiohttp`, `tenacity`, or when the user mentions rate limit, 429, retry, backoff, throttle, or external API resilience. Especially relevant for the GitHub GraphQL fan-out at depth 2 in this project.
---

# API Rate-Limit Resilience — Practices

Applies to Phase 1 of `requirements.md` (BFS depth-2 sampling fans out fast against GitHub's 5000 pts/hr GraphQL budget).

## 1. Bound concurrency

- Never fire unbounded `gather(*tasks)`. Use `asyncio.Semaphore(N)` (start `N=8`) so at most N requests are in flight.
- For sync code, use a thread pool with explicit `max_workers`.
- Concurrency × per-request cost = burst rate. A burst above ~80 req/min on GitHub triggers secondary abuse limits even when the primary budget has room.

## 2. Retry with backoff + jitter

- Retry only **idempotent** requests (GET / GraphQL queries). Never auto-retry mutations.
- Retry on: HTTP 429, 502, 503, 504, network timeouts, `httpx.RemoteProtocolError`. **Do not** retry 4xx other than 429 — they are deterministic failures.
- Backoff: exponential with full jitter — `sleep = random.uniform(0, base * 2 ** attempt)`. Cap at e.g. 60s. Max 3–5 attempts.
- Honor `Retry-After` (seconds or HTTP-date) and `x-ratelimit-reset` (Unix epoch) over your own backoff math when present.
- `tenacity` is fine for the boilerplate: `@retry(wait=wait_random_exponential(max=60), stop=stop_after_attempt(4), retry=retry_if_exception_type(...))`.

## 3. Cache outbound responses

- Key on `(endpoint, normalized_args)`. Hash long arg sets to keep keys short.
- TTL by data volatility:
  - User profile: 1 h
  - User's repo list: 30 min
  - Following list: 30 min
  - Repo language stats: 6–24 h (rarely change)
- Use Redis in production (shared across workers). `LocMemCache` only in single-process dev.
- Treat cache as authoritative within TTL — don't double-fetch to "verify."

## 4. Request coalescing (single-flight)

- During BFS, the same user often appears via multiple paths. Without coalescing you'll fetch them N times concurrently before any cache write.
- Pattern: an in-memory `dict[key, asyncio.Future]`. First caller creates the Future and starts the fetch; subsequent callers `await` the same Future. On completion, populate cache and resolve all waiters.
- Libraries: `aiocache` with `cached_stampede`, or hand-roll ~20 lines.

## 5. Budget-aware fan-out

- Read the rate-limit response (GitHub: `rateLimit { remaining resetAt }`) and short-circuit BFS when `remaining < threshold`. Surface partial results rather than failing.
- Track a per-request budget: e.g. "this BFS may spend at most 500 points." When exceeded, stop expanding new frontier nodes; return what you have with a `truncated: true` flag in the response.
- Log every request's cost and remaining budget — without this you cannot diagnose slowdowns.

## 6. Circuit breaker

- After N consecutive failures (e.g. 5) to the upstream, **open the circuit**: fail fast for `cooldown` seconds (30–60s) without making real requests.
- Half-open after cooldown: allow one probe; close on success, re-open on failure.
- Critical for protecting your own server when GitHub has an incident — without it, every incoming request piles onto a dying upstream.
- `pybreaker` or a 30-line implementation is enough.

## 7. Timeouts

- **Always** set explicit timeouts. `httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)`.
- A request without a timeout will eventually exhaust your worker pool when the upstream stalls.
- Total request budget for the user-facing endpoint should be < 30s; offload anything longer to a background job.

## 8. Observability

- Log structured fields per outbound call: `endpoint`, `latency_ms`, `status`, `attempt`, `cache_hit`, `rate_limit_remaining`. Without these, post-mortems are guesswork.
- Emit metrics: outbound request count, p50/p95 latency, error rate, cache hit ratio, breaker state.

## 9. Anti-patterns to flag

- `time.sleep(60)` after a 429 — fixed backoff with no jitter causes thundering-herd retries across workers.
- Catch-all `except Exception: retry` — retries deterministic failures forever.
- `asyncio.gather(*[fetch(u) for u in 10000_users])` with no semaphore.
- No timeout on `requests.get` / `httpx.get`.
- Cache key includes a timestamp or auth token (cache hit rate ≈ 0).
- Retrying mutations (POST that creates resources) without idempotency keys.
- Treating cache as a fallback only on error — defeats the rate-limit goal; cache should be the **first** lookup.
- No upper bound on retries — eventually saturates worker threads.
