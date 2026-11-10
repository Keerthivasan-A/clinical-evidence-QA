# ETL: BioASQ questions to PubMed articles in Postgres

This folder turns the BioASQ question set into a Postgres database of PubMed articles, ready for the retrieval
experiments (sparse, dense, hybrid) that come next.

Run everything **from the project root** as modules (`python -m etl.<script>`), never `python etl/<script>.py`. The
imports and the log folder depend on it.

## Run order

```
 data/BioASQ_raw.json
        │  1  extract_bioasq_questions
        ▼
 data/BioASQ_extracted.xlsx
        │  2  extract_bioasq_pmids
        ▼
 data/pmids_to_fetch.txt
        │  3  download_pubmed_xml        (network: NCBI efetch)
        ▼
 data/raw/batch_0001.xml.gz, batch_0002.xml.gz, ...
        │  4  load_pubmed_to_postgres    (no network: parses with pubmed_parser)
        ▼
 Postgres: articles, authors, mesh_terms
        │  5  export_pubmed_jsonl        (no network: joins the three tables)
        ▼
 data/pubmed_docs.jsonl                  (one flat document per article, for Pinecone)
```

Each step reads the file the previous step wrote. That file is the only hand-off between steps, so you can stop, inspect
or rerun any step on its own.

| Step | Run | Reads | Writes | Needs |
|------|-----|-------|--------|-------|
| 1 | `uv run python -m etl.extract_bioasq_questions` | `data/BioASQ_raw.json` | `data/BioASQ_extracted.xlsx` | the BioASQ JSON (see [data/README.md](../data/README.md)) |
| 2 | `uv run python -m etl.extract_bioasq_pmids` | `data/BioASQ_extracted.xlsx` | `data/pmids_to_fetch.txt` | step 1 |
| 3 | `uv run python -m etl.download_pubmed_xml` | `data/pmids_to_fetch.txt` | `data/raw/batch_XXXX.xml.gz`, `data/pmids_downloaded.txt`, `data/pmids_failed.txt` | internet, `.env` |
| 4 | `uv run python -m etl.load_pubmed_to_postgres` | `data/raw/*.xml.gz` | tables in Postgres | a running Postgres, `.env` |
| 5 | `uv run python -m etl.export_pubmed_jsonl` | `articles`, `authors`, `mesh_terms` in Postgres | `data/pubmed_docs.jsonl` | step 4, `.env` |

`data/BioASQ_extracted.xlsx` and `data/pmids_to_fetch.txt` are committed, so on a fresh clone you can start at step 3.

## One-time setup

```bash
uv sync
cp .env.example .env     # then edit .env: NCBI key, email, DATABASE_URL
```

Secrets and connection strings live only in `.env` (git ignores it). `.env.example` lists every variable. Start a
database for step 4 if you do not have one:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=<password> -e POSTGRES_DB=clinical_qa -p 5432:5432 pgvector/pgvector:pg17
```

Use the same password in `DATABASE_URL`.

## What each step does

**1. `extract_bioasq_questions`** reads every question in the raw BioASQ JSON and writes one Excel row per question:
id, type, the PMIDs of its gold documents and how many there are. Optional flags: `--input`, `--output`.

**2. `extract_bioasq_pmids`** samples 2 questions of each type (`yesno`, `factoid`, `list`, `summary`, fixed random seed so
the sample is repeatable) and writes the unique PMIDs of those questions, one per line. The sample size is
`N_SAMPLES_PER_GROUP` in the file. This keeps the first download small.

**3. `download_pubmed_xml`** requests the PMIDs from NCBI in batches (100 per request) and saves each answer untouched as
a gzipped XML file. It parses nothing. Keeping the raw XML means a parser change never needs a re-download.
- A PMID is added to `data/pmids_downloaded.txt` only after its batch file is safely on disk. Rerunning skips those
  PMIDs, so an interrupted run resumes where it stopped. To force a re-download, delete the PMID from that file.
- A failed batch (network error, bad response) does not stop the run. Its PMIDs go to `data/pmids_failed.txt` and are
  retried on the next run, because they were not marked as downloaded. The file is deleted when a run has no failures.
- Temporary or flaky errors (HTTP 429, 5xx, timeouts) are retried automatically with exponential backoff before a batch is
  counted as failed.
- With `NCBI_API_KEY` set the script waits 0.11 s between requests (10 per second), without it 0.34 s (3 per second).
- Some PMIDs are never returned by NCBI (retracted or invalid). That is normal and not an error.

**4. `load_pubmed_to_postgres`** reads every `data/raw/*.xml.gz`, parses it with `pubmed_parser` and stores the result:

| Table | One row per | Key |
|-------|-------------|-----|
| `articles` | article (title, abstract, journal, year, DOI, publication types, chemicals, keywords) | `pmid` |
| `authors` | author of an article, in listed order | `pmid`, `position` |
| `mesh_terms` | MeSH concept of an article | `pmid`, `position` |

It creates the tables if they are missing and is safe to rerun: articles are updated in place and their authors and MeSH
terms are replaced, all in one transaction per file. A file that cannot be parsed is logged and skipped, the others still
load.

**5. `export_pubmed_jsonl`** joins `articles`, `authors` and `mesh_terms` on `pmid` and writes one JSON document per
line to `data/pubmed_docs.jsonl` (option: `--output`). Authors and MeSH terms are collapsed to one list per article
before the join, so an article never turns into several lines. Each document is flat, because Pinecone metadata only
allows strings, numbers and lists of strings:

| Field | Source |
|-------|--------|
| `_id` | `pmid` |
| `text` | title, a blank line, then the abstract (title only when the abstract is missing or just repeats the title) |
| `title`, `journal`, `doi`, `year` | the article; fields with no value are left out, never written as `null` |
| `publication_types`, `chemicals`, `keywords` | the article's lists |
| `authors` | author names in listed order |
| `mesh_terms`, `mesh_major` | all MeSH descriptors, and those flagged as a major topic |

`loaded_at` is not exported. Articles with neither title nor abstract are left out. A document Pinecone would reject
(a text token over 1,024 bytes, or a document over 2 MB) is skipped and logged as `doc_skipped` with its reason, because
one bad document fails a whole upsert batch. The file is written to a temp name and renamed at the end, so a failed run
never replaces a good file. The result counts (`expected`, `written`, `skipped`) are logged, and `count_mismatch` is
warned if they do not add up. To change the document shape, edit `EXPORT_SQL` in the script and rerun.

## Other files

| File | Purpose |
|------|---------|
| `pubmed_parser.py` | Library, not a script. Streams PubMed XML into `PubMedArticle` objects. Used by step 4. |
| `explore_bioasq_data.ipynb` | Notebook for exploring the BioASQ data (document counts per question, plots). Not part of the pipeline. |

## Logs

Every script writes JSON lines to its own file in the shared `logs/` folder, whatever folder you start it from:

| Script | Log file |
|--------|----------|
| `extract_bioasq_questions` | `logs/extract_bioasq_questions.log` |
| `extract_bioasq_pmids` | `logs/extract_bioasq_pmids.log` |
| `download_pubmed_xml` | `logs/download_pubmed_xml.log` |
| `load_pubmed_to_postgres` | `logs/load_pubmed_to_postgres.log` |
| `export_pubmed_jsonl` | `logs/export_pubmed_jsonl.log` |

Each line carries `job` and `run_id`, so one run is easy to pull out. Useful events: `job_started` / `job_finished` /
`job_failed` (with traceback), `batch_done` / `batch_failed`, `file_loaded` / `file_load_failed`. Example:

```bash
grep '"batch_failed"' logs/download_pubmed_xml.log
```

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `ModuleNotFoundError: No module named 'etl'` | Not run from the project root or not run with `-m`. In PyCharm run it as a module with the working directory set to the project root. |
| `DATABASE_URL is not set` | Create `.env` from `.env.example` and fill it in. |
| `database "..." does not exist` | The database name in `DATABASE_URL` is wrong, or the database was never created. |
| Downloader says `nothing_to_do` | Every PMID is already in `data/pmids_downloaded.txt`. |
| Batches keep failing | See `batch_failed` in the log. Check the connection, and set `NCBI_API_KEY` to avoid rate limits. |
