import json
import os
from pathlib import Path

import psycopg
import pytest
import structlog.testing

from etl.export_pubmed_jsonl import (
    MAX_DOC_BYTES,
    MAX_TOKEN_BYTES,
    ExportStats,
    PubMedJsonlExporter,
)
from etl.load_pubmed_to_postgres import PubMedPostgresLoader
from etl.pubmed_parser import Author, MeshTerm, PubMedArticle, PubMedParser

SAMPLE = Path("tests/data/pubmed_sample.xml")


class FakeResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class FakeServerCursor:
    """Stands in for a named (server-side) cursor that yields prepared docs."""

    def __init__(self, docs):
        self.docs = docs
        self.itersize = None
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.executed = sql

    def __iter__(self):
        return iter([(d,) for d in self.docs])


class FakeConn:
    """Stands in for a psycopg connection; returns a fixed count and docs."""

    def __init__(self, docs, expected=None):
        self.docs = docs
        self.expected = len(docs) if expected is None else expected
        self.cursor_obj = FakeServerCursor(docs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        return FakeResult(self.expected)

    def cursor(self, name=None):
        assert name, "must be a named cursor so rows are streamed, not loaded at once"
        return self.cursor_obj


def doc(pmid="1", text="Title\n\nAbstract text.", **extra):
    return {"_id": pmid, "text": text, "title": "Title", **extra}


@pytest.fixture
def out(tmp_path):
    return tmp_path / "pubmed_docs.jsonl"


@pytest.fixture
def exporter(out):
    return PubMedJsonlExporter("postgresql://unused", output_path=out)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env")
    assert PubMedJsonlExporter().database_url == "postgresql://env"
    assert PubMedJsonlExporter("postgresql://arg").database_url == "postgresql://arg"


def test_database_url_missing_raises_without_leaking(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match=".env.example"):
        PubMedJsonlExporter()


def test_rejection_reason_accepts_normal_doc():
    d = doc()
    assert PubMedJsonlExporter.rejection_reason(d, json.dumps(d)) is None


def test_rejection_reason_empty_text():
    d = doc(text="   ")
    assert PubMedJsonlExporter.rejection_reason(d, json.dumps(d)) == "empty_text"


def test_rejection_reason_token_too_long():
    d = doc(text="Title\n\n" + "A" * (MAX_TOKEN_BYTES + 1))
    assert PubMedJsonlExporter.rejection_reason(d, json.dumps(d)) == "token_too_long"


def test_rejection_reason_counts_bytes_not_characters():
    # 600 two-byte characters is 1200 bytes: over the limit although under 1024 characters
    d = doc(text="é" * 600)
    assert PubMedJsonlExporter.rejection_reason(d, json.dumps(d)) == "token_too_long"


def test_rejection_reason_document_too_large():
    d = doc(text="word " * 10)
    assert PubMedJsonlExporter.rejection_reason(d, "x" * (MAX_DOC_BYTES + 1)) == "document_too_large"


def test_run_writes_one_json_line_per_doc(exporter, out, monkeypatch):
    docs = [doc("1"), doc("2", text="Other\n\nText é ü.")]
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn(docs))
    stats = exporter.run()
    assert stats == ExportStats(expected=2, written=2, skipped=0)
    assert read_lines(out) == docs
    assert "é" in out.read_text(encoding="utf-8")  # not escaped to é
    assert not out.with_name(out.name + ".tmp").exists()


def test_run_streams_with_the_configured_fetch_size(out, monkeypatch):
    conn = FakeConn([doc()])
    monkeypatch.setattr(psycopg, "connect", lambda url: conn)
    PubMedJsonlExporter("postgresql://unused", output_path=out, fetch_size=7).run()
    assert conn.cursor_obj.itersize == 7


def test_run_skips_rejected_docs_and_counts_them(exporter, out, monkeypatch):
    docs = [doc("1"), doc("2", text="x " + "B" * 2000), doc("3")]
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn(docs))
    with structlog.testing.capture_logs() as logs:
        stats = exporter.run()
    assert stats == ExportStats(expected=3, written=2, skipped=1)
    assert [d["_id"] for d in read_lines(out)] == ["1", "3"]
    skipped = [e for e in logs if e["event"] == "doc_skipped"]
    assert [(e["pmid"], e["reason"]) for e in skipped] == [("2", "token_too_long")]


def test_run_with_nothing_to_export_keeps_existing_file(exporter, out, monkeypatch):
    out.write_text('{"_id": "old"}\n')
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn([], expected=0))
    with structlog.testing.capture_logs() as logs:
        stats = exporter.run()
    assert stats == ExportStats(0, 0, 0)
    assert out.read_text() == '{"_id": "old"}\n'
    assert [e["event"] for e in logs] == ["export_started", "no_articles_to_export"]


def test_run_creates_missing_output_folder(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "docs.jsonl"
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn([doc()]))
    PubMedJsonlExporter("postgresql://unused", output_path=target).run()
    assert target.exists()


def test_failed_run_leaves_no_tmp_and_keeps_old_file(exporter, out, monkeypatch):
    out.write_text('{"_id": "old"}\n')

    class Boom(FakeServerCursor):
        def __iter__(self):
            raise psycopg.OperationalError("connection lost")

    conn = FakeConn([doc()])
    conn.cursor_obj = Boom([])
    monkeypatch.setattr(psycopg, "connect", lambda url: conn)
    with pytest.raises(psycopg.OperationalError):
        exporter.run()
    assert out.read_text() == '{"_id": "old"}\n'
    assert not out.with_name(out.name + ".tmp").exists()


def test_run_warns_when_counts_do_not_add_up(exporter, monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn([doc("1")], expected=3))
    with structlog.testing.capture_logs() as logs:
        exporter.run()
    mismatch = [e for e in logs if e["event"] == "count_mismatch"]
    assert mismatch and mismatch[0]["expected"] == 3 and mismatch[0]["written"] == 1


def test_run_logs_events(exporter, monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda url: FakeConn([doc()]))
    with structlog.testing.capture_logs() as logs:
        exporter.run()
    assert [e["event"] for e in logs] == ["export_started", "export_done"]


# ---- real Postgres: checks that EXPORT_SQL itself is correct ----------------

@pytest.fixture
def pg_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(url) as c:
        c.execute("DROP TABLE IF EXISTS mesh_terms, authors, articles")
        PubMedPostgresLoader.create_schema(c)
    yield url
    with psycopg.connect(url) as c:
        c.execute("DROP TABLE IF EXISTS mesh_terms, authors, articles")


def article(pmid, title, abstract, **kw) -> PubMedArticle:
    fields = dict(authors=[], journal=None, pub_year=None, doi=None, publication_types=[],
                  mesh_terms=[], chemicals=[], keywords=[])
    fields.update(kw)
    return PubMedArticle(pmid=pmid, title=title, abstract=abstract, **fields)


def load(url, articles):
    with psycopg.connect(url) as c:
        PubMedPostgresLoader(url).upsert_articles(c, articles)


def is_pinecone_metadata(value) -> bool:
    """Strings, numbers, booleans and flat lists of strings only."""
    if isinstance(value, (str, int, float, bool)):
        return True
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def test_real_postgres_exports_parsed_articles(pg_url, tmp_path):
    parsed = list(PubMedParser().parse(SAMPLE))
    load(pg_url, parsed)
    out = tmp_path / "docs.jsonl"
    stats = PubMedJsonlExporter(pg_url, output_path=out).run()
    docs = {d["_id"]: d for d in read_lines(out)}

    assert stats.skipped == 0 and stats.written == stats.expected == len(parsed)
    assert list(docs) == sorted(docs)  # ordered by pmid, so the file is diffable
    for a in parsed:
        d = docs[a.pmid]
        assert d["authors"] == [au.name for au in a.authors]  # listed order kept
        assert d["mesh_terms"] == [m.descriptor for m in a.mesh_terms]
        assert d["mesh_major"] == a.concepts
        assert d["publication_types"] == a.publication_types
        assert d.get("year") == a.pub_year
        assert d["text"].startswith(a.title)
        assert "loaded_at" not in d
        assert all(is_pinecone_metadata(v) for v in d.values())
        assert all(v is not None for v in d.values())


def test_real_postgres_text_rules(pg_url, tmp_path):
    load(pg_url, [
        article("1", "Only a title", None),
        article("2", "A title.", "a title"),  # abstract repeats the title
        article("3", None, "Only an abstract"),
        article("4", "Both", "Parts here."),
        article("5", "  ", "   "),  # nothing usable: left out
    ])
    out = tmp_path / "docs.jsonl"
    stats = PubMedJsonlExporter(pg_url, output_path=out).run()
    docs = {d["_id"]: d for d in read_lines(out)}

    assert stats == ExportStats(expected=4, written=4, skipped=0)
    assert set(docs) == {"1", "2", "3", "4"}
    assert docs["1"]["text"] == "Only a title"
    assert docs["2"]["text"] == "A title."  # title once, not twice
    assert docs["3"]["text"] == "Only an abstract" and "title" not in docs["3"]
    assert docs["4"]["text"] == "Both\n\nParts here."
    assert "year" not in docs["1"] and "doi" not in docs["1"]  # nulls are dropped, not sent as null


def test_real_postgres_articles_without_children_get_empty_lists(pg_url, tmp_path):
    load(pg_url, [article("1", "T", "A")])
    out = tmp_path / "docs.jsonl"
    PubMedJsonlExporter(pg_url, output_path=out).run()
    d = read_lines(out)[0]
    assert d["authors"] == [] and d["mesh_terms"] == [] and d["mesh_major"] == []


def test_real_postgres_no_row_multiplication(pg_url, tmp_path):
    """Many authors and many MeSH terms on one article must still give one line."""
    a = article(
        "1", "T", "A",
        authors=[Author(name=f"Person {i}") for i in range(8)],
        mesh_terms=[MeshTerm(f"Term {i}", None, i % 2 == 0) for i in range(12)],
    )
    load(pg_url, [a])
    out = tmp_path / "docs.jsonl"
    PubMedJsonlExporter(pg_url, output_path=out).run()
    docs = read_lines(out)
    assert len(docs) == 1
    assert len(docs[0]["authors"]) == 8 and len(docs[0]["mesh_terms"]) == 12
    assert docs[0]["mesh_major"] == [f"Term {i}" for i in range(0, 12, 2)]
