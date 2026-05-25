"""Fase 4 — Motor de recomendación y filtrado (FR06, FR07, FR08).

Convierte el grafo Usuario↔Usuario ponderado por afinidad de la Fase 3 en una
lista ordenada de usuarios recomendados de GitHub. Tres pasos, como en
`requirements.md`:

  1. **Transformación logarítmica negativa:** ``log_weight = -log(weight)`` en
     cada arista. Esto invierte la escala (afinidad alta → "costo de distancia"
     bajo), de modo que el camino más corto se vuelve el camino que *maximiza* el
     producto de afinidades — el truco de propagación de confianza. Los pesos se
     recortan primero a un piso pequeño para que una arista con ``weight == 0``
     se convierta en un costo finito grande, no en ``inf``.
  2. **Dijkstra:** ``single_source_dijkstra_path_length`` desde la semilla sobre
     ``log_weight`` da el costo propagado hacia cada usuario alcanzable. La
     afinidad propagada se recupera como ``exp(-distancia)``.
  3. **Filtrado (máscaras booleanas):** se descarta la propia semilla, sus
     colaboradores **directos** (FR06 pide usuarios *indirectos*) y a todos los
     del conjunto Following de la semilla (FR07). Lo que queda se ordena por
     afinidad propagada y se trunca a Top N (FR08), empaquetado con datos de
     perfil.

Ver el skill `graph-algorithms-performance` (§4–6).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .github_client import GitHubGraphQLClient
from .graph_builder import GraphBundle

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 10
# Clip floor for the -log transform: weight ∈ [0, 1], and log(0) = -inf, so a
# zero-affinity edge would otherwise produce an infinite (or NaN) distance.
WEIGHT_FLOOR = 1e-12
# Extra candidates ranked beyond Top-N so that hydration can drop non-User
# accounts (bots/orgs that only reveal their type when looked up) and still
# return a full Top-N.
HYDRATION_BUFFER = 10


@dataclass
class RecommendationResult:
    """Resultado de la Fase 4: la lista Top-N de recomendaciones más los conteos
    de exclusión que alimentan el resumen de la respuesta."""

    seed: str
    top_n: int
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    reachable: int = 0
    excluded_direct: int = 0
    excluded_following: int = 0
    excluded_bots: int = 0
    excluded_non_user: int = 0
    excluded_denied: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serializa las recomendaciones y un bloque `stats` con los conteos de
        alcanzados, recomendados y excluidos por cada criterio."""
        return {
            "seed": self.seed,
            "top_n": self.top_n,
            "recommendations": self.recommendations,
            "stats": {
                "reachable": self.reachable,
                "recommended": len(self.recommendations),
                "excluded_direct": self.excluded_direct,
                "excluded_following": self.excluded_following,
                "excluded_bots": self.excluded_bots,
                "excluded_non_user": self.excluded_non_user,
                "excluded_denied": self.excluded_denied,
            },
        }


def _is_bot(login: str) -> bool:
    """Las cuentas bot de GitHub usan el sufijo de login reservado ``[bot]``
    (p. ej. ``dependabot[bot]``, ``github-actions[bot]``). Nunca son
    recomendaciones útiles, así que se excluyen."""
    return login.endswith("[bot]")


def recommend(
    bundle: GraphBundle,
    users: dict[str, Any],
    *,
    top_n: int = DEFAULT_TOP_N,
    weight_floor: float = WEIGHT_FLOOR,
    denylist: set[str] | None = None,
) -> RecommendationResult:
    """Ordena usuarios indirectos y aún no seguidos por afinidad (FR06–FR08).

    Args:
        bundle: el bundle de grafos de las Fases 2/3; las aristas de
            ``user_graph`` deben llevar el atributo ``weight`` que rellena
            :func:`affinity.score_affinity`.
        users: el mapa ``extraction["users"]`` de la Fase 1, usado solo para
            adjuntar campos de perfil (name/avatar/url) a los resultados (FR08).
        top_n: número máximo de recomendaciones a devolver.
        denylist: logins a excluir siempre (configurados vía
            ``settings.RECOMMENDATION_DENYLIST``); comparados sin distinguir
            mayúsculas.
    """
    denied = {d.lower() for d in (denylist or set())}
    G = bundle.user_graph
    seed = bundle.seed

    _apply_log_transform(G, weight_floor)

    if seed not in G:
        log.info("phase4 seed %s absent from user graph; no recommendations", seed)
        return RecommendationResult(seed=seed, top_n=top_n)

    # Step 2 — Dijkstra over the inverted weights (one call, not per-target).
    distances = nx.single_source_dijkstra_path_length(G, seed, weight="log_weight")

    # Step 3 — exclusion masks. Direct collaborators are excluded because FR06
    # asks for *indirect* users; the seed's Following set is excluded by FR07.
    direct_neighbors = set(G.neighbors(seed))
    following = bundle.seed_following

    candidates: list[tuple[str, float]] = []
    excluded_direct = 0
    excluded_following = 0
    excluded_bots = 0
    excluded_denied = 0
    for node, dist in distances.items():
        if node == seed:
            continue
        if node in denied:
            excluded_denied += 1
            continue
        if node in direct_neighbors:
            excluded_direct += 1
            continue
        if node in following:
            excluded_following += 1
            continue
        if _is_bot(node):
            excluded_bots += 1
            continue
        candidates.append((node, dist))

    # Headline metric: DIRECT structural affinity between the seed and each
    # candidate (link prediction), not path-propagated trust. This avoids the
    # decay/floor artifacts that arise when most edges have near-zero weight
    # because indirect users have no language data. The Dijkstra distance above
    # is kept as a secondary "trust" signal (FR06).
    scored: list[_Scored] = []
    for node, dist in candidates:
        scored.append(_structural_scores(G, seed, direct_neighbors, node, dist))

    max_strength = max((s.strength for s in scored), default=0.0)
    # Rank by weighted structural affinity (`strength`), with raw Adamic–Adar,
    # common neighbors, Jaccard and propagated trust (smaller distance) as
    # successive tiebreakers.
    scored.sort(key=lambda s: (s.strength, s.adamic_adar, s.common, s.jaccard, -s.distance), reverse=True)

    # Build a buffered slice; hydration drops non-User accounts and trims to top_n.
    recommendations = [
        _build_recommendation(s, users, max_strength)
        for s in scored[: top_n + HYDRATION_BUFFER]
    ]

    log.info(
        "phase4 seed=%s reachable=%d recommended=%d excluded_direct=%d "
        "excluded_following=%d excluded_bots=%d excluded_denied=%d",
        seed, len(distances), len(recommendations), excluded_direct,
        excluded_following, excluded_bots, excluded_denied,
    )
    return RecommendationResult(
        seed=seed,
        top_n=top_n,
        recommendations=recommendations,
        reachable=len(distances),
        excluded_direct=excluded_direct,
        excluded_following=excluded_following,
        excluded_bots=excluded_bots,
        excluded_denied=excluded_denied,
    )


def hydrate_recommendations(
    result: RecommendationResult, client: GitHubGraphQLClient
) -> None:
    """Hidrata los perfiles del Top-N y descarta cuentas no-usuario (FR08).

    Los usuarios recomendados suelen ser nodos a 2 saltos, descubiertos solo como
    logins de colaboradores (más allá del horizonte de descarga a profundidad 2),
    así que llegan con `url == None`. Una única llamada GraphQL por lotes resuelve
    sus perfiles **y** su tipo de cuenta: el campo `user(login:)` de GitHub
    resuelve solo cuentas de tipo `User`, así que un login que vuelve sin resolver
    es un Bot / Organización / cuenta eliminada y se excluye (filtrado por tipo).
    Luego la lista se recorta a `top_n`.

    Muta `result` en sitio. Best-effort: si una consulta no llegó a ejecutarse
    (fallo transitorio), el login se conserva en vez de descartarse por error."""
    recs = result.recommendations
    pending = [r for r in recs if not r.get("url")]
    profiles = (
        client.fetch_profiles([r["login"] for r in pending]) if pending else {}
    )

    kept: list[dict[str, Any]] = []
    excluded_non_user = 0
    for rec in recs:
        prof = profiles.get(rec["login"])
        if prof is not None:
            rec["name"] = prof.get("name")
            rec["avatarUrl"] = prof.get("avatarUrl")
            rec["url"] = prof.get("url")

        if rec.get("url"):
            kept.append(rec)            # known/resolved User
        elif prof is not None:
            excluded_non_user += 1      # lookup ran, did not resolve → bot/org/deleted
        else:
            kept.append(rec)            # lookup didn't run → keep, don't guess

    result.recommendations = kept[: result.top_n]
    result.excluded_non_user = excluded_non_user
    if excluded_non_user:
        log.info("phase4 hydration dropped %d non-user account(s)", excluded_non_user)


def _apply_log_transform(G: nx.Graph, weight_floor: float) -> None:
    """Anota cada arista con ``log_weight = -log(clip(weight))`` (paso 1)."""
    for _u, _v, data in G.edges(data=True):
        w = data.get("weight", 0.0)
        w = min(1.0, max(weight_floor, w))
        data["log_weight"] = -math.log(w)


@dataclass
class _Scored:
    """Puntajes de afinidad estructural de un candidato respecto a la semilla."""

    node: str
    distance: float
    strength: float       # weighted Adamic–Adar (headline driver)
    adamic_adar: float    # raw Adamic–Adar
    common: int           # common-neighbor count
    jaccard: float        # Jaccard of neighbor sets


def _structural_scores(
    G: nx.Graph, seed: str, seed_neighbors: set[str], node: str, distance: float
) -> _Scored:
    """Afinidad estructural directa semilla↔nodo vía colaboradores compartidos.

    Puntúa sobre los conjuntos de vecinos de ambos usuarios en el grafo
    Usuario↔Usuario:
      - **strength** (principal) — Adamic–Adar *ponderado*: cada vecino
        compartido ``w`` aporta ``weight(seed,w) · weight(w,node) / log(deg w)``.
        Los pesos de arista de la Fase 3 (coseno + solapamiento de Jaccard de
        repos) discriminan candidatos que comparten los *mismos* colaboradores
        pero conectan con ellos con distinta fuerza — algo que el Adamic–Adar
        crudo no logra.
      - **adamic_adar** — ``Σ 1/log(deg w)`` crudo (premia colaboradores raros).
      - **common** — cantidad de colaboradores compartidos.
      - **jaccard** — ``|∩| / |∪|`` de los conjuntos de vecinos.
    """
    nbrs = set(G.neighbors(node))
    common = seed_neighbors & nbrs
    cn = len(common)
    union = len(seed_neighbors | nbrs)
    jac = cn / union if union else 0.0

    aa = 0.0
    strength = 0.0
    for w in common:
        deg = G.degree(w)
        if deg <= 1:
            continue
        inv_log = 1.0 / math.log(deg)
        aa += inv_log
        w_seed = G.edges[seed, w].get("weight", 0.0)
        w_node = G.edges[w, node].get("weight", 0.0)
        strength += w_seed * w_node * inv_log
    return _Scored(node=node, distance=distance, strength=strength, adamic_adar=aa, common=cn, jaccard=jac)


def _build_recommendation(s: _Scored, users: dict[str, Any], max_strength: float) -> dict[str, Any]:
    """Empaqueta un usuario recomendado (FR08).

    La ``affinity`` principal es el puntaje estructural ponderado normalizado
    contra el candidato más fuerte (la mejor coincidencia ≈ 100%). ``trust`` es
    la probabilidad secundaria propagada por Dijkstra ``exp(-distancia)``
    (FR06)."""
    affinity = (s.strength / max_strength) if max_strength > 0 else 0.0
    trust = math.exp(-s.distance)
    profile = users.get(s.node, {})
    return {
        "login": s.node,
        "name": profile.get("name"),
        "avatarUrl": profile.get("avatarUrl"),
        "url": profile.get("url"),
        "affinity": round(affinity, 6),
        "affinity_pct": round(affinity * 100.0, 2),
        "common_neighbors": s.common,
        "adamic_adar": round(s.adamic_adar, 6),
        "jaccard": round(s.jaccard, 6),
        "trust": round(trust, 6),
        "trust_pct": round(trust * 100.0, 2),
        "distance": round(s.distance, 6),
    }
