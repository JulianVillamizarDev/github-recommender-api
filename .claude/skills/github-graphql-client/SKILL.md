---
name: github-graphql-client
description: Use when writing or reviewing code that calls the GitHub GraphQL API (api.github.com/graphql) — query construction, authentication, pagination, rate-limit handling, partial-response handling, error parsing, or response caching. Activates on imports of `requests`, `httpx`, `gql`, `aiohttp` paired with GitHub URLs/tokens, on files named `github_*.py` / `gh_*.py`, or when the user mentions GitHub's REST/GraphQL API, repositories, followers, collaborators, or `GITHUB_TOKEN`.
---

# GitHub GraphQL Client — Practices

Applies to any code that talks to `https://api.github.com/graphql` (FR02, FR03, Phase 1 in `requirements.md`).

## 1. Authentication

- Use a fine-scoped Personal Access Token (PAT) or GitHub App token. For this project: `read:user`, `public_repo` is enough.
- Read the token from env (`GITHUB_TOKEN`); never commit it. `.env` + `.env.example`.
- Header: `Authorization: bearer <token>` (lowercase `bearer`).
- Always send `User-Agent: github-recommendation/<version> (<contact>)` — GitHub rejects requests without a UA.

## 2. Query construction

- Use a single, nested query per request to retrieve user → repos → languages → collaborators → following in one round-trip. Each request costs ≥1 against the rate limit; minimize requests, not response size.
- Request only the fields you need. GraphQL bills point cost based on connections traversed, not bytes.
- Use **named operations** and **variables** (`query UserTopology($login: String!, $first: Int!)`) — never string-interpolate user input into the query.
- Use **fragments** for reused selection sets (e.g. `UserCore { login avatarUrl url }`).
- Use **aliases** when fetching multiple connections of the same type at different page positions.

## 3. Pagination

- Every connection (`repositories`, `followers`, `following`, `collaborators`) is cursor-paginated with `pageInfo { hasNextPage endCursor }`.
- Default to `first: 100` (the GitHub max). Loop until `hasNextPage == false`, passing `after: endCursor`.
- For BFS depth ≤ 2 (FR06, Phase 1), cap pagination per node — e.g. first 100 repos, first 100 follows — and record truncation rather than fetching unbounded pages.

## 4. Rate-limit handling

GitHub GraphQL has a **point budget** (5000 pts/hr for PAT) plus secondary abuse limits.

- Inspect `rateLimit { limit cost remaining resetAt }` in every response and log it. Stop or back off at < 100 remaining.
- On HTTP 403 with `x-ratelimit-remaining: 0`: sleep until `x-ratelimit-reset` (Unix timestamp).
- On HTTP 403 with `Retry-After`: honor the header.
- On HTTP 502/503/504: retry with exponential backoff + jitter (e.g. `2 ** attempt + random()`), max 3 attempts.
- Never busy-loop on 403 — that triggers secondary abuse rate limits and can ban the token.

## 5. Error handling

- GraphQL returns HTTP 200 with an `errors` array even on field failures. Always check `response["errors"]` before `response["data"]`.
- Common error types: `NOT_FOUND` (FR02 — surface as 404 to client), `RATE_LIMITED`, `FORBIDDEN`, `INTERNAL`.
- Partial data: `data` may be present alongside `errors`. Decide per-field whether to accept partials (often yes for repo lists) or fail fast (always fail on missing seed user).

## 6. Caching

- Cache the raw GraphQL response keyed on `(operation_name, variables_hash)` with a TTL (e.g. 10 min for user topology, 1 hr for repo metadata).
- For BFS, dedupe in-flight requests: if two paths reach the same user node concurrently, share one fetch (request coalescing).
- Consider an ETag-aware persistent cache for long-running pipelines.

## 7. Recommended client setup

- Sync: `httpx.Client(http2=True, timeout=20.0)` reuses TCP/TLS — much faster than fresh `requests.post` per call.
- Async (recommended for BFS fan-out): `httpx.AsyncClient` + `asyncio.Semaphore(8)` to bound concurrency.
- Or use `gql` (`gql[httpx]`) for typed schema validation if the project schema is stable.

## 8. Anti-patterns to flag

- Token in source, in a settings file, or printed in logs.
- Building queries via f-strings or `%` from user input.
- Looping `requests.post(...)` per user without a session/client (no connection reuse).
- Ignoring `rateLimit` in the response and only reacting to 403s.
- Fetching all pages unconditionally on a high-fanout connection (`stargazers`, `followers` of a popular user can be 100k+).
- Treating `data` as authoritative without inspecting `errors`.
- Sleeping on a fixed interval after 403 instead of using `resetAt` / `Retry-After`.
