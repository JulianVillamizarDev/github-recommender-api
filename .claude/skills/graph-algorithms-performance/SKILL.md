---
name: graph-algorithms-performance
description: Use when implementing or reviewing graph affinity scoring and pathfinding — cosine similarity over language/feature vectors, min-max normalization, weighted-sum aggregation, negative-logarithm weight transformation, and Dijkstra shortest-path for trust propagation. Activates on imports of `numpy`, `scipy`, `sklearn.metrics.pairwise`, `networkx`, or when the user mentions cosine similarity, normalization, Dijkstra, shortest path, affinity, or weighted-sum scoring.
---

# Graph Algorithms & Performance — Practices

Applies to Phases 3 and 4 of `requirements.md` (FR05 affinity scoring; FR06 candidate search via Dijkstra after `-log(W)` transformation).

## 1. Cosine similarity over language vectors (FR05)

- Build a global **language vocabulary** once: `langs = sorted({l for u in users for l in u.languages})`. Reuse the same index map for every vector.
- Represent each user as a sparse vector — most users use < 10 languages out of hundreds.
- For pairwise similarity over many users, compute **in bulk**, not per-edge:
  ```python
  from sklearn.metrics.pairwise import cosine_similarity
  M = scipy.sparse.csr_matrix(...)        # users × languages
  S = cosine_similarity(M, dense_output=False)
  ```
- Vector values: bytes-of-code per language is more meaningful than repo-count, but log-scale it (`log1p`) — one mega Java repo shouldn't dwarf five small TS repos.
- Cosine output is in `[-1, 1]` for general vectors but **`[0, 1]` here** because byte counts are non-negative. No clipping needed.
- Cache the (user, vector) map; only re-vectorize users whose data changed.

## 2. Min-max normalization (FR05)

```python
x_norm = (x - x_min) / (x_max - x_min) if x_max > x_min else 0.0
```
- Compute `x_min` / `x_max` over the **entire edge set** at once, not per-edge — vectorize with NumPy.
- Guard the divide-by-zero case (single repo, all edges have count 1).
- Don't normalize across two unrelated edge sets that you'll later compare; min/max from each population is meaningless across populations.
- Store the min/max used for normalization — needed if edges are added incrementally.

## 3. Weighted-sum aggregation (FR05)

```python
weight = ALPHA * cosine + (1 - ALPHA) * normalized_overlap
```
- Keep `ALPHA` as a config constant, not magic. Tune empirically; document the value chosen and why.
- Both inputs must already be in `[0, 1]` (cosine of non-negative vectors + min-max normalized overlap). Assert this in dev.
- The output is a **probabilistic-style affinity** — keep it in `[0, 1]` so the `-log` transform behaves.

## 4. Negative-log transformation (Phase 4)

```python
import math
distance = -math.log(weight) if weight > 0 else math.inf
```
- Vectorize with NumPy when applying to all edges: `dist = -np.log(np.clip(W, 1e-12, 1.0))`. Clip to avoid `log(0) = -inf`.
- Effect: `weight=1.0 → dist=0`, `weight=0.5 → dist=0.69`, `weight→0 → dist→∞`. Multiplying probabilities along a path becomes summing distances — exactly what Dijkstra needs to maximize trust propagation.
- Apply once after final weights are computed; don't recompute per Dijkstra call.

## 5. Dijkstra (FR06)

- Use `nx.single_source_dijkstra_path_length(G, seed, weight="log_weight")` once per request, **not** `shortest_path_length(G, seed, target)` in a loop over targets — same complexity, but the loop variant pays setup overhead per call.
- Returns `{node: distance}`. Convert back to affinity with `affinity = math.exp(-distance)` if you need to display a propagated probability.
- Sort candidates by distance ascending (= affinity descending), then apply the Following exclusion (FR07) as a set difference, then take Top N.
- Negative-log distances are **non-negative** (since weights ≤ 1) — Dijkstra's correctness invariant holds. Never feed negative weights to Dijkstra; if you ever get one, your normalization is broken.

## 6. Following exclusion (FR07)

```python
following: set[str] = ...
candidates = [(node, d) for node, d in dists.items()
              if node != seed and node not in following]
candidates.sort(key=lambda x: x[1])
top_n = candidates[:N]
```
- `set` membership is O(1); never use `list.__contains__` here.
- Exclude the seed node itself.

## 7. Performance budget

- Depth-2 BFS sample → typically 100s to low-1000s of users. Cosine bulk: milliseconds. Dijkstra: milliseconds. **The bottleneck is always the GitHub API**, not the math.
- If the math ever becomes the bottleneck, profile with `cProfile` and replace the slow step with a `scipy.sparse` / `numpy` vectorized version. Don't micro-optimize Python loops — replace them.

## 8. Anti-patterns to flag

- Computing cosine in a Python double-loop instead of one matrix multiply.
- Recomputing min/max inside the loop that normalizes each edge.
- Using `math.log` inside Dijkstra's relaxation step instead of pre-transforming edge weights.
- Hardcoding `ALPHA` (the weighted-sum coefficient) inside business logic.
- Using `shortest_path_length(G, src, tgt)` in a loop over candidate `tgt`.
- Forgetting to clip weight before `-log` (causes `inf` propagating through results).
- Passing raw probabilities (without `-log`) to Dijkstra — that finds the *minimum* probability path, the opposite of what you want.
