# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Django + DRF backend for a GitHub user-recommendation engine built as a graph-theory course project. A seed GitHub username goes in; the system samples the user's social/repo neighborhood, builds a graph, scores affinity, and recommends undiscovered users. The full design lives in `requirements.md` (functional requirements FR01–FR08 and a four-phase algorithm plan).

**All four phases are implemented** — `POST /api/recommend` runs the full pipeline end to end:
- Phase 1: BFS topology extraction around the seed → `recommendations/bfs_sampler.py`.
- Phase 2: bipartite (user↔repo) graph + weighted User↔User projection + seed Following set via NetworkX → `recommendations/graph_builder.py`.
- Phase 3: affinity weights — cosine over language vectors + min-max-normalized shared-repo count, weighted sum → `recommendations/affinity.py`. Annotates each `user_graph` edge with `cosine`, `repo_overlap`, `weight`.
- Phase 4: rank indirect candidates (FR06) by **direct structural affinity** (weighted Adamic–Adar over shared collaborators), exclude the Following set (FR07), Top-N with hydrated profile data (FR08) → `recommendations/recommender.py`. The FR06 `-log(weight)` + Dijkstra trust propagation is retained as a secondary `trust` field.

The endpoint returns a **lean** `{phase, seed, recommendations, summary}` by default; pass `verbose: true` in the request to also get the full per-phase dumps (`extraction`, `graph`, `affinity`) — those contain the entire weighted graph (all users' edges), not just the seed's, so they're debug-only. Recommendations are profile-hydrated (one batched GraphQL call resolves Top-N `name`/`avatar`/`url`) and bot accounts (`*[bot]`) are filtered out. Each phase consumes the previous phase's structure: Phase 2 reads `BFSResult.to_dict()`; Phases 3 and 4 mutate the `GraphBundle.user_graph` edges in place (the NetworkX object, not a dict — Phase 3 adds `weight`, Phase 4 adds `log_weight`). Phase 4 clips `weight` to `1e-12` before `-log` since an edge can score `weight=0` (no shared languages + non-discriminating overlap). `networkx` and `numpy` are in `requirements.txt`; `scipy` / `scikit-learn` aren't needed (affinity is vectorized over the edge set with NumPy, not all-pairs).

## Commands

PowerShell on Windows. A `GITHUB_TOKEN` (PAT with `read:user`, `public_repo`) in `.env` is required for the endpoint to work — copy `.env.example` to `.env` first.

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate          # sqlite, only needed for admin/sessions
python manage.py runserver        # serves http://127.0.0.1:8000
```

- API docs (Swagger): `http://127.0.0.1:8000/api/docs/`  · ReDoc: `/api/redoc/` · OpenAPI schema: `/api/schema/`
- There is **no test suite yet** and no linter configured. `python manage.py check` is the only static gate.

> **Git boundary:** the git repository is rooted at this `github-recommendation/` directory, not the parent `TEORIA DE GRAFOS/` folder. The sibling `github-user-recommender-mockup/` frontend and the parent-level root `CLAUDE.md` are **untracked** — every commit, branch, and `git status` covers backend files only.

Example request:
```powershell
curl -X POST http://127.0.0.1:8000/api/recommend -H "Content-Type: application/json" -d '{\"username\": \"octocat\"}'
```

## Architecture

Single Django app `recommendations` mounted under `/api/` (project package: `githubrecommender`). A `POST /api/recommend` request flows through six modules, one per pipeline stage (the view orchestrates them in sequence — see `RecommendView.post`):

1. **`views.py` → `RecommendView`** (`POST /api/recommend`, `AllowAny` + anon throttle). Validates the body, checks user existence, runs the sampler, and maps a fixed set of client exceptions to HTTP codes (404 not-found, 429 rate-limited, 500 missing-token, 502 upstream). Errors use the `_error()` envelope: `{"error": {"code", "message", "details"}}`.

2. **`github_client.py` → `GitHubGraphQLClient`** — thin sync `httpx` wrapper over the GitHub API. One nested `UserTopology` **GraphQL** query fetches repos + languages + following per user. **Collaborators are NOT in the GraphQL query**: GitHub's GraphQL `collaborators` field requires repo-admin and errors for arbitrary public repos, so `fetch_repo_contributors` hits the public **REST** endpoint (`/repos/{owner}/{repo}/contributors`) instead — failures there are non-fatal (empty list), since contributor data is supplementary. Owns all resilience: bearer auth from `GITHUB_TOKEN`, retry-with-jitter backoff, 403/`Retry-After`/rate-limit-reset handling, partial-GraphQL-error tolerance, and caching via Django's cache (`user_exists` 60s, topology + contributors 30min). Raises typed exceptions (`GitHubAuthError/NotFound/RateLimited/UpstreamError`) the view catches. Use as a context manager.

3. **`bfs_sampler.py` → `sample_topology`** — depth-bounded BFS (`max_depth` 1 or 2) over the social graph. Marks `visited` at enqueue time to avoid duplicate fetches; fetches each frontier layer in parallel via a bounded `ThreadPoolExecutor`. **`_enrich_contributors` attaches REST contributors to each fetched user's repos** (top `max_contrib_repos` per user, capped globally by a shared `contrib_budget` counter so a request can't fan out into thousands of REST calls). Crucially, `_absorb_topology` emits a `(contributor, repo)` edge into `user_repo` — this is what links distinct users to a shared repo so the Phase 2 projection can connect the seed to its collaborators' network (without it the seed is almost always isolated). Frontier is capped at `max_frontier`. On mid-traversal rate-limit it returns **partial results** (`truncated_reason="rate_limit"`).

4. **`graph_builder.py` → `build_graphs`** (Phase 2, FR04) — pure in-memory NetworkX work, no GitHub calls. Builds the bipartite User↔Repo `nx.Graph`, drops mega-repos above `DEFAULT_MAX_REPO_DEGREE` (projection is O(Σ deg(repo)²)), projects to a weighted User↔User `nx.Graph` (`shared_repos` edge attr), and keeps the seed's `following` as a plain `set`. Returns a `GraphBundle`; `weight` on the projected edges is intentionally left unset for Phase 3 to fill.

5. **`affinity.py` → `score_affinity`** (Phase 3, FR05) — vectorized NumPy scoring over the projected edge set. Builds one `users × languages` matrix of `log1p`-scaled byte counts, L2-normalizes rows (zero-vector users → cosine 0, guarded against NaN), and computes per-edge cosine via dot product; computes a **Jaccard** repo overlap (`shared_repos / (repo_count_u + repo_count_v - shared_repos)`, using the `repo_count` node attr from Phase 2) — *not* min-max, which flattened the common single-shared-repo case to 0; combines as `DEFAULT_ALPHA*cosine + (1-ALPHA)*repo_overlap`. Mutates edges in place, returns an `AffinityResult`.

6. **`recommender.py` → `recommend`** (Phase 4, FR06–FR08) — filters reachable nodes: drop the seed, its direct neighbors (FR06 wants *indirect* users), `bundle.seed_following` (FR07), `*[bot]` accounts, and `settings.RECOMMENDATION_DENYLIST` logins. **Headline metric is direct structural affinity** (`_structural_scores`), not path-propagated trust: a *weighted Adamic–Adar* over shared collaborators where each shared neighbor `w` contributes `weight(seed,w)·weight(w,node)/log(deg w)`, normalized against the strongest candidate (best ≈ 100%). This was chosen because ~all recommended users are 2-hop nodes with no language data, so cosine/propagated-trust degenerate to near-zero floor artifacts; structural affinity needs no extra fetches. The Dijkstra `-log(weight)` propagation is still computed and exposed as a secondary `trust`/`trust_pct` (preserves FR06). `hydrate_recommendations` fills `name`/`avatar`/`url` for the buffered `top_n + HYDRATION_BUFFER` slice via `client.fetch_profiles` (one batched aliased GraphQL query, `partial_ok=True`) and applies **type-based filtering**: `user(login:)` resolves only `User`-type accounts, so an unresolved login is a Bot/Org/deleted account and is dropped (`excluded_non_user`); the list is then trimmed to `top_n`. (Type filtering can't flag automation under a genuine `User` account like `cursoragent` — that's what the denylist is for.) Returns a `RecommendationResult`.

`serializers.py` holds the request serializer (GitHub-username regex validation, normalized to lowercase) plus a full set of response serializers that exist **only to document the OpenAPI schema** — the view returns plain dicts, not serializer output.

### Key invariants
- All GitHub logins are normalized to **lowercase** throughout extraction; preserve this when adding code.
- `max_depth` is capped at 2 and `per_node_limit` at 100 (serializer-enforced) to respect GitHub's GraphQL quota — don't loosen these casually.
- Settings, throttle rates, the GitHub token, and `RECOMMENDATION_DENYLIST` all come from env vars (see `settings.py` `_bool_env` / `os.environ` reads and `.env.example`). `DEFAULT_PERMISSION_CLASSES` is `IsAuthenticated` globally, but `RecommendView` overrides to `AllowAny`.

## Skills

`.claude/skills/` contains project-specific skills that encode the conventions used here — consult the matching one before editing that area: `github-graphql-client`, `api-rate-limit-resilience`, `bfs-sampling-strategy`, `api-input-validation`, `networkx-graph-modeling`, `graph-algorithms-performance`, `django-drf-best-practices`.
