"""Muestreador BFS de profundidad acotada sobre el grafo social de GitHub
(Fase 1 / FR03).

Sigue el skill `bfs-sampling-strategy`:
  - `visited` se actualiza al encolar (sin descargas duplicadas entre caminos).
  - Descarga por capas en paralelo con un pool de hilos acotado.
  - Banderas de truncamiento por nodo, resultados parciales y una razón de
    terminación estructurada.
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
    """Resultado de la extracción BFS: el snapshot normalizado de la vecindad
    de la semilla (usuarios descubiertos y aristas por tipo), que consume la
    Fase 2."""

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
        """Serializa el resultado al dict que consume la Fase 2 (`build_graphs`),
        agrupando las aristas por tipo y agregando un bloque `stats` con los
        conteos."""
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
    contrib_per_repo: int = 15,
    contrib_budget: int = 150,
    max_frontier: int = 50,
) -> BFSResult:
    """Muestrea por BFS alrededor de `seed` hasta profundidad `max_depth` (1 o 2).

    Los repos de cada usuario descargado se enriquecen con sus colaboradores
    públicos vía REST (hasta `contrib_per_repo` por repo, con tope global de
    `contrib_budget` llamadas por petición) — esto es lo que vincula a usuarios
    distintos con un repo compartido, de modo que la proyección de la Fase 2
    pueda conectar la semilla con la red de sus colaboradores.

    Raises:
        GitHubNotFound: solo si la propia semilla no existe (el llamador lo
            mapea a 404).
    """
    seed = seed.lower()
    budget = [contrib_budget]  # mutable cell shared across enrichment calls

    seed_topo = client.fetch_user_topology(
        seed, per_page=per_node_limit, max_repo_pages=2, max_following_pages=2
    )
    if not seed_topo.get("found"):
        raise GitHubNotFound(f"GitHub user '{seed}' not found.")
    _enrich_contributors(seed_topo, client, contrib_per_repo, budget, max_workers)

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
        if len(frontier) > max_frontier:
            log.info("frontier capped %d -> %d at depth %d", len(frontier), max_frontier, depth)
            result.truncated_nodes.append(f"frontier@depth{depth}")
            frontier = frontier[:max_frontier]
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
            _enrich_contributors(topo, client, contrib_per_repo, budget, max_workers)
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
    """Registra la topología de un usuario descargado en `result`. Devuelve el
    conjunto de logins de vecinos recién descubiertos (que se añaden a `visited`
    aquí mismo, al momento de encolar)."""
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
                # Contributor shares this repo → bipartite edge so the Phase 2
                # projection links `collab` to everyone else on `repo_id`.
                result.edges_user_repo.append((collab, repo_id))

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


def _enrich_contributors(
    topo: dict[str, Any],
    client: GitHubGraphQLClient,
    contrib_per_repo: int,
    budget: list[int],
    max_workers: int,
    max_contrib_repos: int = 12,
) -> None:
    """Rellena el campo ``collaborators`` de cada repo con sus colaboradores
    públicos obtenidos vía REST.

    Muta ``topo`` en sitio. Solo se enriquecen los primeros ``max_contrib_repos``
    repos del usuario (ya ordenados por actualización más reciente), de modo que
    ningún usuario agote por sí solo el ``budget`` compartido — una lista de un
    elemento que actúa como contador decreciente y limita el total de llamadas
    REST por petición. Una vez agotado, los repos restantes conservan su lista
    de colaboradores vacía (enriquecimiento parcial)."""
    repos = topo.get("repositories", [])
    if not repos or budget[0] <= 0 or contrib_per_repo <= 0:
        return

    # Submission runs on this thread, so decrementing the budget here is safe.
    pending: dict[Any, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for repo in repos[:max_contrib_repos]:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            fut = pool.submit(
                client.fetch_repo_contributors, repo["nameWithOwner"], contrib_per_repo
            )
            pending[fut] = repo
        for fut in as_completed(pending):
            repo = pending[fut]
            try:
                repo["collaborators"] = fut.result()
            except Exception:
                log.exception("contributor fetch failed for %s", repo.get("nameWithOwner"))
                repo["collaborators"] = []


def _fetch_frontier_parallel(
    frontier: list[str],
    client: GitHubGraphQLClient,
    *,
    per_node_limit: int,
    max_workers: int,
) -> dict[str, dict[str, Any] | None]:
    """Descarga la topología de toda una capa de la frontera en paralelo.

    Devuelve un dict ``login → topología`` (o ``None`` si el usuario no existe o
    su descarga falló). Si GitHub reporta límite de cuota a mitad de la capa,
    cancela las descargas pendientes y propaga ``GitHubRateLimited`` para que el
    llamador devuelva resultados parciales."""
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
