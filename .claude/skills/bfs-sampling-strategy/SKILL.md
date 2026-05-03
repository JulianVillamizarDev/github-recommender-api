---
name: bfs-sampling-strategy
description: Use when implementing bounded-depth breadth-first traversal over an external graph (e.g. GitHub social graph) for sampling, expansion, or seed-based exploration — frontier management, deduplication, depth/budget caps, interleaved fetch + expand, and partial-result handling. Activates when the user mentions BFS, breadth-first, frontier, depth-limited, sampling, or seed node, especially in the context of GitHub or social graphs.
---

# BFS Sampling Strategy — Practices

Applies to `requirements.md` Phase 1: depth ≤ 2 BFS from the Seed Node over the GitHub social graph (FR03, FR06).

## 1. Frontier model

- Maintain three sets, never one list:
  - `visited: set[str]` — every node already expanded **or** currently fetching.
  - `frontier: list[str]` — the current depth's nodes to expand next.
  - `next_frontier: list[str]` — nodes discovered while expanding `frontier`.
- A node is added to `visited` **at enqueue time**, not at dequeue time. Otherwise the same node enters the frontier multiple times via different paths.

## 2. Depth-limited loop

```python
visited = {seed}
frontier = [seed]
for depth in range(MAX_DEPTH):           # MAX_DEPTH = 2 here
    next_frontier = []
    neighbors_per_node = await fetch_neighbors_bulk(frontier)   # one bounded fan-out
    for node, neighbors in neighbors_per_node.items():
        for nbr in neighbors:
            if nbr not in visited:
                visited.add(nbr)
                next_frontier.append(nbr)
    frontier = next_frontier
    if not frontier:
        break
```

- Fetch the **whole frontier** in parallel (bounded by a semaphore — see `api-rate-limit-resilience`), not one node at a time. The whole point of BFS layered execution is per-layer parallelism.
- The depth boundary is firm: at `depth == MAX_DEPTH - 1` you still expand, at `depth == MAX_DEPTH` you stop. Don't spend budget on a layer you'll never traverse.

## 3. Per-node truncation

- For each expanded node, fetch only the **first K** of each connection (`first: 100` for repos, follows, collaborators). Record `truncated: bool` per node.
- Don't paginate to completion at depth 2 — the cost is quadratic in K and you already have enough signal for affinity scoring.
- For high-degree nodes (a celebrity user with 50k followers), additional truncation: skip expansion past degree threshold, or sample uniformly.

## 4. Edge collection during traversal

- BFS has two outputs, not one:
  1. The set of **users** discovered (input to the recommendation candidate set).
  2. The set of **edges** (user↔repo, user→following, user↔collaborator) to feed graph construction.
- Collect both during the same fetch — don't re-walk later.

## 5. Budget interlock

- Track API points spent. Before expanding `next_frontier`, estimate the cost (number of nodes × per-node query cost). If the estimate exceeds remaining budget, sample the frontier (random or by degree) instead of dropping the whole layer.
- Always return partial results with a `truncated_reason` field (`"depth"`, `"budget"`, `"rate_limit"`) rather than 500-ing.

## 6. Deduplication across paths

- Without `visited` containment at enqueue time, a popular user reachable through 50 paths becomes 50 fetches before any cache write — even with response caching.
- Combine with **request coalescing** (`api-rate-limit-resilience` §4) so concurrent BFS branches that hit the same node still only fetch once.

## 7. Seed validation (FR02)

- Before starting BFS, validate the seed user exists. A 404 on the seed is a deterministic failure — return early with a 404 to the client, never start the traversal.
- Cache "user does not exist" briefly (e.g. 60s) so retries don't hammer GitHub.

## 8. Termination

- BFS terminates on: depth reached, frontier empty, budget exhausted, or upstream circuit open.
- Return `{users, edges, depth_reached, truncated_reason, points_spent}`. Down-stream phases (graph build, scoring) treat this as the contract.

## 9. Anti-patterns to flag

- Recursive DFS where BFS was specified — wrong order of expansion, harder to bound budget.
- Adding to `visited` only when popping — lets the same node enter the frontier N times via N paths.
- Sequential `for node in frontier: await fetch(node)` — kills parallelism, multiplies wall-clock time by frontier size.
- Unbounded pagination per node — at depth 2 this explodes.
- No truncation marker — caller can't tell whether the missing recommendations are absent or just unfetched.
- Mixing the seed-validation request into the BFS loop — slow path for the deterministic 404 case.
