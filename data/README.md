# Collected monitoring data

The first successful GitHub Actions run creates the baseline. Each later run appends JSONL files:
- observations.jsonl: observed page metadata and content hash (not full page text)
- changes.jsonl: NEW_PAGE or CONTENT_CHANGED events
- runs.jsonl: per-source SUCCEEDED / FAILED status
- last_seen.json: last-known hashes for comparison

A content hash may change because of a navigation/footer update, not a new model or price. All yacht prices, options and inventory remain NOT_COLLECTED until explicitly verified from source documents. Failures do not overwrite the previous baseline.
