# FraudTrace - architecture

## 1. Hackathon brief -> design
| Brief | How it is met |
|---|---|
| Acts on connected data | 17 MCP tools: multi-hop traversal, FalkorDB algorithms, and 3 action tools that write back to the graph |
| Remembers what it learns | one isolated `memory_<analyst>` graph per analyst: findings, investigations, feedback, supersede history |
| Shows how it got to each answer | every tool result carries the Cypher that ran and a sub-graph; the verdict lists evidence paths, queries, memory used, actions |
| FalkorDB primary + central | all data, all memory, all algorithms live in FalkorDB; nothing else stores state |

## 2. Components
- **FalkorDB** - three kinds of graph: `fraud_data` (shared), `memory_alice`, `memory_bob` (one per analyst, created on demand for allow-listed analysts).
- **MCP server** (`mcp_server/server.py`, official `mcp` SDK / FastMCP) - publishes the tools with JSON schemas and read-only / write annotations. Streamable HTTP in Docker, stdio for local runs and tests.
- **LLM client** (`backend/llm.py`) - an Anthropic-shaped `messages.create()` adapter over any OpenAI-compatible API (Groq, Gemini, OpenRouter, ...), with retries and token trimming for free tiers; the agent loop does not know which provider it talks to.
- **Agent** (`backend/agent.py`) - discovers tools over MCP, converts them to Claude tool definitions, loops *model picks a tool -> we call it over MCP -> result goes back* until the model calls `submit_verdict` (max 14 steps, then it is forced to). Every step is emitted as an event.
- **API** (`backend/main.py`) - `POST /api/investigate` streams Server-Sent Events; plus `/api/memory/{analyst}`, `/api/feedback`, `/api/scores`, `/api/health`.
- **Frontend** - analyst selector, chat, live timeline, force-directed graph (evidence highlighted, memory-sourced nodes marked, risk colours), verdict card, Cypher accordion, replay, feedback buttons.

## 3. Graphs
`fraud_data`: `Person, Account, Device, IPAddress, Merchant, Transaction`; `OWNS, USED_DEVICE, LOGGED_IN_FROM, SENT, TO`; actions add `Case` and `INCLUDES`, plus `status`/flag properties.
Derived inside FalkorDB by `shared/projection.py` (needed because native algorithms want edges between nodes of the *same* label):
`(Account)-[:PAID {count,total}]->(Account)` money flow, and `(Account)-[:LINKED {weight,why}]->(Account)` identity similarity
(+2 per shared device, +1 per shared IP, +1 shared address, +2 for 3+ repeated transfers; kept at weight >= 3). They are heuristics that
*point* the agent somewhere; it must still confirm with raw evidence.

`memory_<analyst>`: `Investigation -[:FOUND]-> Finding -[:ABOUT]-> Entity`, `Feedback -[:ON]-> Finding`, `Finding -[:SUPERSEDES]-> Finding`, `Investigation -[:FOLLOWS_UP]-> Investigation`.
A new finding about the same set of entities supersedes the old one; the old one is kept with its timestamp.

## 4. Tools (all served over MCP)
Read: `get_account_profile`, `get_transaction_links`, `get_neighbors`, `find_shared_entities`, `shortest_path` (`algo.SPpaths`),
`detect_communities` (`algo.WCC` on `LINKED`), `rank_suspicious_nodes` (`algo.pageRank` / `algo.betweenness`), `detect_cycles`
(bounded paths over `PAID`), `find_mule_candidates`, `run_readonly_cypher`, `score_account`.
Memory: `recall_past_findings`, `save_finding`, `record_feedback`. Actions: `flag_accounts`, `hold_transaction`, `create_case`.

## 5. Risk score (deterministic, explainable)
`final = clamp(base graph score + memory adjustment, 0, 100)`. Base = weighted graph signals (shared devices/IPs, loops, mule pattern, ...).
Memory adjustment = this analyst's feedback on findings about the account: false positive **-35**, confirmed **+10**, clamped to [-50, +20].
The backend overwrites the model's `risk_score` with the maximum `score_account` result, so the number comes from the graph + memory, not from the LLM.

## 6. Security choices
- **Tenant isolation** - `analyst_id` and `investigation_id` are *removed from the schema shown to the model* and injected by the backend from the logged-in analyst. The model cannot address another analyst's graph. Analyst ids are allow-listed and must match `[a-z0-9_]{2,32}` (`memory_graph_name`). Covered by `test_tenant_isolation`.
- **Read-only Cypher guard** (`shared/guard.py`) - strips comments/strings, rejects `CREATE/MERGE/DELETE/SET/REMOVE/CALL/...`, multiple statements, unbounded `[*]` paths, over-long queries; adds a `LIMIT`. Then executes with `GRAPH.RO_QUERY` (FalkorDB itself refuses writes) and a 5 s timeout, and can only see `fraud_data`.
- **Evidence gates** - action tools need cited evidence, and the backend refuses any action before at least one read tool has returned data; the tools themselves refuse accounts whose graph evidence score is below 30. The agent is told to accept refusals.
- **Secrets** - API key only from the environment (`.env`, git-ignored). Backend container runs as a non-root user. CORS limited to the two UI origins.
- **Synthetic data only** - no real PII.

## 7. Questions a judge might ask
- *Why not Postgres?* The core questions are multi-hop and variable-depth (a 12-account money loop); in SQL that is a recursive CTE per question. Here it is one bounded path pattern, and the algorithms run natively.
- *Why MCP instead of function calling?* The tools are discoverable, typed and reusable by any MCP client; the agent has no hard-coded flow, it picks tools from the server's list.
- *Isn't the risk score just LLM opinion?* No - see section 5.
- *How do you know the agent finds real patterns?* The generator plants three patterns and writes an answer key outside the graph; tests check each tool against it (`test_communities_recover_ring_and_synthetic_identities`, `test_cycle_detection_finds_the_12_hop_loop`, `test_mule_candidates_match_ground_truth`).
- *What stops the agent from taking a harmful action?* Section 6, "evidence gates". Actions here only flag/hold/open a case in a demo graph.
- *What if the model hallucinates an id?* Tools validate ids against the graph and return errors; the prompt forbids inventing ids.

## 8. Known limitations (be upfront)
- Synthetic data; thresholds and weights are hand-tuned, not learned.
- `LINKED` / `PAID` projections are built at seed time and are not refreshed when new transactions arrive.
- Tenant isolation is by graph name inside one FalkorDB instance; there is no user authentication - analysts are selected in the UI.
- The live-model path (real Groq/Claude calls) is exercised only manually; the automated suite uses scripted and mock models. Free models are less reliable at long tool-use chains than Claude, and free tiers rate-limit.
