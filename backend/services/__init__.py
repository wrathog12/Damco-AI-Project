"""
Async service layer over Postgres + Qdrant.

`schemes` holds the queries, `resources` the loop-bound clients they need, and
`runner` the event loop those clients live on. The synchronous functions in
`tools/` are shims over this; nothing in here knows about tool calling, JSON
serialisation or the LLM.
"""
