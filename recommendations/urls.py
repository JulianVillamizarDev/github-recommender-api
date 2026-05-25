from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from .views import HealthView, PingView, RecommendView

urlpatterns = [
    path("ping", PingView.as_view(), name="ping"),
    path("health", HealthView.as_view(), name="health"),
    path("recommend", RecommendView.as_view(), name="recommend"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]
