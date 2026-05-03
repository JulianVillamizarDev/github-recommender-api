"""GitHub GraphQL client used by Phase 1 extraction.

Implements the practices from the `github-graphql-client` and
`api-rate-limit-resilience` skills: bearer auth from env, named queries
with variables, pageInfo cursor pagination, retry with jitter, rate-limit
inspection, and response caching via Django's cache framework.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx
from django.core.cache import cache

log = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
USER_AGENT = "github-recommendation/0.1 (academic-project)"

USER_TOPOLOGY_QUERY = """
query UserTopology(
    $login: String!,
    $repoFirst: Int!,
    $repoAfter: String,
    $followingFirst: Int!,
    $followingAfter: String,
    $collabFirst: Int!
) {
    rateLimit { limit cost remaining resetAt }
    user(login: $login) {
        login
        name
        avatarUrl
        url
        repositories(
            first: $repoFirst,
            after: $repoAfter,
            ownerAffiliations: [OWNER, COLLABORATOR],
            isFork: false,
            orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
            pageInfo { hasNextPage endCursor }
            nodes {
                nameWithOwner
                isPrivate
                primaryLanguage { name }
                languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
                    edges { size node { name } }
                }
                collaborators(first: $collabFirst, affiliation: ALL) {
                    nodes { login }
                }
            }
        }
        following(first: $followingFirst, after: $followingAfter) {
            pageInfo { hasNextPage endCursor }
            nodes { login }
        }
    }
}
"""

USER_EXISTS_QUERY = """
query UserExists($login: String!) {
    rateLimit { remaining resetAt }
    user(login: $login) { login }
}
"""


class GitHubAuthError(RuntimeError):
    pass


class GitHubNotFound(RuntimeError):
    pass


class GitHubRateLimited(RuntimeError):
    def __init__(self, reset_at: int | None = None) -> None:
        super().__init__("GitHub rate limit exceeded.")
        self.reset_at = reset_at


class GitHubUpstreamError(RuntimeError):
    pass


@dataclass
class RateLimit:
    remaining: int | None
    reset_at: str | None
    cost: int | None = None


class GitHubGraphQLClient:
    """Thin sync wrapper around the GitHub GraphQL endpoint."""

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 20.0,
        max_attempts: int = 4,
    ) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise GitHubAuthError(
                "GITHUB_TOKEN is not set. Add it to your environment or .env file."
            )
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=5.0),
            headers={
                "Authorization": f"bearer {self.token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            http2=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubGraphQLClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                resp = self._client.post(
                    GITHUB_GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                payload = resp.json()
                self._record_rate_limit(payload)
                if "errors" in payload and payload["errors"]:
                    self._raise_for_graphql_errors(payload["errors"])
                return payload["data"]

            if resp.status_code == 401:
                raise GitHubAuthError("GitHub rejected the token (401).")
            if resp.status_code == 403:
                self._handle_403(resp, attempt)
                continue
            if resp.status_code == 404:
                raise GitHubNotFound("Resource not found (404).")
            if resp.status_code in (502, 503, 504):
                self._sleep_backoff(attempt)
                continue
            raise GitHubUpstreamError(
                f"Unexpected GitHub status {resp.status_code}: {resp.text[:200]}"
            )

        raise GitHubUpstreamError(
            f"GitHub request failed after {self.max_attempts} attempts: {last_exc!r}"
        )

    def _handle_403(self, resp: httpx.Response, attempt: int) -> None:
        retry_after = resp.headers.get("Retry-After")
        reset = resp.headers.get("x-ratelimit-reset")
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining == "0" and reset:
            raise GitHubRateLimited(reset_at=int(reset))
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 60.0))
                return
            except ValueError:
                pass
        self._sleep_backoff(attempt)

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(60.0, random.uniform(0.0, 2.0 ** attempt))
        time.sleep(delay)

    @staticmethod
    def _raise_for_graphql_errors(errors: list[dict[str, Any]]) -> None:
        codes = {e.get("type") for e in errors}
        if "NOT_FOUND" in codes:
            raise GitHubNotFound("; ".join(e.get("message", "") for e in errors))
        if "RATE_LIMITED" in codes:
            raise GitHubRateLimited()
        raise GitHubUpstreamError(
            "GraphQL error: " + "; ".join(e.get("message", "") for e in errors)
        )

    @staticmethod
    def _record_rate_limit(payload: dict[str, Any]) -> None:
        rl = (payload.get("data") or {}).get("rateLimit") or {}
        if rl:
            log.info(
                "github.ratelimit remaining=%s cost=%s reset_at=%s",
                rl.get("remaining"), rl.get("cost"), rl.get("resetAt"),
            )

    # ---- Public API used by views / sampler ---------------------------------

    def user_exists(self, login: str) -> bool:
        cache_key = f"gh:user_exists:{login}"
        cached = cache.get(cache_key)
        if cached is not None:
            return bool(cached)
        try:
            data = self._post(USER_EXISTS_QUERY, {"login": login})
            exists = bool(data.get("user"))
        except GitHubNotFound:
            exists = False
        cache.set(cache_key, exists, timeout=60)
        return exists

    def fetch_user_topology(
        self,
        login: str,
        per_page: int = 50,
        max_repo_pages: int = 2,
        max_following_pages: int = 2,
        collab_first: int = 20,
    ) -> dict[str, Any]:
        """Fetch one user's topology (repos+languages+collaborators+following).

        Returns a normalized dict; never raises GitHubNotFound for sub-frontier
        users (returns an empty record so BFS can continue). Raises for the
        seed caller's first call when the seed itself is missing.
        """
        cache_key = f"gh:topology:{login}:{per_page}:{max_repo_pages}:{max_following_pages}:{collab_first}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        repos: list[dict[str, Any]] = []
        following: list[str] = []
        truncated = {"repositories": False, "following": False}
        repo_after: str | None = None
        following_after: str | None = None
        user_meta: dict[str, Any] | None = None

        for page in range(max(max_repo_pages, max_following_pages)):
            variables = {
                "login": login,
                "repoFirst": per_page,
                "repoAfter": repo_after,
                "followingFirst": per_page,
                "followingAfter": following_after,
                "collabFirst": collab_first,
            }
            try:
                data = self._post(USER_TOPOLOGY_QUERY, variables)
            except GitHubNotFound:
                return {
                    "login": login,
                    "found": False,
                    "repositories": [],
                    "following": [],
                    "truncated": truncated,
                }

            user = data.get("user")
            if not user:
                return {
                    "login": login,
                    "found": False,
                    "repositories": [],
                    "following": [],
                    "truncated": truncated,
                }
            if user_meta is None:
                user_meta = {
                    "login": user["login"],
                    "name": user.get("name"),
                    "avatarUrl": user.get("avatarUrl"),
                    "url": user.get("url"),
                }

            repo_conn = user["repositories"]
            if page < max_repo_pages:
                repos.extend(_normalize_repos(repo_conn["nodes"]))
                if repo_conn["pageInfo"]["hasNextPage"] and page + 1 < max_repo_pages:
                    repo_after = repo_conn["pageInfo"]["endCursor"]
                else:
                    if repo_conn["pageInfo"]["hasNextPage"]:
                        truncated["repositories"] = True
                    repo_after = None

            follow_conn = user["following"]
            if page < max_following_pages:
                following.extend(n["login"].lower() for n in follow_conn["nodes"])
                if follow_conn["pageInfo"]["hasNextPage"] and page + 1 < max_following_pages:
                    following_after = follow_conn["pageInfo"]["endCursor"]
                else:
                    if follow_conn["pageInfo"]["hasNextPage"]:
                        truncated["following"] = True
                    following_after = None

            if repo_after is None and following_after is None:
                break

        result = {
            **(user_meta or {"login": login}),
            "found": user_meta is not None,
            "repositories": repos,
            "following": following,
            "truncated": truncated,
        }
        cache.set(cache_key, result, timeout=30 * 60)
        return result


def _normalize_repos(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        primary = (n.get("primaryLanguage") or {}).get("name")
        lang_edges = ((n.get("languages") or {}).get("edges") or [])
        languages = {
            e["node"]["name"]: int(e["size"])
            for e in lang_edges
            if e.get("node") and e.get("node", {}).get("name")
        }
        collaborators = [
            c["login"].lower()
            for c in ((n.get("collaborators") or {}).get("nodes") or [])
            if c and c.get("login")
        ]
        out.append({
            "nameWithOwner": n["nameWithOwner"],
            "isPrivate": n.get("isPrivate", False),
            "primaryLanguage": primary,
            "languages": languages,
            "collaborators": collaborators,
        })
    return out
