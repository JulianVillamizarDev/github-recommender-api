"""App `recommendations`: motor de recomendación de usuarios de GitHub.

Reúne el pipeline de cuatro fases (extracción BFS, modelado del grafo, cálculo
de afinidad y motor de recomendación) que expone el endpoint `POST /api/recommend`.
"""
