# Field boost weights applied per matched lexical term.
RERANK_FIELD_WEIGHTS = {
    "header_text": 0.15,
    "section_path": 0.12,
    "entity_terms": 0.10,
}

# Added per matched term, capped at 4 (rewards multi-term overlap).
RERANK_OVERLAP_WEIGHT = 0.08

# Per-kind score adjustments on top of the raw cross-encoder score.
RERANK_KIND_BONUS: dict[str, float] = {
    "text": 0.12,
    "section_summary": 0.05,
    "summary": 0.05,
}

# Applied when the query is in table mode.
RERANK_TABLE_MODE_BONUS: dict[str, float] = {
    "table": 0.20,
    "table_rows": 0.20,
}

# Penalty for summary/section_summary chunks with zero lexical term overlap.
# Prevents low-relevance summaries from surviving purely on cross-encoder score.
RERANK_SUMMARY_ZERO_OVERLAP_PENALTY = 0.12
