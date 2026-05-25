"""Fase 3 — Cálculo de pesos / Afinidad técnica (FR05).

Asigna un peso de afinidad final en ``[0, 1]`` a cada arista de la proyección
Usuario↔Usuario de la Fase 2 (`GraphBundle.user_graph`). Tres pasos, tal como se
describe en `requirements.md`:

  1. **Similitud coseno** sobre el vector de lenguajes de programación de cada
     usuario (bytes de código por lenguaje, escalados con ``log1p`` para que un
     mega-repo no domine). Como los conteos de bytes son no negativos, el coseno
     cae en ``[0, 1]`` sin necesidad de recorte.
  2. **Solapamiento de Jaccard** del conteo ``shared_repos`` respecto a la unión
     de los repos de ambos usuarios, mapeado a ``[0, 1]``.
  3. **Suma ponderada** ``ALPHA * cosine + (1 - ALPHA) * repo_overlap`` → el
     ``weight`` probabilístico final.

Cada arista se anota en sitio con ``cosine``, ``repo_overlap`` y ``weight`` (la
convención de los skills `graph-algorithms-performance` /
`networkx-graph-modeling`). La Fase 4 aplicará ``-log(weight)`` y correrá
Dijkstra; lee el atributo ``weight`` que esta fase rellena.

El cálculo está totalmente vectorizado con NumPy sobre el conjunto de aristas
— sin bucle Python por arista ni matriz N×N de todos contra todos (solo se
puntúan las aristas que ya produjo la proyección de la Fase 2).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import networkx as nx
import numpy as np

from .graph_builder import GraphBundle

log = logging.getLogger(__name__)

# Weighted-sum coefficient (FR05). Language affinity is the primary "technical"
# signal the project is named for, so it carries slightly more than the
# structural shared-repo overlap. Tunable; documented rather than magic.
DEFAULT_ALPHA = 0.6


@dataclass
class AffinityResult:
    """Resumen de la pasada de scoring de la Fase 3 (los pesos viven en las
    aristas del grafo)."""

    alpha: float
    shared_repos_min: float
    shared_repos_max: float
    edges_scored: int

    def to_dict(self, graph: nx.Graph) -> dict[str, Any]:
        """Serializa el resumen y la lista de aristas ponderadas (con `cosine`,
        `repo_overlap` y `weight` redondeados) más estadísticos de los pesos,
        para la respuesta `verbose` del endpoint."""
        edge_list = [
            {
                "source": u,
                "target": v,
                "cosine": round(d["cosine"], 6),
                "repo_overlap": round(d["repo_overlap"], 6),
                "weight": round(d["weight"], 6),
            }
            for u, v, d in graph.edges(data=True)
        ]
        weights = [e["weight"] for e in edge_list]
        return {
            "alpha": self.alpha,
            "shared_repos_min": self.shared_repos_min,
            "shared_repos_max": self.shared_repos_max,
            "edges": edge_list,
            "stats": {
                "edges_scored": self.edges_scored,
                "weight_mean": round(float(np.mean(weights)), 6) if weights else 0.0,
                "weight_max": round(max(weights), 6) if weights else 0.0,
                "weight_min": round(min(weights), 6) if weights else 0.0,
            },
        }


def score_affinity(bundle: GraphBundle, *, alpha: float = DEFAULT_ALPHA) -> AffinityResult:
    """Calcula y adjunta los pesos de afinidad a las aristas de
    ``bundle.user_graph`` (FR05).

    Muta el grafo en sitio (añade ``cosine``, ``repo_overlap`` y ``weight`` a
    cada arista) y devuelve un resumen :class:`AffinityResult`.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha!r}")

    G = bundle.user_graph
    edges = list(G.edges())

    if not edges:
        return AffinityResult(alpha=alpha, shared_repos_min=0.0, shared_repos_max=0.0, edges_scored=0)

    cosine = _edge_cosine(G, edges)
    repo_overlap, c_min, c_max = _edge_repo_overlap(G, edges)
    weight = alpha * cosine + (1.0 - alpha) * repo_overlap

    for k, (u, v) in enumerate(edges):
        G.edges[u, v].update(
            cosine=float(cosine[k]),
            repo_overlap=float(repo_overlap[k]),
            weight=float(weight[k]),
        )

    log.info(
        "phase3 affinity scored edges=%d alpha=%.2f weight_mean=%.4f",
        len(edges), alpha, float(weight.mean()),
    )
    return AffinityResult(
        alpha=alpha,
        shared_repos_min=float(c_min),
        shared_repos_max=float(c_max),
        edges_scored=len(edges),
    )


def _edge_cosine(G: nx.Graph, edges: list[tuple[str, str]]) -> np.ndarray:
    """Similitud coseno de los vectores de lenguajes de los extremos de cada
    arista (paso 1).

    Construye una matriz ``usuarios × lenguajes`` de conteos de bytes escalados
    con ``log1p``, normaliza las filas en L2 y luego toma el producto punto por
    arista en una única pasada vectorizada. La salida está en ``[0, 1]`` (vectores
    no negativos)."""
    nodes = list(G.nodes())
    node_idx = {n: i for i, n in enumerate(nodes)}
    vocab = sorted({lang for n in nodes for lang in G.nodes[n].get("languages", {})})

    M = np.zeros((len(nodes), len(vocab)), dtype=float)
    if vocab:
        vocab_idx = {lang: j for j, lang in enumerate(vocab)}
        for n in nodes:
            for lang, size in G.nodes[n].get("languages", {}).items():
                M[node_idx[n], vocab_idx[lang]] = math.log1p(max(0, size))

    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0  # zero-vector users stay zero → cosine 0, no NaN
    M_norm = M / norms

    src = np.fromiter((node_idx[u] for u, _ in edges), dtype=int, count=len(edges))
    dst = np.fromiter((node_idx[v] for _, v in edges), dtype=int, count=len(edges))
    cosine = np.sum(M_norm[src] * M_norm[dst], axis=1)
    return np.clip(cosine, 0.0, 1.0)


def _edge_repo_overlap(
    G: nx.Graph, edges: list[tuple[str, str]]
) -> tuple[np.ndarray, float, float]:
    """Solapamiento de Jaccard de los conjuntos de repos de ambos usuarios por
    arista (paso 2).

    ``shared_repos`` es el tamaño de la intersección (el peso de la proyección) y
    el ``repo_count`` de cada nodo es el tamaño de su conjunto de repos, así que
    la unión es ``count_u + count_v - shared`` y Jaccard = ``shared / union`` ∈
    ``[0, 1]``. A diferencia de la normalización min-max, esto conserva la señal
    en el caso común de un único repo compartido (que min-max aplanaría a 0).
    Devuelve ``(jaccard, shared_min, shared_max)`` — el mín/máx crudos se guardan
    para el resumen de la respuesta."""
    jaccard = np.zeros(len(edges), dtype=float)
    shared = np.zeros(len(edges), dtype=float)
    for k, (u, v) in enumerate(edges):
        inter = G.edges[u, v].get("shared_repos", 0)
        union = G.nodes[u].get("repo_count", 0) + G.nodes[v].get("repo_count", 0) - inter
        shared[k] = inter
        jaccard[k] = (inter / union) if union > 0 else 0.0
    return jaccard, float(shared.min()), float(shared.max())
