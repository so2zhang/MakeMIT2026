# Vultr Demo Stack

## What is implemented

- **Vultr Compute VM** hosts backend + dashboard:
  - `http://104.207.143.159:8000`
- **Vultr Managed PostgreSQL** stores sessions/events/fingerprints.
- Live ingestion API for glove telemetry.
- Fingerprint generation on session stop.
- Hosted dashboard showing recent sessions and fingerprints.

## Backend files

- `/Users/patliu/Desktop/Coding/MakeMIT2026/vultr_backend.py`
- `/Users/patliu/Desktop/Coding/MakeMIT2026/vultr_schema.sql`
- `/Users/patliu/Desktop/Coding/MakeMIT2026/vultr_templates/index.html`
- `/Users/patliu/Desktop/Coding/MakeMIT2026/vultr_ingest_client.py`

## Deployment scripts

- `/Users/patliu/Desktop/Coding/MakeMIT2026/init_vultr_db.py`
- `/Users/patliu/Desktop/Coding/MakeMIT2026/deploy_vultr_vm.py`

## API quick reference

- `POST /api/session/start`
  - body: `{"performer_id":"name"}`
- `POST /api/session/<session_id>/ingest`
  - body: `{"events":[ ... ]}`
- `POST /api/session/<session_id>/stop`
- `GET /api/sessions/recent`
- `GET /health`

## Live ingest runner (from glove stream)

```bash
python3 /Users/patliu/Desktop/Coding/MakeMIT2026/vultr_ingest_client.py \
  --port /dev/cu.usbserial-0001 \
  --api-base http://104.207.143.159:8000 \
  --performer-id makeMIT-demo
```

