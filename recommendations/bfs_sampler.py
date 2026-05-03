"""Depth-bounded BFS sampler over the GitHub social graph (Phase 1 / FR03).

Follows the `bfs-sampling-strategy` skill:
  - `visited` updated at enqueue time (no duplicate fetches across paths)
  - per-layer parallel fetch with a bounded thread pool
  - per-node truncation flags, partial results, structured termination reason
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .github_client import (
    GitHubGraphQLClient,
    GitHubNotFound,
    GitHubRateLimited,
)

log = logging.getLogger(__name__)


@dataclass
class BFSResult:
    seed: str
    seed_profile: dict[str, Any]
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges_user_repo: list[tuple[str, str]] = field(default_factory=list)
    edges_collaborator: list[tuple[str, str]] = field(default_factory=list)
    edges_following: list[tuple[str, str]] = field(default_factory=list)
    depth_reached: int = 0
    truncated_reason: str | None = None
    truncated_nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "seed_profile": self.seed_profile,
            "users": self.users,
            "edges": {
                "user_repo": self.edges_user_repo,
                "collaborator": self.edges_collaborator,
                "following": self.edges_following,
            },
            "depth_reached": self.depth_reached,
            "truncated_reason": self.truncated_reason,
            "truncated_nodes": self.truncated_nodes,
            "stats": {
                "users": len(self.users),
                "edges_user_repo": len(self.edges_user_repo),
                "edges_collaborator": len(self.edges_collaborator),
                "edges_following": len(self.edges_following),
            },
        }


def sample_topology(
    seed: str,
    client: GitHubGraphQLClient,
    max_depth: int = 2,
    per_node_limit: int = 50,
    max_workers: int = 8,
) -> BFSResult:
    """BFS sample around `seed` to depth `max_depth` (1 or 2).

    Raises:
        GitHubNotFound: only if the seed itself does not exist (caller maps to 404).
    """
    seed = seed.lower()

    seed_topo = client.fetch_user_topology(
        seed, per_page=per_node_limit, max_repo_pages=2, max_following_pages=2
    )
    if not seed_topo.get("found"):
        raise GitHubNotFound(f"GitHub user '{seed}' not found.")

    result = BFSResult(
        seed=seed,
        seed_profile={
            "login": seed_topo["login"],
            "name": seed_topo.get("name"),
            "avatarUrl": seed_topo.get("avatarUrl"),
            "url": seed_topo.get("url"),
        },
    )
    visited: set[str] = {seed}

    next_frontier = _absorb_topology(seed, seed_topo, result, visited, is_seed=True)
    result.depth_reached = 1

    for depth in range(1, max_depth):
        if not next_frontier:
            break
        frontier = list(next_frontier)
        next_frontier = set()

        try:
            fetched = _fetch_frontier_parallel(
                frontier, client, per_node_limit=per_node_limit, max_workers=max_workers
            )
        except GitHubRateLimited:
            result.truncated_reason = "rate_limit"
            break

        for login, topo in fetched.items():
            if topo is None or not topo.get("found"):
                continue
            new_neighbors = _absorb_topology(login, topo, result, visited, is_seed=False)
            # Do not enqueue further: max_depth=2 means depth-1 nodes don't expand further.
            if depth + 1 < max_depth:
                next_frontier |= new_neighbors

        result.depth_reached = depth + 1

    return result


def _absorb_topology(
    login: str,
    topo: dict[str, Any],
    result: BFSResult,
    visited: set[str],
    *,
    is_seed: bool,
) -> set[str]:
    """Record a fetched user's topology into `result`. Returns set of newly
    discovered neighbor logins (added to `visited` here, at enqueue time)."""
    languages: dict[str, int] = {}
    repos: list[str] = []
    for repo in topo.get("repositories", []):
        repo_id = repo["nameWithOwner"]
        repos.append(repo_id)
        result.edges_user_repo.append((login, repo_id))
        for lang, size in repo.get("languages", {}).items():
            languages[lang] = languages.get(lang, 0) + size
        for collab in repo.get("collaborators", []):
            if collab and collab != login:
                result.edges_collaborator.append((login, collab))

    following = list(topo.get("following", []))
    for tgt in following:
        if tgt and tgt != login:
            result.edges_following.append((login, tgt))

    result.users[login] = {
        "login": topo.get("login", login),
        "name": topo.get("name"),
        "avatarUrl": topo.get("avatarUrl"),
        "url": topo.get("url"),
        "languages": languages,
        "repos": repos,
        "following": following,
        "is_seed": is_seed,
        "truncated": topo.get("truncated", {}),
    }

    truncated = topo.get("truncated") or {}
    if truncated.get("repositories") or truncated.get("following"):
        result.truncated_nodes.append(login)

    new: set[str] = set()
    for repo in topo.get("repositories", []):
        for collab in repo.get("collaborators", []):
            if collab and collab not in visited:
                visited.add(collab)
                new.add(collab)
    for tgt in following:
        if tgt and tgt not in visited:
            visited.add(tgt)
            new.add(tgt)
    return new


def _fetch_frontier_parallel(
    frontier: list[str],
    client: GitHubGraphQLClient,
    *,
    per_node_limit: int,
    max_workers: int,
) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                client.fetch_user_topology,
                login,
                per_node_limit,
                1,  # depth-1 nodes: single page each, no need to paginate further
                1,
            ): login
            for login in frontier
        }
        for fut in as_completed(futures):
            login = futures[fut]
            try:
                out[login] = fut.result()
            except GitHubNotFound:
                out[login] = None
            except GitHubRateLimited:
                # Bubble up to caller — partial results below this point.
                for f in futures:
                    f.cancel()
                raise
            except Exception:
                log.exception("frontier fetch failed for %s", login)
                out[login] = None
    return out
