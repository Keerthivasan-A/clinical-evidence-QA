import os

import psycopg
import pytest

from etl.load_pubmed_to_postgres import (
    _author_rows,
    _mesh_rows,
    create_schema,
    upsert_articles,
)
from etl.pubmed_parser import PubMedParser

SAMPLE = "tests/data/pubmed_sample.xml"


@pytest.fixture
def articles():
    return list(PubMedParser().parse(SAMPLE))


def test_child_rows_keep_order(articles):
    a = articles[0]
    assert [r[1] for r in _author_rows(a)] == list(range(len(a.authors)))
    assert [r[2] for r in _mesh_rows(a)] == [m.descriptor for m in a.mesh_terms]


@pytest.fixture
def conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(url) as c:
        c.execute("DROP TABLE IF EXISTS mesh_terms, authors, articles")
        create_schema(c)
        yield c
        c.execute("DROP TABLE IF EXISTS mesh_terms, authors, articles")


def test_upsert_is_idempotent(conn, articles):
    for _ in range(2):
        upsert_articles(conn, articles)
    count = lambda t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    assert count("articles") == len(articles)
    assert count("authors") == sum(len(a.authors) for a in articles)
    assert count("mesh_terms") == sum(len(a.mesh_terms) for a in articles)
