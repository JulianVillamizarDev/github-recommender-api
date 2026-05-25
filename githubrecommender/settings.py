"""Django settings for githubrecommender project."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(BASE_DIR / ".env")


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-CHANGE-ME-in-production",
)

DEBUG = _bool_env("DJANGO_DEBUG", True)

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]

# Render injects the public hostname here; trust it automatically so we don't
# have to hard-code the *.onrender.com URL in DJANGO_ALLOWED_HOSTS.
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS = [f"https://{_render_host}"]

# Browser origins allowed to call the API (the React dev server is cross-origin:
# Vite runs on :5173, Django on :8000). Comma-separated env var; defaults cover
# Vite's localhost/127.0.0.1 dev URLs.
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Logins to always exclude from recommendations (e.g. automation that runs under
# a genuine User-type account, which type-based filtering can't catch). Comma-
# separated env var, normalized to lowercase.
RECOMMENDATION_DENYLIST = {
    u.strip().lower()
    for u in os.environ.get("RECOMMENDATION_DENYLIST", "").split(",")
    if u.strip()
}


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "recommendations.apps.RecommendationsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files (admin/Swagger assets) in production
    # without needing a separate web server; must sit right after security.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # CORS must precede CommonMiddleware so its headers are added to every
    # response (including the preflight OPTIONS the React dev server sends).
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "githubrecommender.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "githubrecommender.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# DRF — Phase 1 endpoint is public (course project) but throttled.
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("THROTTLE_ANON", "20/min"),
        "user": os.environ.get("THROTTLE_USER", "60/min"),
    },
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "API de Recomendación de GitHub",
    "DESCRIPTION": (
        "Servicio backend que construye recomendaciones de usuarios de GitHub "
        "a partir de un nodo semilla. Implementa la **Fase 1 — Extracción de "
        "Datos**: recibe un nombre de usuario, valida su existencia contra la "
        "API pública de GitHub y extrae la topología social del usuario "
        "(repositorios, lenguajes de programación, colaboradores directos y "
        "lista de seguidos) mediante un muestreo BFS de profundidad ≤ 2.\n\n"
        "Las fases posteriores (construcción del grafo bipartito, cálculo de "
        "afinidad mediante similitud coseno, transformación logarítmica y "
        "búsqueda de candidatos con Dijkstra) consumirán la salida de este "
        "endpoint."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/",
    "TAGS": [
        {"name": "Recomendaciones", "description": "Endpoints del motor de recomendación."},
    ],
    "CONTACT": {"name": "Equipo Teoría de Grafos"},
    "LICENSE": {"name": "Uso académico"},
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,
        "displayOperationId": False,
        "docExpansion": "list",
    },
    "REDOC_UI_SETTINGS": {
        "expandResponses": "200,201",
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "github-recommendation",
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "recommendations": {"handlers": ["console"], "level": "INFO"},
        "django": {"handlers": ["console"], "level": "INFO"},
    },
}
