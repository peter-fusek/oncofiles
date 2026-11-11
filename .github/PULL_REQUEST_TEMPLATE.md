## What

<!-- Brief description of the changes -->

## Why

<!-- Link to issue(s): Closes #... -->

## Checklist

- [ ] Tests pass (`uv run pytest`)
- [ ] Linting passes (`uv run ruff check`)
- [ ] No secrets committed
- [ ] Changes what a parser or rule extracts (`curated/facts_rules.py`, `indexer.py`, `fact_codes` aliases)? Then bump `INDEX_VERSION` in this PR, so production never holds facts from two parser generations (#672)
- [ ] Regression against the production DB baseline, not code vs code: per-doc lost / gained / changed counts in the PR text (no values)
