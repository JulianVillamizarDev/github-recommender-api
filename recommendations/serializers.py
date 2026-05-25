"""Serializadores de petición y respuesta del endpoint de recomendación.

Contiene el serializador de entrada (`RecommendRequestSerializer`, que valida y
normaliza el `username`) y un conjunto de serializadores de respuesta que existen
**únicamente para documentar el esquema OpenAPI** (Swagger/ReDoc): la vista
devuelve diccionarios planos, no la salida de estos serializadores.
"""

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

    top_n = serializers.IntegerField(
        min_value=1,
        max_value=50,
        required=False,
        default=10,
        help_text=(
            "Número máximo de usuarios recomendados a devolver (Top N, FR08). "
            "Se ordenan por afinidad propagada descendente."
        ),
    )

    verbose = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "Si es `true`, la respuesta incluye además los volcados completos de "
            "cada fase (`extraction`, `graph`, `affinity`) para depuración. Por "
            "defecto la respuesta sólo trae las recomendaciones y un resumen."
        ),
    )

    def validate_username(self, value: str) -> str:
        """Normaliza el login: recorta espacios y lo pasa a minúsculas.

        Mantiene el invariante de todo el pipeline (los logins se manejan en
        minúsculas) ya desde el borde de la API.
        """
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
    """Conteos agregados de la extracción BFS (usuarios y aristas por tipo)."""

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


# ---- Fase 2: Modelado del grafo (FR04) ---------------------------------------


class ProjectedEdgeSerializer(serializers.Serializer):
    """Arista del grafo proyectado Usuario↔Usuario."""

    source = serializers.CharField(help_text="Login de uno de los usuarios.")
    target = serializers.CharField(help_text="Login del otro usuario.")
    shared_repos = serializers.IntegerField(
        help_text=(
            "Número de repositorios compartidos entre ambos usuarios (peso de "
            "la proyección). La afinidad final (`weight`) la calcula la Fase 3."
        ),
    )


class BipartiteStatsSerializer(serializers.Serializer):
    """Tamaño del grafo bipartito Usuario↔Repositorio construido en la Fase 2."""

    users = serializers.IntegerField(help_text="Nodos de tipo usuario.")
    repos = serializers.IntegerField(help_text="Nodos de tipo repositorio.")
    edges = serializers.IntegerField(help_text="Aristas usuario↔repositorio.")


class UserGraphSerializer(serializers.Serializer):
    """Grafo proyectado Usuario↔Usuario (enlace si comparten ≥ 1 repositorio)."""

    nodes = serializers.IntegerField(help_text="Cantidad de nodos de usuario.")
    edges = serializers.IntegerField(help_text="Cantidad de aristas de afinidad estructural.")
    edge_list = ProjectedEdgeSerializer(many=True)


class FollowingStatsSerializer(serializers.Serializer):
    """Métricas del grafo dirigido de seguimiento (relación asimétrica, FR07)."""

    edges = serializers.IntegerField(help_text="Aristas dirigidas de seguimiento totales.")
    seed_following_count = serializers.IntegerField(
        help_text="Tamaño del conjunto `following` de la semilla (filtro FR07).",
    )


class GraphStatsSerializer(serializers.Serializer):
    """Resumen del grafo Usuario↔Usuario y de los repos descartados (Fase 2)."""

    user_nodes = serializers.IntegerField()
    user_edges = serializers.IntegerField()
    dropped_repos = serializers.IntegerField()


class GraphSerializer(serializers.Serializer):
    """Topología NetworkX construida en la Fase 2 a partir de la extracción."""

    seed = serializers.CharField(help_text="Login normalizado del nodo semilla.")
    bipartite = BipartiteStatsSerializer()
    user_graph = UserGraphSerializer()
    following = FollowingStatsSerializer()
    seed_following = serializers.ListField(
        child=serializers.CharField(),
        help_text="Logins seguidos por la semilla, excluidos de las recomendaciones (FR07).",
    )
    dropped_repos = serializers.ListField(
        child=serializers.CharField(),
        help_text=(
            "Repositorios descartados antes de proyectar por superar el umbral "
            "de colaboradores (guarda contra mega-repos)."
        ),
    )
    stats = GraphStatsSerializer()


# ---- Fase 3: Cálculo de afinidad técnica (FR05) ------------------------------


class WeightedEdgeSerializer(serializers.Serializer):
    """Arista Usuario↔Usuario con sus componentes de afinidad y el peso final."""

    source = serializers.CharField(help_text="Login de uno de los usuarios.")
    target = serializers.CharField(help_text="Login del otro usuario.")
    cosine = serializers.FloatField(
        help_text="Similitud coseno de los vectores de lenguajes (0.0–1.0).",
    )
    repo_overlap = serializers.FloatField(
        help_text="Solapamiento de Jaccard de los repos de ambos usuarios (`∩/∪`, 0.0–1.0).",
    )
    weight = serializers.FloatField(
        help_text=(
            "Peso final de afinidad: `alpha * cosine + (1 - alpha) * repo_overlap`. "
            "Es la probabilidad que la Fase 4 transforma con `-log(weight)` antes de Dijkstra."
        ),
    )


class AffinityStatsSerializer(serializers.Serializer):
    """Estadísticos de los pesos de afinidad calculados en la Fase 3."""

    edges_scored = serializers.IntegerField(help_text="Número de aristas a las que se les asignó peso.")
    weight_mean = serializers.FloatField(help_text="Peso de afinidad promedio.")
    weight_max = serializers.FloatField(help_text="Peso de afinidad máximo.")
    weight_min = serializers.FloatField(help_text="Peso de afinidad mínimo.")


class AffinitySerializer(serializers.Serializer):
    """Resultado de la Fase 3: pesos de afinidad sobre el grafo Usuario↔Usuario."""

    alpha = serializers.FloatField(
        help_text="Coeficiente de la suma ponderada aplicado al término de similitud de lenguajes.",
    )
    shared_repos_min = serializers.FloatField(help_text="Mínimo de repos compartidos usado en la normalización.")
    shared_repos_max = serializers.FloatField(help_text="Máximo de repos compartidos usado en la normalización.")
    edges = WeightedEdgeSerializer(many=True)
    stats = AffinityStatsSerializer()


# ---- Fase 4: Motor de recomendación y filtrado (FR06–FR08) -------------------


class RecommendedUserSerializer(serializers.Serializer):
    """Usuario recomendado con perfil, afinidad estructural directa y confianza (FR08)."""

    login = serializers.CharField(help_text="Login del usuario recomendado.")
    name = serializers.CharField(allow_null=True, required=False, help_text="Nombre público.")
    avatarUrl = serializers.URLField(allow_null=True, required=False, help_text="URL del avatar.")
    url = serializers.URLField(allow_null=True, required=False, help_text="URL del perfil.")
    affinity = serializers.FloatField(
        help_text=(
            "Afinidad estructural **directa** semilla↔candidato (Adamic–Adar "
            "normalizado contra el mejor candidato). 0.0–1.0; el mejor ≈ 1.0."
        ),
    )
    affinity_pct = serializers.FloatField(help_text="Afinidad estructural como porcentaje (métrica principal).")
    common_neighbors = serializers.IntegerField(
        help_text="Colaboradores en común entre la semilla y el candidato.",
    )
    adamic_adar = serializers.FloatField(
        help_text="Índice Adamic–Adar (`Σ 1/log(grado)` sobre vecinos comunes); pondera colaboradores raros.",
    )
    jaccard = serializers.FloatField(
        help_text="Jaccard de los conjuntos de vecinos de la semilla y el candidato.",
    )
    trust = serializers.FloatField(
        help_text="Confianza propagada por Dijkstra `exp(-distancia)` (métrica secundaria, FR06). 0.0–1.0.",
    )
    trust_pct = serializers.FloatField(help_text="Confianza propagada como porcentaje.")
    distance = serializers.FloatField(
        help_text="Costo acumulado de Dijkstra (`Σ -log(weight)`); menor = más confianza.",
    )


class RecommendationStatsSerializer(serializers.Serializer):
    """Conteos del motor de recomendación: alcanzados, recomendados y excluidos."""

    reachable = serializers.IntegerField(help_text="Usuarios alcanzables desde la semilla por Dijkstra.")
    recommended = serializers.IntegerField(help_text="Usuarios efectivamente recomendados (≤ Top N).")
    excluded_direct = serializers.IntegerField(
        help_text="Colaboradores directos descartados (FR06 pide usuarios indirectos).",
    )
    excluded_following = serializers.IntegerField(
        help_text="Usuarios descartados por estar en la lista de seguidos (FR07).",
    )


class RecommendationsSerializer(serializers.Serializer):
    """Resultado del motor de recomendación (Fase 4)."""

    seed = serializers.CharField(help_text="Login normalizado del nodo semilla.")
    top_n = serializers.IntegerField(help_text="Máximo de recomendaciones solicitado.")
    recommendations = RecommendedUserSerializer(many=True)
    stats = RecommendationStatsSerializer()


class RunSummarySerializer(serializers.Serializer):
    """Resumen compacto de la ejecución del pipeline (siempre presente)."""

    users_sampled = serializers.IntegerField(help_text="Usuarios visitados por el muestreo BFS.")
    graph_nodes = serializers.IntegerField(help_text="Nodos del grafo Usuario↔Usuario.")
    graph_edges = serializers.IntegerField(help_text="Aristas del grafo Usuario↔Usuario.")
    alpha = serializers.FloatField(help_text="Coeficiente de la suma ponderada usado en la Fase 3.")
    truncated_reason = serializers.CharField(
        allow_null=True, help_text="`'rate_limit'` si la extracción se truncó; `null` si no."
    )
    reachable = serializers.IntegerField(help_text="Usuarios alcanzables desde la semilla (Dijkstra).")
    recommended = serializers.IntegerField(help_text="Usuarios recomendados (≤ Top N).")
    excluded_direct = serializers.IntegerField(help_text="Colaboradores directos excluidos (FR06).")
    excluded_following = serializers.IntegerField(help_text="Seguidos excluidos (FR07).")
    excluded_bots = serializers.IntegerField(help_text="Cuentas bot (`*[bot]`) excluidas por sufijo.")
    excluded_non_user = serializers.IntegerField(
        help_text="Cuentas no-usuario (bots/organizaciones/eliminadas) excluidas por tipo durante la hidratación."
    )
    excluded_denied = serializers.IntegerField(
        help_text="Cuentas excluidas por la lista de bloqueo configurable (`RECOMMENDATION_DENYLIST`)."
    )


class RecommendResponseSerializer(serializers.Serializer):
    """Respuesta del endpoint `POST /api/recommend`.

    Por defecto es *liviana*: sólo la semilla, las recomendaciones y un resumen.
    Los campos `extraction`, `graph` y `affinity` sólo aparecen si la petición
    incluye `verbose: true`.
    """

    phase = serializers.IntegerField(help_text="Fase del pipeline a la que corresponde la salida (4 = Recomendación).")
    seed = serializers.CharField(help_text="Login normalizado del nodo semilla consultado.")
    recommendations = RecommendedUserSerializer(many=True)
    summary = RunSummarySerializer()

    # Sólo presentes cuando `verbose=true`.
    extraction = ExtractionSerializer(required=False)
    graph = GraphSerializer(required=False)
    affinity = AffinitySerializer(required=False)


class ErrorBodySerializer(serializers.Serializer):
    """Cuerpo del error: código, mensaje y detalles contextuales opcionales."""

    code = serializers.CharField(help_text="Código de error legible por máquina.")
    message = serializers.CharField(help_text="Mensaje descriptivo en español.")
    details = serializers.DictField(required=False, help_text="Detalles adicionales contextuales.")


class ErrorResponseSerializer(serializers.Serializer):
    """Envoltura de error estándar de la API: `{"error": {...}}`."""

    error = ErrorBodySerializer()
