---
name: django-drf-best-practices
description: Use when writing or reviewing Django or Django REST Framework code — models, views, serializers, querysets, settings, middleware, URL routing, migrations, tests, or anything under a Django project layout (manage.py, settings.py, INSTALLED_APPS). Covers ORM/query performance (N+1, select_related, prefetch_related, only/defer, indexes), DRF serializers and viewsets, pagination, throttling, caching, async views, project structure, and security defaults (CSRF/CORS, SECRET_KEY, permissions, manage.py check --deploy).
---

# Django & DRF — Good Practices and Performance

Apply this guidance whenever the working file is part of a Django/DRF project (presence of `manage.py`, `settings.py`, `INSTALLED_APPS`, or imports from `django` / `rest_framework`).

## 1. ORM & queries

- Profile before optimizing: `django-debug-toolbar` in dev, `QuerySet.explain(analyze=True)` for slow queries.
- Default joins:
  - FK / OneToOne → `select_related(...)` (single SQL JOIN).
  - M2M / reverse FK → `prefetch_related(...)` (separate query, joined in Python).
- Use `only(...)` / `defer(...)` **only** when a column is genuinely heavy (text, JSON, blob). Otherwise it usually hurts.
- Large reads: `.iterator(chunk_size=2000)` to avoid loading the entire queryset into memory.
- Batch writes: `bulk_create`, `bulk_update`. Wrap multi-step writes in `transaction.atomic()`.
- Indexes: `db_index=True` on columns used in `filter`, `order_by`, joins; composite indexes via `Meta.indexes = [models.Index(fields=[...])]`.
- Detect N+1 in tests with `self.assertNumQueries(n)` or `CaptureQueriesContext`.
- Avoid `len(qs)` when you only need existence — use `qs.exists()`. Avoid `count()` in loops.
- Push computation into the DB with `annotate`, `aggregate`, `F`, `Q`, `Subquery`, `Exists` instead of Python loops.

## 2. DRF serializers & views

- Prefer `ModelSerializer` with explicit `fields = [...]`. Never use `fields = '__all__'` on public APIs (leaks columns added later).
- For list endpoints, override `get_queryset()` to apply `select_related` / `prefetch_related` that match the serializer's nested fields. The serializer reveals the access pattern; the view must prefetch it.
- `SerializerMethodField` is the most common N+1 source. Prefer queryset annotations and expose them via plain serializer fields.
- Routing: `ViewSet` + `DefaultRouter` for CRUD resources; `APIView` for one-off endpoints. Don't fight the framework with custom URL handlers.
- Always paginate list endpoints. `PageNumberPagination` for small/medium sets, `CursorPagination` for large or append-only feeds.
- Permissions: set `DEFAULT_PERMISSION_CLASSES = ['rest_framework.permissions.IsAuthenticated']` globally and opt **in** to public with `AllowAny`. Default-deny is safer than default-allow.
- Throttling: apply `UserRateThrottle` / `AnonRateThrottle` (or scoped throttles) on expensive endpoints — especially anything that fans out to an external API.
- Validation belongs in the serializer (`validate_<field>`, `validate`). Don't validate in the view.

## 3. Caching & async

- `django.core.cache` with Redis in prod; `LocMemCache` only for dev. Per-process memory caches lie under multi-worker servers.
- Cache expensive reads with `@cache_page` (view-level) or `cache.get_or_set(key, fn, ttl)` (fine-grained). Always set a TTL.
- For outbound calls to third-party APIs (e.g. the GitHub GraphQL API used by this project), cache responses keyed on (input args, auth scope) with a sane TTL. Respect upstream rate limits.
- Async views (`async def`) help only for IO-bound work (HTTP, sockets). Use `httpx.AsyncClient` for outbound; don't call sync ORM from async without `sync_to_async`.
- Offload long-running work (>~500ms, or anything graph/ML/scraping such as BFS sampling and Dijkstra in this project) to Celery or RQ. The request cycle is not a job queue.

## 4. Project structure & settings

- Split settings: `settings/base.py`, `settings/dev.py`, `settings/prod.py`, selected via `DJANGO_SETTINGS_MODULE`. Don't gate behavior on `if DEBUG:`.
- Read secrets from env (`os.environ`, `django-environ`, or `pydantic-settings`). Never commit `SECRET_KEY`, DB passwords, OAuth tokens, or API keys (e.g. `GITHUB_TOKEN`). Add a `.env.example`.
- One app per bounded context (e.g. `recommendations`, `github_client`, `graphs`). Keep apps small and cohesive; avoid one mega-app named `core` that grows forever.
- Pin dependencies (`requirements.txt` with hashes, or `pyproject.toml` + `uv` / `poetry`).
- Migrations are code: review them, name them, and don't squash without a reason. Never edit a migration that has shipped.

## 5. Security defaults

- Run `python manage.py check --deploy` before every deploy and treat warnings as errors.
- In production: `DEBUG = False`, explicit `ALLOWED_HOSTS`, `SECURE_SSL_REDIRECT = True`, `SESSION_COOKIE_SECURE = True`, `CSRF_COOKIE_SECURE = True`, `SECURE_HSTS_SECONDS` set.
- CORS: use `django-cors-headers` with an explicit `CORS_ALLOWED_ORIGINS` list. Never `CORS_ALLOW_ALL_ORIGINS = True` in prod.
- Validate all input through serializers / forms. Never interpolate user input into raw SQL — use parameterized `RawSQL` / `cursor.execute(sql, params)` if raw is unavoidable.
- File uploads: validate content-type and size; store outside the web root; serve via signed URLs.

## 6. Testing

- Use DRF's `APIClient` for API tests. Assert status code, response shape, and `assertNumQueries(n)` for hot endpoints — query count regressions are the most common perf bug.
- `pytest-django` + `factory_boy` over JSON fixtures: factories are typed, composable, and don't drift.
- Test permission boundaries explicitly: anonymous, wrong-user, correct-user, admin.

## 7. Diagnostic commands

```bash
pip install django-debug-toolbar django-silk django-extensions
python manage.py check --deploy
python manage.py shell_plus --print-sql      # see SQL for every ORM call
python manage.py migrate --plan              # preview migrations
```

```python
qs.explain(analyze=True)                      # PG/MySQL EXPLAIN ANALYZE
from django.db import connection; print(connection.queries)
from django.test.utils import CaptureQueriesContext
```

## Anti-patterns to flag immediately

- `fields = '__all__'` in a public serializer.
- `SerializerMethodField` that calls `obj.related_set.all()` or `.filter()` per row.
- A list view without pagination.
- A view that hits an external API synchronously without caching or throttling.
- `DEBUG = True` or `ALLOWED_HOSTS = ['*']` in any committed settings file.
- Secrets in `settings.py` or `settings/base.py`.
- `.all()` followed by Python-side filtering/aggregation that the DB could do.
