"""Export PubMed articles from Postgres to a JSONL file, one document per line.

Reads articles, authors and mesh_terms (written by etl.load_pubmed_to_postgres),
joins them on pmid and writes data/pubmed_docs.jsonl. Each line is one flat
document in the shape Pinecone accepts: a string _id, a ranked text field, and
metadata that is only strings, numbers or lists of strings. No network access.

The document shape lives in EXPORT_SQL. To try another shape (different text
field, extra metadata), change that query and rerun: nothing is re-parsed and
nothing is re-downloaded.

The file is written to a temp name and renamed when finished, so a failed run
never leaves a half-written file and never replaces a good one.

The connection string comes from DATABASE_URL in the .env file (see
.env.example).

Run from the project root:
    uv run python -m etl.export_pubmed_jsonl
    uv run python -m etl.export_pubmed_jsonl --output data/other.jsonl
"""
import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
import structlog

from logs.logger_config import log_file_for, logged_job, setup_production_logging
from settings import get_required, load_env

logger = structlog.get_logger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "pubmed_docs.jsonl"
JOB_NAME = "export_pubmed_jsonl"
FETCH_SIZE = 2000  # rows pulled from Postgres per round trip
MAX_TOKEN_BYTES = 1024  # Pinecone rejects a text token longer than this
MAX_DOC_BYTES = 2_000_000  # Pinecone's per-document limit is 2 MB

# Raw string: the SQL itself must see E'\n\n', not a Python newline.
# articles.pmid is the primary key and both child tables cascade from it, so
# there are no duplicate or orphan rows to guard against.
EXPORT_SQL = r"""
WITH a AS (
  SELECT pmid, jsonb_agg(name ORDER BY position) AS authors
  FROM authors
  GROUP BY pmid
),
m AS (
  SELECT pmid,
         jsonb_agg(descriptor ORDER BY position) AS mesh_terms,
         jsonb_agg(descriptor ORDER BY position) FILTER (WHERE major_topic) AS mesh_major
  FROM mesh_terms
  GROUP BY pmid
),
base AS (
  SELECT ar.pmid, ar.journal, ar.pub_year, ar.doi,
         ar.publication_types, ar.chemicals, ar.keywords,
         NULLIF(btrim(ar.title), '') AS t,
         -- some records repeat the title as the abstract: keep the title only
         CASE
           WHEN lower(btrim(ar.abstract, E' .\n\t')) = lower(btrim(ar.title, E' .\n\t')) THEN NULL
           ELSE NULLIF(btrim(ar.abstract), '')
         END AS ab
  FROM articles ar
)
SELECT jsonb_strip_nulls(jsonb_build_object(
         '_id',               b.pmid,
         'text',              concat_ws(E'\n\n', b.t, b.ab),
         'title',             b.t,
         'journal',           NULLIF(btrim(b.journal), ''),
         'year',              b.pub_year,
         'doi',               NULLIF(btrim(b.doi), ''),
         'publication_types', to_jsonb(b.publication_types),
         'chemicals',         to_jsonb(b.chemicals),
         'keywords',          to_jsonb(b.keywords),
         'authors',           COALESCE(a.authors,    '[]'::jsonb),
         'mesh_terms',        COALESCE(m.mesh_terms, '[]'::jsonb),
         'mesh_major',        COALESCE(m.mesh_major, '[]'::jsonb)
       )) AS doc
FROM base b
LEFT JOIN a USING (pmid)
LEFT JOIN m USING (pmid)
WHERE b.t IS NOT NULL OR b.ab IS NOT NULL
ORDER BY b.pmid
"""

# Articles the export is expected to produce: the same filter as EXPORT_SQL.
COUNT_SQL = """
SELECT count(*) FROM articles
WHERE NULLIF(btrim(title), '') IS NOT NULL OR NULLIF(btrim(abstract), '') IS NOT NULL
"""

_TOKEN_SPLIT = re.compile(r"\W+")


@dataclass(frozen=True)
class ExportStats:
    """Counts from one export run.

    Attributes:
        expected: Articles in Postgres that have a title or an abstract.
        written: Documents written to the file.
        skipped: Documents left out because Pinecone would reject them.
    """

    expected: int
    written: int
    skipped: int


class PubMedJsonlExporter:
    """Writes one JSON document per PubMed article to a JSONL file.

    Attributes:
        database_url: Postgres connection string.
        output_path: The JSONL file to write.
        fetch_size: Rows pulled from Postgres per round trip.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        output_path: Path = OUTPUT_PATH,
        fetch_size: int = FETCH_SIZE,
    ) -> None:
        """Set up the exporter.

        Args:
            database_url: Postgres connection string. Defaults to the
                DATABASE_URL environment variable (normally from .env).
            output_path: The JSONL file to write. Its folder is created if
                missing.
            fetch_size: Rows pulled from Postgres per round trip.
        """
        self.database_url = database_url or get_required("DATABASE_URL")
        self.output_path = output_path
        self.fetch_size = fetch_size

    @staticmethod
    def rejection_reason(doc: dict, line: str) -> str | None:
        """Say why Pinecone would reject a document, or None if it is fine.

        One bad document fails a whole Pinecone upsert batch, so these are
        caught here and the document is left out of the file.

        Args:
            doc: The document built by EXPORT_SQL.
            line: The document serialised as one JSON line.

        Returns:
            A short reason code, or None when the document is acceptable.
        """
        text = doc.get("text") or ""
        if not text.strip():
            return "empty_text"
        if len(line.encode("utf-8")) > MAX_DOC_BYTES:
            return "document_too_large"
        if any(len(t.encode("utf-8")) > MAX_TOKEN_BYTES for t in _TOKEN_SPLIT.split(text)):
            return "token_too_long"
        return None

    def run(self) -> ExportStats:
        """Export every article that has text to the output file.

        Streams rows with a server-side cursor, so memory stays flat. If the
        database has nothing to export, the existing output file is left
        untouched.

        Returns:
            Counts of expected, written and skipped documents.
        """
        tmp_path = self.output_path.with_name(self.output_path.name + ".tmp")
        written = skipped = 0
        logger.info("export_started", output=str(self.output_path))

        with psycopg.connect(self.database_url) as conn:
            expected = conn.execute(COUNT_SQL).fetchone()[0]
            if expected == 0:
                logger.warning("no_articles_to_export")
                return ExportStats(expected=0, written=0, skipped=0)

            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with tmp_path.open("w", encoding="utf-8", newline="\n") as out, \
                        conn.cursor(name="export_pubmed_docs") as cur:
                    cur.itersize = self.fetch_size
                    cur.execute(EXPORT_SQL)
                    for (doc,) in cur:
                        line = json.dumps(doc, ensure_ascii=False)
                        reason = self.rejection_reason(doc, line)
                        if reason:
                            skipped += 1
                            logger.warning("doc_skipped", pmid=doc.get("_id"), reason=reason)
                            continue
                        out.write(line + "\n")
                        written += 1
                tmp_path.replace(self.output_path)
            finally:
                tmp_path.unlink(missing_ok=True)  # only exists if the run failed

        if written + skipped != expected:
            logger.warning("count_mismatch", expected=expected, written=written, skipped=skipped)
        logger.info("export_done", expected=expected, written=written, skipped=skipped,
                    output=str(self.output_path))
        return ExportStats(expected=expected, written=written, skipped=skipped)


def main() -> None:
    """Run the exporter with the database from .env."""
    parser = argparse.ArgumentParser(description="Export PubMed articles from Postgres to JSONL.")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="JSONL file to write")
    args = parser.parse_args()

    load_env()
    with logged_job(JOB_NAME):
        PubMedJsonlExporter(output_path=args.output).run()


if __name__ == "__main__":
    setup_production_logging(log_file_for(JOB_NAME))  # once, at the application entry point
    main()
