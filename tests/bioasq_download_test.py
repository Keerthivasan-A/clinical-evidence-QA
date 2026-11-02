import gzip

from etl.bioasq_download_pubmed_docs import (
    next_batch_number,
    read_done_pmids,
    read_pmids,
    save_raw_batch,
)


def test_read_pmids_dedupes_and_skips_blanks(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("1\n\n2\n1\n 3 \n")
    assert read_pmids(f) == ["1", "2", "3"]


def test_read_done_pmids_missing_file(tmp_path):
    assert read_done_pmids(tmp_path / "none.txt") == set()


def test_save_raw_batch_roundtrip_and_numbering(tmp_path):
    assert next_batch_number(tmp_path) == 1
    path = save_raw_batch(b"<PubmedArticleSet/>", tmp_path, 1)
    assert path.name == "batch_0001.xml.gz"
    assert gzip.decompress(path.read_bytes()) == b"<PubmedArticleSet/>"
    assert not list(tmp_path.glob("*.tmp"))
    assert next_batch_number(tmp_path) == 2
