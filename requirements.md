# Functional Requirements (FR)

- FR01 - Request Reception: The system must provide an API endpoint capable of receiving HTTP POST requests from the web client, accepting the GitHub username (Seed Node) as the primary parameter.
- FR02 - Input Validation: The system must verify the existence and availability of the entered profile by querying the public GitHub API. If the profile does not exist, it must return an appropriate error message.
- FR03 - Topology Extraction: The system must be able to extract the user's list of repositories, main programming languages, direct collaborators, and "Following" profile list.
- FR04 - Network Construction: The system must transform the extracted information into a data structure based on nodes and connections (Graph).
  FR05 - Affinity Measurement: The system must mathematically calculate a compatibility score between 0.0 and 1.0 for each pair of connected users, based on their shared code and programming languages.
  FR06 - Candidate Search: The system must traverse the built network to find indirect users (those who have no direct collaboration with the Seed Node), ordering them by their propagated affinity level.
  FR07 - Profile Filtering: The system must ensure that no recommended user belongs to the Seed Node's current "Following" list.
  FR08 - Result Return: The system must package the best candidates (Top N) along with their profile data and affinity percentage in a JSON format, and send them back to the web client.

# 2. Implementation and Algorithms per PhaseTo fulfill the described requirements, the backend will implement a series of algorithms divided into the following processing stages:

## Phase 1: Data Extraction

Implementation: Native Python HTTP requests will be used to consume the GitHub GraphQL API. A nested query approach will be utilized to retrieve the maximum amount of relationship data in a single request, taking care not to exceed the platform's rate limits.
Extraction Algorithm: Breadth-First Search (BFS) Sampling. The algorithm will explore the network starting from the Seed Node level by level, limiting the search depth to a maximum of 2 hops to maintain efficient response times.

## Phase 2: Graph Modeling

Implementation: The specialized Python library NetworkX will be used to instantiate and manipulate the network topology within the server's memory.

### Construction Algorithms:

- 1. Bipartite Graph Projection: Initially, "User" and "Repository" nodes will be created. Through network algebra operations, the system will project this to create direct links exclusively between "Users" who share at least one repository.
- 2. Subgraph Extraction: Concurrently, the list of users currently followed by the Seed Node will be stored in a Set data structure.

## Phase 3: Weight Calculation (Technical Affinity)

Vector calculations will be performed leveraging Python's mathematical libraries.

Measurement Algorithms: The final weight of each connection will require three mathematical steps:

- Cosine Similarity: This spatial metric will be used to compare the programming language vectors of two users. It will output a compatibility value from 0.0 to 1.0.
- Min-Max Normalization: This statistical scaling formula will be applied to take the total number of shared repositories between two users and map it proportionally to a scale from 0.0 to 1.0.
- Weighted Sum: The system will merge the language score and the shared repository score, assigning a predefined importance percentage to each, resulting in the final probabilistic weight of the connection.

## Phase 4: Recommendation and Filtering Engine

### Implementation:

NetworkX will be used for pathfinding, coupled with standard Python logical operations for data cleansing.

### Recommendation Algorithms:

- Negative Logarithmic Transformation: Before traversal, the mathematical formula $-\log(W)$ will be applied to all weights calculated in Phase 3. This inverts the scale, transforming high probabilities into "low distance costs."
- Dijkstra's Algorithm: This classic shortest-path algorithm will be executed starting from the Seed Node. Thanks to the prior mathematical transformation, when Dijkstra searches for the "shortest" path, it will actually be finding the route that maximizes the multiplication of the original affinities, effectively solving the trust propagation problem.
- Boolean Mask (Exclusion Filter): A set difference operation (Candidates - Following) will be applied to the resulting list from Dijkstra against the "Following" list obtained in Phase 2. This guarantees that the final recommendations are entirely undiscovered profiles for the consulting user.
