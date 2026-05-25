"""Endpoint de la API que orquesta el pipeline de recomendación (FR01–FR08).

Ejecuta las cuatro fases en secuencia (extracción BFS → grafo → afinidad →
recomendación) y mapea las excepciones del cliente de GitHub a códigos HTTP."""

from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .affinity import score_affinity
from .bfs_sampler import sample_topology
from .graph_builder import build_graphs
from .recommender import hydrate_recommendations, recommend
from .github_client import (
    GitHubAuthError,
    GitHubGraphQLClient,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubUpstreamError,
)
from .serializers import (
    ErrorResponseSerializer,
    RecommendRequestSerializer,
    RecommendResponseSerializer,
)

log = logging.getLogger(__name__)


@extend_schema(
    tags=["Recomendaciones"],
    summary="Extraer topología de GitHub alrededor de un usuario semilla",
    description=(
        "Implementa la **Fase 1 — Extracción de Datos** del pipeline de "
        "recomendación.\n\n"
        "**Pasos que ejecuta este endpoint:**\n"
        "1. **FR01 — Recepción:** valida el cuerpo JSON con el campo `username`.\n"
        "2. **FR02 — Validación de entrada:** verifica el formato del nombre "
        "de usuario y consulta la API pública de GitHub para confirmar que "
        "el perfil existe; si no existe, devuelve `404`.\n"
        "3. **FR03 — Extracción de topología:** ejecuta un muestreo BFS de "
        "profundidad ≤ 2 sobre el grafo social de GitHub para recolectar:\n"
        "   - Repositorios del usuario (no fork) con sus lenguajes y tamaño en bytes.\n"
        "   - Colaboradores directos por repositorio.\n"
        "   - Lista de usuarios seguidos (relación dirigida).\n\n"
        "El resultado es un *snapshot* normalizado de la vecindad de la "
        "semilla, listo para ser consumido por las fases posteriores "
        "(construcción del grafo bipartito, cálculo de afinidad y Dijkstra).\n\n"
        "**Notas operativas:**\n"
        "- El endpoint es público (sin autenticación) pero está limitado por "
        "throttling anónimo configurable en `settings.py`.\n"
        "- Las respuestas se cachean en memoria por 30 minutos por usuario "
        "para evitar consumir cuota de GitHub innecesariamente.\n"
        "- Si la cuota GraphQL se agota a mitad del recorrido, se devuelve "
        "una respuesta parcial con `truncated_reason = 'rate_limit'` en "
        "lugar de un error `429`."
    ),
    request=RecommendRequestSerializer,
    responses={
        200: OpenApiResponse(
            response=RecommendResponseSerializer,
            description="Extracción exitosa. Devuelve la topología muestreada.",
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Cuerpo inválido (formato de `username` incorrecto, etc.).",
        ),
        404: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="El usuario de GitHub no existe (FR02).",
        ),
        429: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Cuota de GitHub agotada o throttle local activo.",
        ),
        500: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Servidor mal configurado (token de GitHub ausente).",
        ),
        502: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Error inesperado en la API de GitHub.",
        ),
    },
    examples=[
        OpenApiExample(
            "Petición mínima",
            value={"username": "octocat"},
            request_only=True,
            description="Profundidad y límite por nodo toman sus valores por defecto.",
        ),
        OpenApiExample(
            "Petición con parámetros",
            value={"username": "torvalds", "max_depth": 2, "per_node_limit": 25},
            request_only=True,
            description="Reduce el límite por nodo para acelerar el muestreo.",
        ),
        OpenApiExample(
            "Respuesta de error 404",
            value={
                "error": {
                    "code": "user_not_found",
                    "message": "GitHub user 'noexiste' not found.",
                    "details": {"username": "noexiste"},
                }
            },
            response_only=True,
            status_codes=["404"],
        ),
    ],
)
class RecommendView(APIView):
    """POST /api/recommend — ejecuta el pipeline completo sobre un usuario semilla.

    FR01: recibe `username` del cuerpo JSON.
    FR02: valida formato (regex) y existencia (consulta a la API de GitHub).
    FR03: extrae repos, lenguajes, colaboradores y following (BFS de profundidad ≤ 2).
    FR04–FR08: construye el grafo, calcula la afinidad y rankea las recomendaciones.

    Endpoint público (`AllowAny`) con throttling anónimo.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request, *args, **kwargs):
        """Atiende la petición ejecutando las cuatro fases del pipeline.

        Valida el cuerpo, confirma que la semilla existe (404 si no), corre el
        muestreo BFS y las Fases 2–4, y devuelve una respuesta liviana
        (recomendaciones + resumen), o el volcado completo por fase si se pide
        `verbose`. Las excepciones del cliente de GitHub se mapean a 404/429/500/502.
        """
        ser = RecommendRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        username = ser.validated_data["username"]
        max_depth = ser.validated_data["max_depth"]
        per_node_limit = ser.validated_data["per_node_limit"]
        top_n = ser.validated_data["top_n"]
        verbose = ser.validated_data["verbose"]

        try:
            with GitHubGraphQLClient() as client:
                if not client.user_exists(username):
                    raise NotFound(_error("user_not_found", f"GitHub user '{username}' not found.", username=username))

                # Phase 1 (FR01–FR03): BFS topology extraction around the seed.
                bfs = sample_topology(
                    username,
                    client=client,
                    max_depth=max_depth,
                    per_node_limit=per_node_limit,
                )
                extraction = bfs.to_dict()

                # Phase 2 (FR04): build the in-memory NetworkX topology —
                # bipartite graph, User↔User projection, seed Following set.
                bundle = build_graphs(extraction)

                # Phase 3 (FR05): score each User↔User edge (cosine language
                # similarity + min-max shared-repo overlap, weighted sum).
                affinity = score_affinity(bundle)

                # Phase 4 (FR06–FR08): -log transform + Dijkstra trust
                # propagation, exclude direct collaborators and the Following
                # set, take Top N, then hydrate their profiles (one batch call).
                result = recommend(
                    bundle,
                    extraction["users"],
                    top_n=top_n,
                    denylist=settings.RECOMMENDATION_DENYLIST,
                )
                hydrate_recommendations(result, client)
        except GitHubAuthError as exc:
            log.error("github auth error: %s", exc)
            return Response(
                _error("github_auth", "Server is not configured to call the GitHub API."),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except GitHubNotFound as exc:
            return Response(
                _error("user_not_found", str(exc), username=username),
                status=status.HTTP_404_NOT_FOUND,
            )
        except GitHubRateLimited as exc:
            return Response(
                _error("rate_limited", "Upstream GitHub rate limit reached.", reset_at=exc.reset_at),
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except GitHubUpstreamError as exc:
            log.exception("github upstream error")
            return Response(
                _error("upstream_error", str(exc)),
                status=status.HTTP_502_BAD_GATEWAY,
            )

        rec = result.to_dict()
        # Lean by default: the seed's recommendations + compact run summary.
        # The full per-phase data (every graph edge, all sampled users) is a
        # large debug dump, returned only when `verbose` is requested.
        response = {
            "seed": result.seed,
            "recommendations": rec["recommendations"],
            "summary": {
                "users_sampled": extraction["stats"]["users"],
                "graph_nodes": bundle.user_graph.number_of_nodes(),
                "graph_edges": bundle.user_graph.number_of_edges(),
                "alpha": affinity.alpha,
                "truncated_reason": extraction["truncated_reason"],
                **rec["stats"],
            },
        }
        if verbose:
            response["extraction"] = extraction
            response["graph"] = bundle.to_dict()
            response["affinity"] = affinity.to_dict(bundle.user_graph)

        return Response(response)


@extend_schema(
    tags=["Operaciones"],
    summary="Liveness check (ping)",
    description=(
        "Sonda de *liveness*: confirma que el proceso está vivo y respondiendo. "
        "No toca la base de datos ni la API de GitHub, así que siempre devuelve "
        "`200` mientras el servidor esté en pie. Útil para health checks de "
        "plataformas (Render/Vercel/Docker) y balanceadores."
    ),
    responses={200: OpenApiResponse(description='`{"message": "pong"}`')},
)
class PingView(APIView):
    """GET /api/ping — liveness mínima, sin dependencias externas."""

    permission_classes = [AllowAny]
    throttle_classes = []

    def get(self, request, *args, **kwargs):
        """Devuelve `pong` sin tocar dependencias externas."""
        return Response({"message": "pong"})


@extend_schema(
    tags=["Operaciones"],
    summary="Readiness check (health)",
    description=(
        "Sonda de *readiness*: reporta si el servicio está listo para atender "
        "el pipeline de recomendación. Verifica que el `GITHUB_TOKEN` esté "
        "configurado (sin él, `POST /api/recommend` falla con `500`). Devuelve "
        "`200` con `status='ok'` cuando todo está listo, o `503` con "
        "`status='degraded'` y el detalle de los chequeos que fallaron."
    ),
    responses={
        200: OpenApiResponse(description="Servicio listo (`status='ok'`)."),
        503: OpenApiResponse(description="Servicio degradado (`status='degraded'`)."),
    },
)
class HealthView(APIView):
    """GET /api/health — readiness: valida la configuración mínima del servicio."""

    permission_classes = [AllowAny]
    throttle_classes = []

    def get(self, request, *args, **kwargs):
        """Comprueba la configuración requerida y reporta el estado de salud."""
        checks = {
            "github_token": bool(getattr(settings, "GITHUB_TOKEN", "") or ""),
        }
        healthy = all(checks.values())
        return Response(
            {"status": "ok" if healthy else "degraded", "checks": checks},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def _error(code: str, message: str, **details) -> dict:
    """Construye la envoltura de error estándar `{"error": {code, message, details}}`."""
    return {"error": {"code": code, "message": message, "details": details}}
