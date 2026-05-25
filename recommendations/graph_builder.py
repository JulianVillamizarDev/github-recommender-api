"""Fase 2 — Modelado del grafo (FR04).

Transforma el snapshot de extracción de la Fase 1 (`BFSResult.to_dict()`) en la
topología NetworkX en memoria que consumen las Fases 3–4:

  1. **Grafo bipartito Usuario↔Repositorio** (`nx.Graph`, con atributo de nodo
     `bipartite`).
  2. **Proyección a un grafo ponderado Usuario↔Usuario** — un `nx.Graph` no
     dirigido donde existe una arista sii dos usuarios comparten ≥ 1 repositorio;
     la arista guarda el conteo crudo `shared_repos`.
  3. **Conjunto Following** — la lista `following` de la semilla, conservada como
     un `set[str]` para el filtro de exclusión O(1) de la Fase 4 (FR07), más el
     grafo dirigido `following` completo por completitud.

La ponderación de afinidad (similitud coseno, normalización, suma ponderada) es
de la **Fase 3** y deliberadamente no se hace aquí: la proyección guarda solo el
conteo crudo de repos compartidos para que la Fase 3 calcule el `weight` final
sin volver a proyectar. Ver los skills `networkx-graph-modeling` y
`graph-algorithms-performance`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

log = logging.getLogger(__name__)

# Repos with more contributors than this are dropped before projection.
# Projection cost is O(Σ deg(repo)²), so one mega-repo (e.g. a popular OSS
# project with thousands of contributors) dominates runtime while adding little
# affinity signal — everyone "shares" it, so it discriminates nothing.
DEFAULT_MAX_REPO_DEGREE = 100


@dataclass
class GraphBundle:
    """Contenedor de los grafos de la Fase 2, espejando la forma de `BFSResult`."""

    seed: str
    bipartite: nx.Graph
    user_graph: nx.Graph
    following: nx.DiGraph
    seed_following: set[str]
    dropped_repos: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializa los grafos a un dict apto para JSON (conteos, lista de
        aristas del grafo Usuario↔Usuario y `stats`), para la respuesta `verbose`
        del endpoint."""
        users = [n for n, d in self.bipartite.nodes(data=True) if d.get("bipartite") == 0]
        repos = [n for n, d in self.bipartite.nodes(data=True) if d.get("bipartite") == 1]
        return {
            "seed": self.seed,
            "bipartite": {
                "users": len(users),
                "repos": len(repos),
                "edges": self.bipartite.number_of_edges(),
            },
            "user_graph": {
                "nodes": self.user_graph.number_of_nodes(),
                "edges": self.user_graph.number_of_edges(),
                "edge_list": [
                    {"source": u, "target": v, "shared_repos": data["shared_repos"]}
                    for u, v, data in self.user_graph.edges(data=True)
                ],
            },
            "following": {
                "edges": self.following.number_of_edges(),
                "seed_following_count": len(self.seed_following),
            },
            "seed_following": sorted(self.seed_following),
            "dropped_repos": self.dropped_repos,
            "stats": {
                "user_nodes": self.user_graph.number_of_nodes(),
                "user_edges": self.user_graph.number_of_edges(),
                "dropped_repos": len(self.dropped_repos),
            },
        }


def build_graphs(
    extraction: dict[str, Any],
    *,
    max_repo_degree: int = DEFAULT_MAX_REPO_DEGREE,
) -> GraphBundle:
    """Construye los grafos de la Fase 2 a partir del dict de extracción de la
    Fase 1 (FR04).

    Args:
        extraction: el payload ``extraction`` producido por
            ``BFSResult.to_dict()`` — claves ``seed``, ``users``, ``edges``.
        max_repo_degree: los repos con más aristas de colaborador que este umbral
            se descartan antes de proyectar (guarda contra mega-repos).
    """
    seed = extraction["seed"]
    users: dict[str, Any] = extraction.get("users", {})
    edges = extraction.get("edges", {})
    user_repo_edges = edges.get("user_repo", [])
    following_edges = edges.get("following", [])

    bipartite = _build_bipartite(users, user_repo_edges)
    dropped = _drop_mega_repos(bipartite, max_repo_degree)
    user_graph = _project_users(bipartite, users)
    following = _build_following(following_edges)
    seed_following = set(users.get(seed, {}).get("following", []))

    log.info(
        "phase2 graph built seed=%s users=%d user_edges=%d dropped_repos=%d following=%d",
        seed,
        user_graph.number_of_nodes(),
        user_graph.number_of_edges(),
        len(dropped),
        len(seed_following),
    )

    return GraphBundle(
        seed=seed,
        bipartite=bipartite,
        user_graph=user_graph,
        following=following,
        seed_following=seed_following,
        dropped_repos=dropped,
    )


def _build_bipartite(
    users: dict[str, Any],
    user_repo_edges: list[Any],
) -> nx.Graph:
    """Grafo bipartito Usuario↔Repositorio. Los usuarios conservan su mapa
    `languages` como atributo de nodo para que la Fase 3 pueda vectorizar sin
    releer la extracción."""
    B = nx.Graph()
    for login, node in users.items():
        B.add_node(
            login,
            bipartite=0,
            kind="user",
            languages=node.get("languages", {}),
            is_seed=node.get("is_seed", False),
        )
    for u, repo in user_repo_edges:
        # The seed's repos appear under the seed login; depth-1 nodes only
        # contribute repos GitHub returned for them. Add repo nodes lazily.
        if repo not in B:
            B.add_node(repo, bipartite=1, kind="repo")
        if u not in B:
            # A collaborator surfaced via a repo edge but absent from `users`
            # (e.g. truncated frontier) — still a valid user node.
            B.add_node(u, bipartite=0, kind="user", languages={}, is_seed=False)
        B.add_edge(u, repo)
    return B


def _drop_mega_repos(B: nx.Graph, max_repo_degree: int) -> list[str]:
    """Elimina los nodos de repo cuyo grado de colaboradores supera el umbral.
    Devuelve los ids de repos descartados (se reportan al llamador por
    transparencia)."""
    dropped = [
        n
        for n, d in B.nodes(data=True)
        if d.get("bipartite") == 1 and B.degree(n) > max_repo_degree
    ]
    B.remove_nodes_from(dropped)
    return dropped


def _project_users(B: nx.Graph, users: dict[str, Any]) -> nx.Graph:
    """Proyecta el grafo bipartito sobre sus nodos de usuario.

    El peso de arista de ``weighted_projected_graph`` es el número de repos
    compartidos; lo copiamos a ``shared_repos`` y dejamos ``weight`` para que la
    Fase 3 lo rellene con el puntaje de afinidad final, de modo que Dijkstra lea
    un atributo sin ambigüedad.
    """
    user_nodes = [n for n, d in B.nodes(data=True) if d.get("bipartite") == 0]
    G = nx.bipartite.weighted_projected_graph(B, user_nodes)
    for u, v, data in G.edges(data=True):
        data["shared_repos"] = data.pop("weight", 0)
    # Carry the language vectors + repo count onto the projected nodes. Phase 3
    # uses `repo_count` (= number of repos this user is on, i.e. bipartite
    # degree) to turn the shared-repo intersection into a Jaccard overlap.
    for n in G.nodes:
        G.nodes[n]["languages"] = B.nodes[n].get("languages", {})
        G.nodes[n]["is_seed"] = B.nodes[n].get("is_seed", False)
        G.nodes[n]["repo_count"] = B.degree(n)
    return G


def _build_following(following_edges: list[Any]) -> nx.DiGraph:
    """Grafo dirigido de seguimiento (relación asimétrica, FR07)."""
    F = nx.DiGraph()
    F.add_edges_from((u, t) for u, t in following_edges)
    return F
