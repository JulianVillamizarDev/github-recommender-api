from rest_framework import serializers

GITHUB_USERNAME_REGEX = r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$"


class RecommendRequestSerializer(serializers.Serializer):
    """Cuerpo de la petición para iniciar la extracción de topología."""

    username = serializers.RegexField(
        regex=GITHUB_USERNAME_REGEX,
        max_length=39,
        help_text=(
            "Nombre de usuario de GitHub que actúa como **nodo semilla** del "
            "recorrido. Debe cumplir las reglas oficiales de GitHub: "
            "1–39 caracteres alfanuméricos, con guiones simples (no al inicio "
            "ni al final, sin guiones consecutivos). El valor se normaliza a "
            "minúsculas antes de procesarse."
        ),
        error_messages={"invalid": "Nombre de usuario de GitHub no válido."},
    )
    max_depth = serializers.IntegerField(
        min_value=1,
        max_value=2,
        required=False,
        default=2,
        help_text=(
            "Profundidad máxima del muestreo BFS desde la semilla. "
            "Valor 1 → solo vecinos directos. Valor 2 → vecinos de vecinos "
            "(recomendado). Se limita a 2 para respetar la cuota de la API "
            "de GitHub."
        ),
    )
    per_node_limit = serializers.IntegerField(
        min_value=1,
        max_value=100,
        required=False,
        default=50,
        help_text=(
            "Número máximo de elementos a recuperar por conexión y por nodo "
            "(repositorios, seguidos). Valores altos producen un grafo más "
            "completo a costa de más puntos de cuota GraphQL."
        ),
    )

    def validate_username(self, value: str) -> str:
        return value.strip().lower()


# ---- Schemas de respuesta (sólo documentación) -------------------------------


class _LanguageStatsSerializer(serializers.Serializer):
    """Mapa lenguaje → bytes de código agregados para el usuario."""

    class Meta:
        ref_name = "LanguageStats"


class RepositoryEdgeSerializer(serializers.Serializer):
    """Arista (usuario, repositorio) del grafo bipartito (Fase 2)."""

    user = serializers.CharField(help_text="Login del usuario.")
    repo = serializers.CharField(help_text="Identificador `owner/name` del repositorio.")


class UserNodeSerializer(serializers.Serializer):
    """Nodo de usuario con metadatos agregados durante la extracción."""

    login = serializers.CharField(help_text="Login normalizado en minúsculas.")
    name = serializers.CharField(allow_null=True, required=False, help_text="Nombre público del usuario.")
    avatarUrl = serializers.URLField(allow_null=True, required=False, help_text="URL del avatar.")
    url = serializers.URLField(allow_null=True, required=False, help_text="URL pública del perfil.")
    languages = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Mapa lenguaje → bytes de código agregados a través de sus repositorios.",
    )
    repos = serializers.ListField(
        child=serializers.CharField(),
        help_text="Lista de identificadores `owner/name` de los repositorios extraídos.",
    )
    following = serializers.ListField(
        child=serializers.CharField(),
        help_text="Logins de los usuarios que sigue (truncado por `per_node_limit`).",
    )
    is_seed = serializers.BooleanField(help_text="Indica si este nodo es la semilla.")
    truncated = serializers.DictField(
        child=serializers.BooleanField(),
        help_text="Banderas por conexión que indican si la paginación se truncó.",
    )


class EdgesSerializer(serializers.Serializer):
    """Aristas extraídas, agrupadas por tipo, listas para construir el grafo."""

    user_repo = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField(), min_length=2, max_length=2),
        help_text="Pares `[usuario, repo]` que alimentan el grafo bipartito (Fase 2).",
    )
    collaborator = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField(), min_length=2, max_length=2),
        help_text="Pares `[usuario_a, usuario_b]` que han colaborado en algún repositorio.",
    )
    following = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField(), min_length=2, max_length=2),
        help_text="Pares dirigidos `[usuario, seguido]` (relación asimétrica).",
    )


class ExtractionStatsSerializer(serializers.Serializer):
    users = serializers.IntegerField(help_text="Cantidad total de usuarios visitados.")
    edges_user_repo = serializers.IntegerField(help_text="Cantidad de aristas usuario↔repositorio.")
    edges_collaborator = serializers.IntegerField(help_text="Cantidad de aristas de colaboración.")
    edges_following = serializers.IntegerField(help_text="Cantidad de aristas de seguimiento.")


class ExtractionSerializer(serializers.Serializer):
    """Resultado de la extracción BFS alrededor del nodo semilla."""

    seed = serializers.CharField(help_text="Login normalizado del nodo semilla.")
    seed_profile = serializers.DictField(help_text="Datos de perfil de la semilla.")
    users = serializers.DictField(
        child=UserNodeSerializer(),
        help_text="Diccionario `login → UserNode` con todos los usuarios descubiertos.",
    )
    edges = EdgesSerializer()
    depth_reached = serializers.IntegerField(
        help_text="Profundidad efectivamente recorrida (puede ser menor a `max_depth` si se trunca).",
    )
    truncated_reason = serializers.CharField(
        allow_null=True,
        help_text="`'rate_limit'` si se detuvo por cuota; `null` si terminó normalmente.",
    )
    truncated_nodes = serializers.ListField(
        child=serializers.CharField(),
        help_text="Logins cuya paginación se truncó por `per_node_limit`.",
    )
    stats = ExtractionStatsSerializer()


class RecommendResponseSerializer(serializers.Serializer):
    """Envoltorio de respuesta del endpoint `POST /api/recommend`."""

    phase = serializers.IntegerField(help_text="Fase del pipeline a la que corresponde la salida (1 = Extracción).")
    extraction = ExtractionSerializer()


class ErrorBodySerializer(serializers.Serializer):
    code = serializers.CharField(help_text="Código de error legible por máquina.")
    message = serializers.CharField(help_text="Mensaje descriptivo en español.")
    details = serializers.DictField(required=False, help_text="Detalles adicionales contextuales.")


class ErrorResponseSerializer(serializers.Serializer):
    error = ErrorBodySerializer()
