"""Load raw PubMed XML batches into Postgres.

Reads data/raw/*.xml.gz (written by etl.download_pubmed_xml), parses
them with PubMedParser and upserts into articles, authors and mesh_terms.
No network access. Re-running is safe: rows are keyed on pmid, so articles are
updated in place and their authors / MeSH terms are replaced.

The connection string comes from DATABASE_URL in the .env file (see
.env.example), e.g. postgresql://USER:PASSWORD@localhost:5432/clinical_qa

Start a local database with (use the same password as in DATABASE_URL):
    docker run -d --name pg -e POSTGRES_PASSWORD=<password> -e POSTGRES_DB=clinical_qa \
        -p 5432:5432 pgvector/pgvector:pg17

Run from the project root:
    uv run python -m etl.load_pubmed_to_postgres
"""
import gzip
from pathlib import Path

import psycopg
import structlog

from etl.pubmed_parser import PubMedArticle, PubMedParseError, PubMedParser
from settings import get_required, load_env
from logs.logger_config import log_file_for, logged_job, setup_production_logging

logger = structlog.get_logger(__name__)

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
JOB_NAME = "load_pubmed_to_postgres"

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


class PubMedPostgresLoader:
    """Loads raw PubMed XML batch files into Postgres.

    Attributes:
        database_url: Postgres connection string.
        raw_dir: Folder holding batch_*.xml.gz files.
        parser: Parser shared across files, so its counts cover the whole run.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        raw_dir: Path = RAW_DIR,
        parser: PubMedParser | None = None,
    ) -> None:
        """Set up the loader.

        Args:
            database_url: Postgres connection string. Defaults to the
                DATABASE_URL environment variable (normally from .env).
            raw_dir: Folder holding the raw batch files.
            parser: Parser to use. Defaults to a new non-strict PubMedParser.
        """
        self.database_url = database_url or get_required("DATABASE_URL")
        self.raw_dir = raw_dir
        self.parser = parser or PubMedParser()

    @staticmethod
    def create_schema(conn: psycopg.Connection) -> None:
        """Create the tables if they do not exist yet.

        Args:
            conn: Open Postgres connection.
        """
        conn.execute(SCHEMA_SQL)
        conn.commit()

    @staticmethod
    def article_row(a: PubMedArticle) -> tuple:
        """Build the articles table row for one article.

        Args:
            a: Parsed article.

        Returns:
            Values in the column order of UPSERT_ARTICLE_SQL.
        """
        return (a.pmid, a.title, a.abstract, a.journal, a.pub_year, a.doi,
                a.publication_types, a.chemicals, a.keywords)

    @staticmethod
    def author_rows(a: PubMedArticle) -> list[tuple]:
        """Build the authors table rows for one article.

        Args:
            a: Parsed article.

        Returns:
            One row per author; position keeps the listed author order.
        """
        return [(a.pmid, i, au.name, au.last_name, au.fore_name, au.affiliations)
                for i, au in enumerate(a.authors)]

    @staticmethod
    def mesh_rows(a: PubMedArticle) -> list[tuple]:
        """Build the mesh_terms table rows for one article.

        Args:
            a: Parsed article.

        Returns:
            One row per MeSH term; position keeps the source order.
        """
        return [(a.pmid, i, m.descriptor, m.ui, m.major_topic, m.qualifiers)
                for i, m in enumerate(a.mesh_terms)]

    def upsert_articles(self, conn: psycopg.Connection, articles: list[PubMedArticle]) -> None:
        """Upsert articles and replace their authors and MeSH terms.

        Runs in one transaction. A PMID repeated within the list keeps its
        last version.

        Args:
            conn: Open Postgres connection.
            articles: Parsed articles to store.
        """
        unique = list({a.pmid: a for a in articles}.values())
        pmids = [a.pmid for a in unique]
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(UPSERT_ARTICLE_SQL, [self.article_row(a) for a in unique])
            cur.execute("DELETE FROM authors WHERE pmid = ANY(%s)", (pmids,))
            cur.execute("DELETE FROM mesh_terms WHERE pmid = ANY(%s)", (pmids,))
            cur.executemany(INSERT_AUTHOR_SQL, [r for a in unique for r in self.author_rows(a)])
            cur.executemany(INSERT_MESH_SQL, [r for a in unique for r in self.mesh_rows(a)])

    def parse_file(self, path: Path) -> list[PubMedArticle]:
        """Parse one gzipped raw batch file.

        Args:
            path: A batch_XXXX.xml.gz file.

        Returns:
            Articles found in the file (malformed ones are skipped).

        Raises:
            PubMedParseError: If the file cannot be parsed.
        """
        with gzip.open(path, "rb") as f:
            return list(self.parser.parse(f))

    def load_file(self, conn: psycopg.Connection, path: Path) -> int:
        """Parse one raw batch file and upsert its articles.

        Args:
            conn: Open Postgres connection.
            path: A batch_XXXX.xml.gz file.

        Returns:
            Number of articles written.

        Raises:
            PubMedParseError: If the file cannot be parsed.
        """
        articles = self.parse_file(path)
        self.upsert_articles(conn, articles)
        return len(articles)

    def run(self) -> list[str]:
        """Load every raw batch file into Postgres.

        A file that fails to parse or store is logged and skipped; the others
        still load.

        Returns:
            Names of files that failed (empty if all succeeded).
        """
        files = sorted(self.raw_dir.glob("batch_*.xml.gz"))
        logger.info("load_started", files=len(files), raw_dir=str(self.raw_dir))
        if not files:
            logger.warning("no_raw_files_found", raw_dir=str(self.raw_dir))
            return []

        failed_files: list[str] = []
        written = 0
        with psycopg.connect(self.database_url) as conn:
            self.create_schema(conn)
            for i, path in enumerate(files, start=1):
                with structlog.contextvars.bound_contextvars(file=path.name, file_no=i, n_files=len(files)):
                    try:
                        n = self.load_file(conn, path)
                        written += n
                        logger.info("file_loaded", articles=n)
                    except (PubMedParseError, OSError, psycopg.Error):
                        logger.exception("file_load_failed")
                        failed_files.append(path.name)

        if failed_files:
            logger.warning("files_failed", files=failed_files)
        logger.info("load_done", written=written, skipped=self.parser.skipped, failed_files=len(failed_files))
        return failed_files


def main() -> None:
    """Run the loader with the default folder and the database from .env."""
    load_env()
    with logged_job(JOB_NAME):
        PubMedPostgresLoader().run()


if __name__ == "__main__":
    setup_production_logging(log_file_for(JOB_NAME))  # once, at the application entry point
    main()
