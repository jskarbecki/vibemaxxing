## What changed, and why

<!-- Readable without opening the diff. -->

## Checks

<!-- CI runs this; paste what it printed, or say which parts you skipped and why. -->

```
uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```

- [ ] I read `docs/CONTRACT.md` for the area I touched
- [ ] Behaviour a doc describes changed, and I updated that doc here
- [ ] No new runtime dependency (or I opened an issue first)
