# Tests

Unit tests for the ETL pipeline, the API clients and logging. Run from the project root:

```bash
uv run pytest -v
```

Everything runs offline and in about a second. One test needs a real Postgres and is skipped unless you configure it
(see [Postgres test](#postgres-test)).

In PyCharm, set the default test runner to **pytest** (Settings > Tools > Python Integrated Tools).

## Which test covers what

Each test file is named after the module it tests, with `_test` at the end.

| Test file | Module under test | What it checks |
|-----------|-------------------|----------------|
| `extract_bioasq_questions_test.py` | `etl/extract_bioasq_questions.py` | PMID extraction from URLs, Excel output, log events |
| `extract_bioasq_pmids_test.py` | `etl/extract_bioasq_pmids.py` | sampling per question type, flattening and de-duplicating PMIDs, missing input file |
| `download_pubmed_xml_test.py` | `etl/download_pubmed_xml.py` | batching, resume, failed batches, raw file saving and numbering, log events |
| `load_pubmed_to_postgres_test.py` | `etl/load_pubmed_to_postgres.py` | row building, upsert logic, per-file failure handling, log events, real Postgres upsert |
| `pubmed_parser_test.py` | `etl/pubmed_parser.py` | field extraction, bad or malicious XML, counters, parser logging |
| `base_api_client_test.py` | `api_clients/base_api_client.py` | URL building, hooks, retry on 429/5xx/timeouts, no retry on 4xx |
| `pubmed_api_client_test.py` | `api_clients/pubmed_api_client.py` | env settings, rate-limit delay, efetch and esearch requests |
| `logger_config_test.py` | `logs/logger_config.py` | JSON line output, per-job log files, `logged_job` context and failure logging |
| `settings_test.py` | `settings.py` | `.env` loading, shell variables win, required variables |

Supporting files:

| File | Purpose |
|------|---------|
| `conftest.py` | Loads `.env` before the tests, so `TEST_DATABASE_URL` can live there. |
| `data/pubmed_sample.xml` | Three real PubMed articles (24086431, 22623959, 23587639) used by the parser and loader tests. |

## How the tests avoid the outside world

| Dependency | How it is replaced |
|------------|--------------------|
| NCBI | `FakeClient` returns canned responses, so nothing touches the network |
| HTTP session | `Recorder` replaces `session.request` and records the calls |
| Retry waits | `wait_none()` removes the backoff sleep |
| Postgres | `FakeConn` / `FakeCursor` record the SQL; the real database is only used by one test |
| Files | pytest `tmp_path` folders, so no test writes into `data/` |
| Environment | `monkeypatch.setenv` / `delenv`, so your own `.env` values do not change results |

## Postgres test

`test_upsert_is_idempotent_in_real_postgres` loads the sample articles twice into a real database and checks that the row
counts do not change. It is skipped when `TEST_DATABASE_URL` is not set.

1. Create an empty throwaway database, for example `clinical_qa_test`.
2. Put its URL in `.env` (see `.env.example`):
   `TEST_DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/clinical_qa_test`
3. Run `uv run pytest -v tests/load_pubmed_to_postgres_test.py`.

**This test drops and recreates `articles`, `authors` and `mesh_terms`.** Never point it at the database you use for real
data (`DATABASE_URL`).

## Testing log output

Log events are asserted with `structlog.testing.capture_logs()`, which collects the events without needing a log file:

```python
with structlog.testing.capture_logs() as logs:
    downloader.run()
assert [e["event"] for e in logs] == ["download_started", "batch_done", "download_done"]
```

Tests that check the real log file use `setup_production_logging(tmp_path / "app.log", cache_logger=False)` and reset
logging afterwards (see `logger_config_test.py`).

## Adding a test

- Put it in the file named after the module it tests. For a new module `etl/foo.py`, create `tests/foo_test.py`.
- Name tests `test_<behaviour>`, one behaviour per test.
- Do not call the network or write outside `tmp_path`. Inject fakes through the constructor arguments the classes provide.
- Do not call `setup_production_logging()` at module level in a test, only inside a test that needs it.
