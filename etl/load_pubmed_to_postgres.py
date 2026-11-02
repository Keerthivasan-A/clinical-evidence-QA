"""Load raw PubMed XML batches into Postgres.

Reads data/raw/*.xml.gz (written by etl.bioasq_download_pubmed_docs), parses
them with PubMedParser and upserts into articles, authors and mesh_terms.
No network access. Re-running is safe: rows are keyed on pmid, so articles are
updated in place and their authors / MeSH terms are replaced.

Connection comes from DATABASE_URL, e.g.
    postgresql://postgres:postgres@localhost:5432/clinical_qa

Start a local database with:
    docker run -d --name pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=clinical_qa \
        -p 5432:5432 pgvector/pgvector:pg17

Run from the project root:
    uv run python -m etl.load_pubmed_to_postgres
"""
import gzip
import os
from pathlib import Path

import psycopg
import structlog

from etl.pubmed_parser import PubMedArticle, PubMedParseError, PubMedParser
from logs.logger_config import setup_production_logging

logger = structlog.get_logger(__name__)

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/clinical_qa"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    pmid              TEXT PRIMARY KEY,
    title             TEXT,
    abstract          TEXT,
    journal           TEXT,
    pub_year          INTEGER,
    doi               TEXT,
    publication_types TEXT[] NOT NULL DEFAULT '{}',
    chemicals         TEXT[] NOT NULL DEFAULT '{}',
    keywords          TEXT[] NOT NULL DEFAULT '{}',
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS authors (
    pmid         TEXT NOT NULL REFERENCES articles(pmid) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    last_name    TEXT,
    fore_name    TEXT,
    affiliations TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (pmid, position)
);
CREATE TABLE IF NOT EXISTS mesh_terms (
    pmid        TEXT NOT NULL REFERENCES articles(pmid) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    descriptor  TEXT NOT NULL,
    ui          TEXT,
    major_topic BOOLEAN NOT NULL,
    qualifiers  TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (pmid, position)
);
"""

UPSERT_ARTICLE_SQL = """
INSERT INTO articles (pmid, title, abstract, journal, pub_year, doi,
                      publication_types, chemicals, keywords)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (pmid) DO UPDATE SET
    title = EXCLUDED.title,
    abstract = EXCLUDED.abstract,
    journal = EXCLUDED.journal,
    pub_year = EXCLUDED.pub_year,
    doi = EXCLUDED.doi,
    publication_types = EXCLUDED.publication_types,
    chemicals = EXCLUDED.chemicals,
    keywords = EXCLUDED.keywords,
    loaded_at = now()
"""

INSERT_AUTHOR_SQL = (
    "INSERT INTO authors (pmid, position, name, last_name, fore_name, affiliations) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_MESH_SQL = (
    "INSERT INTO mesh_terms (pmid, position, descriptor, ui, major_topic, qualifiers) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)


def create_schema(conn: psycopg.Connection) -> None:
    """Create the tables if they do not exist yet."""
    conn.execute(SCHEMA_SQL)
    conn.commit()


def _article_row(a: PubMedArticle) -> tuple:
    return (a.pmid, a.title, a.abstract, a.journal, a.pub_year, a.doi,
            a.publication_types, a.chemicals, a.keywords)


def _author_rows(a: PubMedArticle) -> list[tuple]:
    return [(a.pmid, i, au.name, au.last_name, au.fore_name, au.affiliations)
            for i, au in enumerate(a.authors)]


def _mesh_rows(a: PubMedArticle) -> list[tuple]:
    return [(a.pmid, i, m.descriptor, m.ui, m.major_topic, m.qualifiers)
            for i, m in enumerate(a.mesh_terms)]


def upsert_articles(conn: psycopg.Connection, articles: list[PubMedArticle]) -> None:
    """Upsert articles and replace their authors and MeSH terms (one transaction).

    A PMID repeated within the list keeps its last version.
    """
    unique = list({a.pmid: a for a in articles}.values())
    pmids = [a.pmid for a in unique]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(UPSERT_ARTICLE_SQL, [_article_row(a) for a in unique])
        cur.execute("DELETE FROM authors WHERE pmid = ANY(%s)", (pmids,))
        cur.execute("DELETE FROM mesh_terms WHERE pmid = ANY(%s)", (pmids,))
        cur.executemany(INSERT_AUTHOR_SQL, [r for a in unique for r in _author_rows(a)])
        cur.executemany(INSERT_MESH_SQL, [r for a in unique for r in _mesh_rows(a)])


def load_file(conn: psycopg.Connection, parser: PubMedParser, path: Path) -> int:
    """Parse one raw batch file and upsert its articles.

    Returns:
        Number of articles written.

    Raises:
        PubMedParseError: If the file cannot be parsed.
    """
    with gzip.open(path, "rb") as f:
        articles = list(parser.parse(f))
    upsert_articles(conn, articles)
    return len(articles)


def main() -> None:
    files = sorted(RAW_DIR.glob("batch_*.xml.gz"))
    logger.info("raw_files_found", count=len(files), raw_dir=str(RAW_DIR))
    if not files:
        return

    parser = PubMedParser()
    failed_files: list[str] = []
    written = 0
    with psycopg.connect(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)) as conn:
        create_schema(conn)
        for i, path in enumerate(files, start=1):
            structlog.contextvars.bind_contextvars(file=path.name, file_no=i, n_files=len(files))
            try:
                n = load_file(conn, parser, path)
                written += n
                logger.info("file_loaded", articles=n)
            except (PubMedParseError, OSError, psycopg.Error):
                logger.exception("file_load_failed")
                failed_files.append(path.name)
            finally:
                structlog.contextvars.clear_contextvars()

    logger.info("load_done", written=written, skipped=parser.skipped, failed_files=len(failed_files))


if __name__ == "__main__":
    setup_production_logging()  # once, at the application entry point
    main()
