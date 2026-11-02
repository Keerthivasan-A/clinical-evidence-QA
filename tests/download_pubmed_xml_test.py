import gzip

import pytest
import structlog.testing

from etl.download_pubmed_xml import PubMedDownloader

XML = b"<PubmedArticleSet></PubmedArticleSet>"


class FakeResponse:
    """Minimal stand-in for a requests response."""

    def __init__(self, content=XML, status=200):
        self.content = content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeClient:
    """Returns the given responses in order and records requested id lists."""

    request_delay_seconds = 0

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def fetch(self, id_list, db="pmc"):
        self.calls.append((list(id_list), db))
        return self.responses.pop(0) if self.responses else FakeResponse()


@pytest.fixture
def make(tmp_path):
    def _make(pmids="1\n2\n3\n", client=None, **kw):
        (tmp_path / "in.txt").write_text(pmids)
        return PubMedDownloader(
            client or FakeClient(),
            input_path=tmp_path / "in.txt",
            raw_dir=tmp_path / "raw",
            downloaded_path=tmp_path / "done.txt",
            failed_path=tmp_path / "failed.txt",
            delay_seconds=0,
            **kw,
        )

    return _make


def test_read_pmids_dedupes_and_skips_blanks(make):
    assert make("1\n\n2\n1\n 3 \n").read_pmids() == ["1", "2", "3"]


def test_read_done_pmids_missing_file(make):
    assert make().read_done_pmids() == set()


def test_read_done_pmids_reads_file(make, tmp_path):
    d = make()
    (tmp_path / "done.txt").write_text("1\n\n2\n")
    assert d.read_done_pmids() == {"1", "2"}


def test_default_delay_comes_from_client(tmp_path):
    client = FakeClient()
    client.request_delay_seconds = 0.5
    d = PubMedDownloader(client, input_path=tmp_path, raw_dir=tmp_path)
    assert d.delay_seconds == 0.5


def test_save_raw_batch_roundtrip_and_numbering(make):
    d = make()
    d.raw_dir.mkdir()
    assert d.next_batch_number() == 1
    path = d.save_raw_batch(XML, 1)
    assert path.name == "batch_0001.xml.gz"
    assert gzip.decompress(path.read_bytes()) == XML
    assert not list(d.raw_dir.glob("*.tmp"))
    assert d.next_batch_number() == 2


def test_download_batch_returns_content(make):
    d = make(client=FakeClient([FakeResponse(XML)]))
    assert d.download_batch(["1"]) == XML
    assert d.client.calls == [(["1"], "pubmed")]


def test_download_batch_rejects_non_xml(make):
    d = make(client=FakeClient([FakeResponse(b"<error>nope</error>")]))
    with pytest.raises(ValueError):
        d.download_batch(["1"])


def test_download_batch_raises_on_http_error(make):
    d = make(client=FakeClient([FakeResponse(XML, status=500)]))
    with pytest.raises(RuntimeError):
        d.download_batch(["1"])


def test_run_downloads_in_batches_and_records(make):
    d = make("1\n2\n3\n", batch_size=2)
    assert d.run() == []
    assert [c[0] for c in d.client.calls] == [["1", "2"], ["3"]]
    assert sorted(p.name for p in d.raw_dir.glob("*.gz")) == ["batch_0001.xml.gz", "batch_0002.xml.gz"]
    assert d.read_done_pmids() == {"1", "2", "3"}
    assert not d.failed_path.exists()


def test_run_resumes_and_skips_done(make):
    d = make("1\n2\n3\n", batch_size=2)
    d.downloaded_path.write_text("1\n2\n")
    d.run()
    assert [c[0] for c in d.client.calls] == [["3"]]


def test_run_nothing_to_do(make):
    d = make("1\n")
    d.downloaded_path.write_text("1\n")
    assert d.run() == []
    assert d.client.calls == []


def test_run_failed_batch_is_recorded_and_not_marked_done(make):
    client = FakeClient([FakeResponse(XML, status=500), FakeResponse(XML)])
    d = make("1\n2\n3\n", client=client, batch_size=2)
    assert d.run() == ["1", "2"]
    assert d.failed_path.read_text() == "1\n2\n"
    assert d.read_done_pmids() == {"3"}


def test_run_clears_stale_failed_file(make):
    d = make("1\n")
    d.failed_path.write_text("old\n")
    d.run()
    assert not d.failed_path.exists()


def test_run_new_files_do_not_overwrite_existing(make):
    d = make("1\n2\n", batch_size=1)
    d.raw_dir.mkdir()
    (d.raw_dir / "batch_0005.xml.gz").write_bytes(b"x")
    d.run()
    assert (d.raw_dir / "batch_0006.xml.gz").exists()
    assert (d.raw_dir / "batch_0007.xml.gz").exists()
    assert (d.raw_dir / "batch_0005.xml.gz").read_bytes() == b"x"


def test_run_logs_batch_events(make):
    client = FakeClient([FakeResponse(XML), FakeResponse(XML, status=500)])
    d = make("1\n2\n", client=client, batch_size=1)
    with structlog.testing.capture_logs() as logs:
        d.run()
    events = [e["event"] for e in logs]
    assert events == ["download_started", "batch_done", "batch_failed", "failed_pmids_written", "download_done"]
    failed = logs[2]
    assert failed["log_level"] == "error" and failed["first_pmid"] == "2"
    assert "exc_info" in failed or "exception" in failed
