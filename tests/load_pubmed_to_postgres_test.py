import gzip
import os
from pathlib import Path

import psycopg
import pytest
import structlog.testing

from etl.load_pubmed_to_postgres import PubMedPostgresLoader
from etl.pubmed_parser import PubMedParseError, PubMedParser

SAMPLE = Path("tests/data/pubmed_sample.xml")


class FakeCursor:
    """Records SQL calls instead of talking to Postgres."""

    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))

    def executemany(self, sql, rows):
        self.log.append(("executemany", sql, list(rows)))


class FakeConn:
    """Stands in for a psycopg connection; records everything it is asked."""

    def __init__(self):
        self.log = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.log.append(("execute", sql, None))

    def commit(self):
        self.commits += 1

    def transaction(self):
        return FakeCursor(self.log)  # any context manager will do

    def cursor(self):
        return FakeCursor(self.log)

    def rows(self, sql_start):
        return [c[2] for c in self.log if c[0] == "executemany" and c[1].lstrip().startswith(sql_start)]


@pytest.fixture
def articles():
    return list(PubMedParser().parse(SAMPLE))


@pytest.fixture
def loader(tmp_path):
    return PubMedPostgresLoader("postgresql://unused", raw_dir=tmp_path)


def write_raw(path: Path, content: bytes) -> Path:
    path.write_bytes(gzip.compress(content))
    return path


def test_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env")
    assert PubMedPostgresLoader().database_url == "postgresql://env"
    assert PubMedPostgresLoader("postgresql://arg").database_url == "postgresql://arg"


def test_database_url_missing_raises_without_leaking(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match=".env.example"):
        PubMedPostgresLoader()


def test_article_row_matches_columns(articles):
    row = PubMedPostgresLoader.article_row(articles[0])
    assert len(row) == 9
    assert row[0] == "24086431"


def test_child_rows_keep_order(articles):
    a = articles[0]
    authors = PubMedPostgresLoader.author_rows(a)
    mesh = PubMedPostgresLoader.mesh_rows(a)
    assert [r[1] for r in authors] == list(range(len(a.authors)))
    assert [r[2] for r in mesh] == [m.descriptor for m in a.mesh_terms]


def test_create_schema_runs_ddl_and_commits():
    conn = FakeConn()
    PubMedPostgresLoader.create_schema(conn)
    assert "CREATE TABLE IF NOT EXISTS articles" in conn.log[0][1]
    assert conn.commits == 1


def test_upsert_dedupes_and_replaces_children(loader, articles):
    conn = FakeConn()
    loader.upsert_articles(conn, articles + [articles[0]])  # duplicate pmid
    assert len(conn.rows("INSERT INTO articles")[0]) == len(articles)
    deletes = [c for c in conn.log if c[0] == "execute" and c[1].startswith("DELETE")]
    assert len(deletes) == 2
    assert len(conn.rows("INSERT INTO authors")[0]) == sum(len(a.authors) for a in articles)
    assert len(conn.rows("INSERT INTO mesh_terms")[0]) == sum(len(a.mesh_terms) for a in articles)


def test_parse_file_reads_gzip(loader, tmp_path):
    path = write_raw(tmp_path / "batch_0001.xml.gz", SAMPLE.read_bytes())
    assert len(loader.parse_file(path)) == 3


def test_parse_file_broken_xml_raises(loader, tmp_path):
    path = write_raw(tmp_path / "batch_0001.xml.gz", b"<PubmedArticleSet><oops")
    with pytest.raises(PubMedParseError):
        loader.parse_file(path)


def test_load_file_returns_count(loader, tmp_path):
    path = write_raw(tmp_path / "batch_0001.xml.gz", SAMPLE.read_bytes())
    assert loader.load_file(FakeConn(), path) == 3


def test_run_without_files_does_not_connect(loader, monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("should not connect"))
    assert loader.run() == []


def test_run_loads_good_files_and_reports_bad(loader, tmp_path, monkeypatch):
    write_raw(tmp_path / "batch_0001.xml.gz", SAMPLE.read_bytes())
    write_raw(tmp_path / "batch_0002.xml.gz", b"<PubmedArticleSet><oops")
    write_raw(tmp_path / "batch_0003.xml.gz", SAMPLE.read_bytes())
    conn = FakeConn()
    monkeypatch.setattr(psycopg, "connect", lambda url: conn)
    assert loader.run() == ["batch_0002.xml.gz"]
    assert len(conn.rows("INSERT INTO articles")) == 2  # files 1 and 3
    assert conn.commits == 1


def test_run_logs_events(loader, tmp_path, monkeypatch):
    write_raw(tmp_path / "batch_0001.xml.gz", SAMPLE.read_bytes())
    write_raw(tmp_path / "batch_0002.xml.gz", b"<PubmedArticleSet><oops")
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn())
    with structlog.testing.capture_logs() as logs:
        loader.run()
    events = [e["event"] for e in logs if not e["event"].startswith("pubmed_")]
    assert events == ["load_started", "file_loaded", "file_load_failed", "files_failed", "load_done"]


def test_run_warns_when_no_files(loader):
    with structlog.testing.capture_logs() as logs:
        loader.run()
    assert [e["event"] for e in logs] == ["load_started", "no_raw_files_found"]


@pytest.fixture
def pg():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(url) as c:
        c.execute("DROP TABLE IF EXISTS mesh_terms, authors, articles")
        PubMedPostgresLoader.create_schema(c)
        yield c
        c.execute("DROP TABLE IF EXISTS mesh_terms, authors, articles")


def test_upsert_is_idempotent_in_real_postgres(pg, loader, articles):
    for _ in range(2):
        loader.upsert_articles(pg, articles)
    count = lambda t: pg.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    assert count("articles") == len(articles)
    assert count("authors") == sum(len(a.authors) for a in articles)
    assert count("mesh_terms") == sum(len(a.mesh_terms) for a in articles)
