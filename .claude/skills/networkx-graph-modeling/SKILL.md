---
name: networkx-graph-modeling
description: Use when modeling, building, transforming, or serializing graphs with NetworkX — choosing between Graph/DiGraph/MultiGraph, bipartite graphs and projections, node/edge attributes, subgraph extraction, weighted graphs, and large-graph memory considerations. Activates on `import networkx`, on graph-construction code, or when the user mentions NetworkX, bipartite projection, graph topology, nodes, edges, or adjacency.
---

# NetworkX Graph Modeling — Practices

Applies to graph construction in `requirements.md` Phase 2 (FR04: bipartite User↔Repository graph; projection to User↔User graph; subgraph for Following set).

## 1. Choosing the graph type

- `nx.Graph` — undirected, simple. Use for the **User↔User affinity graph** (collaboration is symmetric).
- `nx.DiGraph` — directed. Use for the **Following relationship** (FR07) since it is asymmetric.
- `nx.MultiGraph` / `nx.MultiDiGraph` — only when multiple parallel edges carry distinct semantics. Almost never what you want; collapse parallels into edge weights instead.
- The bipartite **User↔Repository** graph in Phase 2 is a plain `nx.Graph` with a `bipartite` node attribute (`0` for users, `1` for repos).

## 2. Building the bipartite graph

```python
import networkx as nx

B = nx.Graph()
B.add_nodes_from(user_logins, bipartite=0, kind="user")
B.add_nodes_from(repo_ids,    bipartite=1, kind="repo")
B.add_edges_from(
    (login, repo_id)
    for login, repos in user_repos.items()
    for repo_id in repos
)
```

- Use **stable, hashable IDs** for nodes (`login` string for users, `nameWithOwner` or numeric repo id for repos). Don't use mutable dicts.
- Store metadata as **node/edge attributes**, not parallel dicts: `B.nodes[login]["languages"] = {...}`, `B.edges[u, v]["weight"] = 0.83`.

## 3. Bipartite projection (FR04 step 1)

- `nx.bipartite.weighted_projected_graph(B, user_nodes)` — edge weight = number of shared repos. Cheap, good baseline.
- `nx.bipartite.overlap_weighted_projected_graph(B, user_nodes)` — Jaccard-like overlap.
- For the project's weighted-sum (Phase 3), use the **count** projection and recompute edge weight afterwards rather than picking a built-in projection that bakes in an opinion.
- **Cost warning:** projection is O(Σ deg(repo)²). One mega-repo with 50k contributors will dominate runtime — drop or down-weight repos above a contributor threshold.

## 4. Subgraph and set operations

- Following set (FR07): keep as a plain `set[str]` — set difference is O(|A|), much faster than walking a NetworkX `DiGraph`.
- `G.subgraph(nodes)` returns a **read-only view**. Use `G.subgraph(nodes).copy()` if you need to mutate.
- `nx.ego_graph(G, seed, radius=2)` for the depth-2 BFS neighborhood (matches Phase 1's BFS sampling).

## 5. Edge weights and attributes

- Store affinity components on the edge so they survive transformation:
  ```python
  G.edges[u, v].update(
      cosine=0.71, repo_overlap=0.42, weight=0.58, log_weight=0.544
  )
  ```
- Algorithms read from the `weight` keyword by default (`nx.shortest_path(G, weight="weight")`); be explicit which attribute the algorithm should use.

## 6. Memory & performance

- A user node with `dict` of language counts is fine for thousands of users; for millions, switch to a sparse matrix (`scipy.sparse.csr_matrix`) with a parallel `id ↔ index` map and skip NetworkX entirely.
- Avoid `nx.adjacency_matrix(G)` on graphs above ~100k nodes unless you need it — it's dense by default. Use `nx.to_scipy_sparse_array(G, weight="weight")`.
- `len(G)` is O(1); `G.number_of_edges()` is O(1); `G.degree(n)` is O(1). But `nx.shortest_path` with a weight is O((V+E) log V) per source — never call it in a loop without caching.
- NetworkX is single-threaded pure Python. For tight loops, drop down to `scipy.sparse` + `numpy`; use NetworkX as the **modeling layer**, not the **compute layer**.

## 7. Serialization

- For debugging / hand-off: `nx.write_gexf(G, "graph.gexf")` (Gephi-friendly) or `nx.node_link_data(G)` → JSON.
- For pipeline persistence: pickle is fine in-process, but version-fragile. Prefer GEXF or GraphML across runs.

## 8. Anti-patterns to flag

- Using `MultiGraph` to "store more info" — push it onto edge attributes instead.
- Recomputing the projection on every request — cache the User↔User graph and invalidate on data refresh.
- Calling `nx.shortest_path_length(G, source=u, target=v)` inside a loop over candidate `v` — call `nx.single_source_dijkstra_path_length(G, u)` once and look up.
- Storing node IDs as objects with `__hash__` based on identity — surprises across processes.
- Using `G.nodes()` repeatedly in hot loops; bind it once: `nodes = list(G.nodes())`.
