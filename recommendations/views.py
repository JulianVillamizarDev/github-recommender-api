"""Phase 1 API endpoint (FR01, FR02, FR03)."""

from __future__ import annotations

import logging

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

from .bfs_sampler import sample_topology
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
    """POST /api/recommend  — Phase 1: extract topology around a seed user.

    FR01: accepts `username` from the JSON body.
    FR02: validates format (regex) and existence (GitHub API check).
    FR03: extracts repos, languages, collaborators, and following (BFS depth ≤ 2).
    Phases 2–4 (graph build, scoring, ranking) plug into this output later.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def post(self, request, *args, **kwargs):
        ser = RecommendRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        username = ser.validated_data["username"]
        max_depth = ser.validated_data["max_depth"]
        per_node_limit = ser.validated_data["per_node_limit"]

        try:
            with GitHubGraphQLClient() as client:
                if not client.user_exists(username):
                    raise NotFound(_error("user_not_found", f"GitHub user '{username}' not found.", username=username))

                bfs = sample_topology(
                    username,
                    client=client,
                    max_depth=max_depth,
                    per_node_limit=per_node_limit,
                )
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

        return Response({
            "phase": 1,
            "extraction": bfs.to_dict(),
        })


def _error(code: str, message: str, **details) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}
