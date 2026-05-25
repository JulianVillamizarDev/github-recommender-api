"""Cliente GraphQL de GitHub usado por la extracción de la Fase 1.

Implementa las prácticas de los skills `github-graphql-client` y
`api-rate-limit-resilience`: autenticación bearer desde el entorno, consultas
con nombre y variables, paginación por cursor (`pageInfo`), reintentos con
jitter, inspección de límites de cuota y caché de respuestas mediante el
framework de caché de Django.
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
GITHUB_REST_URL = "https://api.github.com"
USER_AGENT = "github-recommendation/0.1 (academic-project)"

USER_TOPOLOGY_QUERY = """
query UserTopology(
    $login: String!,
    $repoFirst: Int!,
    $repoAfter: String,
    $followingFirst: Int!,
    $followingAfter: String
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
    """Token ausente o rechazado por GitHub (401). La vista lo mapea a HTTP 500
    (servidor mal configurado)."""


class GitHubNotFound(RuntimeError):
    """El recurso solicitado no existe (usuario o repositorio). La vista lo mapea
    a HTTP 404 cuando se refiere a la propia semilla."""


class GitHubRateLimited(RuntimeError):
    """Se agotó la cuota de la API de GitHub. Lleva opcionalmente el instante de
    reinicio (`reset_at`); la vista lo mapea a HTTP 429."""

    def __init__(self, reset_at: int | None = None) -> None:
        super().__init__("GitHub rate limit exceeded.")
        self.reset_at = reset_at


class GitHubUpstreamError(RuntimeError):
    """Error inesperado de la API de GitHub (5xx o error GraphQL no tolerable).
    La vista lo mapea a HTTP 502."""


@dataclass
class RateLimit:
    """Instantánea del estado de cuota GraphQL reportado por GitHub (`rateLimit`)."""

    remaining: int | None
    reset_at: str | None
    cost: int | None = None


class GitHubGraphQLClient:
    """Envoltura síncrona y ligera sobre el endpoint GraphQL de GitHub.

    Concentra toda la resiliencia (auth, reintentos, manejo de cuota, tolerancia
    a errores parciales y caché). Úsese como context manager para cerrar el
    cliente HTTP subyacente."""

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 20.0,
        max_attempts: int = 4,
    ) -> None:
        """Inicializa el cliente HTTP con auth bearer desde `GITHUB_TOKEN`.

        Lanza :class:`GitHubAuthError` si no hay token configurado (en el entorno
        o en `.env`)."""
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
        """Cierra el cliente HTTP subyacente y libera sus conexiones."""
        self._client.close()

    def __enter__(self) -> "GitHubGraphQLClient":
        """Entra al context manager; devuelve el propio cliente."""
        return self

    def __exit__(self, *exc: Any) -> None:
        """Sale del context manager cerrando el cliente HTTP."""
        self.close()

    def _post(
        self, query: str, variables: dict[str, Any], partial_ok: bool = False
    ) -> dict[str, Any]:
        """Ejecuta una consulta GraphQL con reintentos y devuelve el bloque `data`.

        Reintenta con backoff y jitter ante timeouts, errores de transporte y
        respuestas 5xx; maneja 401/403/404 y los límites de cuota. Tolera errores
        GraphQL parciales (ver :meth:`_classify_graphql_errors`); con
        ``partial_ok`` activo también tolera `NOT_FOUND` por alias. Lanza las
        excepciones tipadas del módulo cuando el fallo es fatal."""
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
                if payload.get("errors"):
                    fatal = self._classify_graphql_errors(payload["errors"], partial_ok=partial_ok)
                    if fatal is not None:
                        raise fatal
                    # Non-fatal partial errors (e.g. forbidden `collaborators`):
                    # GitHub still returns the sibling data, so use it.
                    log.warning(
                        "github graphql partial errors tolerated: %s",
                        self._summarize_errors(payload["errors"]),
                    )
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
        """Maneja una respuesta 403: distingue agotamiento de cuota de un bloqueo
        temporal.

        Si las cabeceras indican cuota agotada (`x-ratelimit-remaining == 0`)
        lanza :class:`GitHubRateLimited`; si hay `Retry-After`, duerme ese tiempo
        (tope 60 s); en otro caso aplica backoff exponencial."""
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
        """Duerme un backoff exponencial con jitter (tope 60 s) antes de
        reintentar, según el número de intento."""
        delay = min(60.0, random.uniform(0.0, 2.0 ** attempt))
        time.sleep(delay)

    @staticmethod
    def _classify_graphql_errors(
        errors: list[dict[str, Any]], partial_ok: bool = False
    ) -> Exception | None:
        """Decide cómo manejar los ``errors`` de GraphQL.

        Devuelve la excepción a lanzar, o ``None`` si todos los errores son
        parciales no fatales y GitHub aun así devolvió ``data`` hermana usable.
        Siempre no fatal: un subcampo prohibido (p. ej. ``collaborators`` en
        repos que no administramos). Con ``partial_ok`` activo (hidratación de
        perfiles por lotes), un ``NOT_FOUND`` por alias (un login sin resolver,
        como un bot) también se tolera — solo la verificación de existencia de la
        semilla necesita que ``NOT_FOUND`` sea fatal.
        """
        codes = {e.get("type") for e in errors}
        if "RATE_LIMITED" in codes:
            return GitHubRateLimited()
        if "NOT_FOUND" in codes and not partial_ok:
            return GitHubNotFound("; ".join(e.get("message", "") for e in errors))

        def _tolerable(e: dict[str, Any]) -> bool:
            if "permission" in (e.get("message") or "").lower():
                return True
            return partial_ok and e.get("type") == "NOT_FOUND"

        if all(_tolerable(e) for e in errors):
            return None
        return GitHubUpstreamError(
            "GraphQL error: " + "; ".join(e.get("message", "") for e in errors)
        )

    @staticmethod
    def _summarize_errors(errors: list[dict[str, Any]]) -> str:
        """Resume una lista de errores GraphQL en una línea (conteo + mensajes
        únicos) para registrarla en el log."""
        unique = sorted({e.get("message", "") for e in errors})
        return f"{len(errors)} error(s): " + " | ".join(unique)

    @staticmethod
    def _record_rate_limit(payload: dict[str, Any]) -> None:
        """Registra en el log el bloque `rateLimit` de la respuesta (cuota
        restante, costo e instante de reinicio), si está presente."""
        rl = (payload.get("data") or {}).get("rateLimit") or {}
        if rl:
            log.info(
                "github.ratelimit remaining=%s cost=%s reset_at=%s",
                rl.get("remaining"), rl.get("cost"), rl.get("resetAt"),
            )

    # ---- Public API used by views / sampler ---------------------------------

    def user_exists(self, login: str) -> bool:
        """Indica si existe el usuario de GitHub (FR02). Resultado cacheado 60 s;
        un `NOT_FOUND` se interpreta como inexistente (devuelve ``False``)."""
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

    def fetch_repo_contributors(self, name_with_owner: str, limit: int = 15) -> list[str]:
        """Logins de los principales colaboradores de un repo vía el endpoint
        **REST público**.

        El campo ``collaborators`` de GraphQL exige permiso de administrador del
        repo y falla para repos públicos arbitrarios; el endpoint REST de
        contributors es público. Esta es la señal que vincula a usuarios distintos
        con un repo compartido (proyección FR04). Los datos de colaboradores son
        complementarios — cualquier fallo (prohibido, inexistente, 202 aún
        calculando, cuota agotada) produce una lista vacía en vez de abortar toda
        la extracción.
        """
        cache_key = f"gh:contributors:{name_with_owner}:{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        owner, _, name = name_with_owner.partition("/")
        if not owner or not name:
            return []

        logins: list[str] = []
        try:
            resp = self._client.get(
                f"{GITHUB_REST_URL}/repos/{owner}/{name}/contributors",
                params={"per_page": limit, "anon": "false"},
                headers={"Accept": "application/vnd.github+json"},
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return []

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = []
            logins = [
                c["login"].lower()
                for c in data
                if isinstance(c, dict) and c.get("login")
            ]
        # 202 = stats still computing, 204 = empty, 403 = forbidden/rate,
        # 404 = gone — all non-fatal for supplementary data; logins stays [].

        cache.set(cache_key, logins, timeout=30 * 60)
        return logins

    def fetch_profiles(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        """Obtiene en lote el perfil público + tipo de cuenta de unos pocos logins
        en una única consulta GraphQL con alias (hidratación del Top-N, FR08).

        El campo ``user(login:)`` de GraphQL resuelve **solo cuentas de tipo
        `User`**; bots, organizaciones y cuentas eliminadas devuelven ``null``.
        Así, cada perfil devuelto lleva un ``type`` ``"User"`` (resuelto) o
        ``None`` (no es un usuario real) — los llamadores lo usan para el filtrado
        por tipo (bots/orgs). Tolera logins sin resolver (no aborta el lote).
        Cacheado por login 30 min.

        Ante un fallo duro (cuota agotada / error de upstream), los logins no
        obtenidos simplemente se **omiten** del resultado — nunca se fabrican como
        no-usuarios — para que un error transitorio no se confunda con un veredicto
        de "no es un usuario real".
        """
        uniq = [login for login in dict.fromkeys(logins) if login]
        if not uniq:
            return {}

        result: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for login in uniq:
            cached = cache.get(f"gh:profile:{login}")
            if cached is not None:
                result[login] = cached
            else:
                missing.append(login)

        if missing:
            var_defs = ", ".join(f"$l{i}: String!" for i in range(len(missing)))
            aliases = "\n".join(
                f'u{i}: user(login: $l{i}) {{ __typename login name avatarUrl url }}'
                for i in range(len(missing))
            )
            query = f"query Profiles({var_defs}) {{ rateLimit {{ remaining resetAt }}\n{aliases}\n}}"
            variables = {f"l{i}": login for i, login in enumerate(missing)}
            try:
                data = self._post(query, variables, partial_ok=True) or {}
            except (GitHubRateLimited, GitHubUpstreamError):
                return result  # best-effort: leave un-fetched logins absent
            for i, login in enumerate(missing):
                node = data.get(f"u{i}") or {}
                profile = {
                    "login": login,
                    "type": node.get("__typename"),  # "User", or None if not a user
                    "name": node.get("name"),
                    "avatarUrl": node.get("avatarUrl"),
                    "url": node.get("url"),
                }
                cache.set(f"gh:profile:{login}", profile, timeout=30 * 60)
                result[login] = profile
        return result

    def fetch_user_topology(
        self,
        login: str,
        per_page: int = 50,
        max_repo_pages: int = 2,
        max_following_pages: int = 2,
    ) -> dict[str, Any]:
        """Obtiene la topología de un usuario (repos + lenguajes + following).

        Los colaboradores se obtienen aparte vía :meth:`fetch_repo_contributors`
        (REST) porque GraphQL prohíbe ``collaborators`` en repos que no
        administramos. Devuelve un dict normalizado; nunca lanza GitHubNotFound
        para usuarios de la frontera (devuelve un registro con ``found=False`` para
        que el BFS continúe). Sí lanza en la primera llamada de la semilla cuando
        la propia semilla no existe.
        """
        cache_key = f"gh:topology:{login}:{per_page}:{max_repo_pages}:{max_following_pages}"
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
    """Normaliza los nodos de repositorio de la respuesta GraphQL a un dict
    plano (``nameWithOwner``, ``isPrivate``, lenguaje principal, mapa de
    lenguajes→bytes), con ``collaborators`` vacío que el muestreador rellenará
    luego vía REST."""
    out: list[dict[str, Any]] = []
    for n in nodes:
        primary = (n.get("primaryLanguage") or {}).get("name")
        lang_edges = ((n.get("languages") or {}).get("edges") or [])
        languages = {
            e["node"]["name"]: int(e["size"])
            for e in lang_edges
            if e.get("node") and e.get("node", {}).get("name")
        }
        out.append({
            "nameWithOwner": n["nameWithOwner"],
            "isPrivate": n.get("isPrivate", False),
            "primaryLanguage": primary,
            "languages": languages,
            # Populated later from REST contributors (see bfs_sampler enrichment).
            "collaborators": [],
        })
    return out
