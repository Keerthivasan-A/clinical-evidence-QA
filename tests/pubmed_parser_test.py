import io
import json
import logging
import os

import pytest
import structlog

from etl.pubmed_parser import PubMedParseError, PubMedParser, parse_pubmed
from logs.logger_config import setup_production_logging

SAMPLE = os.environ.get("PUBMED_SAMPLE", "tests/data/pubmed_sample.xml")
BAD = "<PubmedArticle><MedlineCitation/></PubmedArticle>"  # article with no PMID


# --- helpers ---------------------------------------------------------------
def xml(body: str) -> io.BytesIO:
    """Wrap article XML snippets in a <PubmedArticleSet> as a byte stream."""
    return io.BytesIO(f"<PubmedArticleSet>{body}</PubmedArticleSet>".encode())


def article(inner: str, pmid: str = "1") -> str:
    """Build a minimal valid <PubmedArticle> with custom <Article> content."""
    return (f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID>"
            f"<Article>{inner}</Article></MedlineCitation></PubmedArticle>")


# --- field extraction --------------------------------------------------------
@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="sample file not present")
def test_real_file():
    arts = list(parse_pubmed(SAMPLE))
    assert len(arts) == 3
    a = arts[0]
    assert a.pmid == "24086431"
    assert a.title.startswith("NOTCH3 variants")
    assert a.abstract.startswith("BACKGROUND:")
    assert len(a.authors) == 16
    assert a.doi == "10.1371/journal.pone.0075035"
    assert "Brain Ischemia" in a.concepts


def test_inline_tags_not_truncated():
    a = next(parse_pubmed(xml(article(
        "<ArticleTitle>Role of <i>NOTCH3</i> in CADASIL</ArticleTitle>"))))
    assert a.title == "Role of NOTCH3 in CADASIL"


def test_missing_optional_fields_are_none_or_empty():
    a = next(parse_pubmed(xml(article(""))))
    assert a.title is None and a.abstract is None and a.pub_year is None
    assert a.authors == [] and a.mesh_terms == []


def test_medline_date_year():
    a = next(parse_pubmed(xml(article(
        "<Journal><JournalIssue><PubDate><MedlineDate>2013 Sep-Oct</MedlineDate>"
        "</PubDate></JournalIssue></Journal>"))))
    assert a.pub_year == 2013


def test_collective_author():
    a = next(parse_pubmed(xml(article(
        "<AuthorList><Author><CollectiveName>SWISS Investigators</CollectiveName>"
        "</Author></AuthorList>"))))
    assert a.authors[0].name == "SWISS Investigators"


# --- error handling ----------------------------------------------------------
def test_bad_article_skipped_not_fatal():
    arts = list(parse_pubmed(xml(BAD + article("", pmid="2"))))
    assert [a.pmid for a in arts] == ["2"]


def test_strict_mode_raises():
    with pytest.raises(PubMedParseError):
        list(parse_pubmed(xml(BAD), strict=True))


def test_broken_xml_raises_clean_error():
    with pytest.raises(PubMedParseError):
        list(parse_pubmed(io.BytesIO(b"<PubmedArticleSet><PubmedArticle>")))


def test_xml_bomb_blocked():
    bomb = b'<!DOCTYPE x [<!ENTITY a "aaaa">]><PubmedArticleSet>&a;</PubmedArticleSet>'
    with pytest.raises(PubMedParseError):
        list(parse_pubmed(io.BytesIO(bomb)))


# --- PubMedParser counts -----------------------------------------------------
def test_counts_parsed_and_skipped():
    p = PubMedParser()
    list(p.parse(xml(BAD + article("", pmid="2") + article("", pmid="3"))))
    assert (p.parsed, p.skipped, p.total) == (2, 1, 3)


def test_counts_accumulate_across_batches_and_reset():
    p = PubMedParser()
    list(p.parse(xml(article("", pmid="1"))))
    list(p.parse(xml(BAD + article("", pmid="2"))))
    assert (p.parsed, p.skipped) == (2, 1)
    p.reset()
    assert (p.parsed, p.skipped) == (0, 0)


def test_count_correct_when_loop_breaks_early():
    p = PubMedParser()
    for _ in p.parse(xml(article("", pmid="1") + article("", pmid="2"))):
        break
    assert p.parsed == 1


# --- logging (through the real production logger) --------------------------
@pytest.fixture
def app_logs(tmp_path):
    """Set up the production logger writing to a temp file.

    Returns a function that reads the log file back as a list of dicts,
    one per JSON line. After the test, logging is reset so other tests
    are not affected.
    """
    log_file = tmp_path / "app.log"
    setup_production_logging(log_file, cache_logger=False)

    def read() -> list[dict]:
        return [json.loads(line) for line in log_file.read_text().splitlines()]

    yield read

    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)


def test_counts_are_logged(app_logs):
    list(PubMedParser().parse(xml(BAD + article(""))))
    done = next(e for e in app_logs() if e["event"] == "pubmed_parse_done")
    assert done["level"] == "info"
    assert done["logger"] == "etl.pubmed_parser"
    assert (done["parsed"], done["skipped"]) == (1, 1)
    assert "timestamp" in done


def test_skipped_article_logs_warning(app_logs):
    list(PubMedParser().parse(xml(BAD)))
    skipped = next(e for e in app_logs() if e["event"] == "pubmed_article_skipped")
    assert skipped["level"] == "warning"
    assert "no PMID" in skipped["error"]
