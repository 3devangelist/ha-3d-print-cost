# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest && ruff check . && ruff format --check .
```

Ground rules (they are what keeps the costs trustworthy):

1. **Never write to Spoolman** and never re-implement HaspelSync (slot mapping, G-code parsing, booking).
2. **Never invent an `entity_id`**: discover entities by device, platform, `translation_key` or `unique_id`.
3. **Business logic stays free of Home Assistant imports** (`models/`, `units.py`, `tracker.py`, `filament.py`) and is unit-tested.
4. **Every external system sits behind an adapter** that can be mocked; all I/O is async with a timeout.
5. **Unknown is never zero**: missing data yields `None` plus a note, not a silently wrong number.
6. **Money uses `Decimal`**, rounded only for display.
7. **Electricity price is never assumed constant** during a print.
8. A print in progress must survive a restart or reload, and repeated states must never create duplicate jobs.
9. Never log or expose an API key.
