# andai/services/intake/andai_intake

Python package of the andai-intake service.

- `main.py` — the FastAPI app (`/healthz`); its lifespan starts the SMTP receiver and the purge loop
  when `ANDAI_DB_URL_APP` is set.
- `settings.py` — andai-core's settings plus `ANDAI_INTAKE_*` (addresses, bind, size cap, retention).
- `smtp.py` — the aiosmtpd handler: recipient check (550), recipient cap (452), size cap (552), store
  then 250, 451 on any store failure.
- `store.py` — `save()`: one `inbound_mail` row through `andai_store_inbound`, deduplicated on the
  raw bytes within 24 hours.
- `purge.py` — raw bytes nulled after 7 days, rows deleted after 30 (`andai_purge_inbound`).
- `replay.py` — `python -m andai_intake.replay`: send captured `.eml` files over SMTP.
