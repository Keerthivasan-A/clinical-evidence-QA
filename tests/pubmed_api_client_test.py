import pytest

from api_clients.pubmed_api_client import PubMedAPIClient


class Recorder:
    """Fake session.request that records its calls and returns status 200."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return type("R", (), {"ok": True, "status_code": 200, "reason": "OK"})()


@pytest.fixture
def pubmed(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "KEY")
    monkeypatch.setenv("NCBI_EMAIL_ADDRESS", "me@example.com")
    client = PubMedAPIClient()
    client.session.request = Recorder()
    return client


def test_pubmed_reads_env_and_default_params(pubmed):
    assert pubmed.default_params() == {"api_key": "KEY", "email": "me@example.com"}


def test_pubmed_accepts_old_misspelled_email_var(monkeypatch):
    monkeypatch.delenv("NCBI_EMAIL_ADDRESS", raising=False)
    monkeypatch.setenv("NCBI_EMAI_ADDRESS", "old@example.com")
    assert PubMedAPIClient().email == "old@example.com"


def test_request_delay_depends_on_api_key(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "KEY")
    assert PubMedAPIClient().request_delay_seconds == 0.11
    monkeypatch.delenv("NCBI_API_KEY")
    assert PubMedAPIClient().request_delay_seconds == 0.34


def test_fetch_builds_efetch_request(pubmed):
    pubmed.fetch(["1", "2"], db="pubmed")
    method, url, kwargs = pubmed.session.request.calls[0]
    assert url.endswith("/efetch.fcgi")
    assert kwargs["params"]["id"] == "1,2"
    assert kwargs["params"]["db"] == "pubmed"
    assert kwargs["params"]["retmode"] == "xml"


def test_search_builds_esearch_request(pubmed):
    pubmed.search("cancer", retmax=5)
    _, url, kwargs = pubmed.session.request.calls[0]
    assert url.endswith("/esearch.fcgi")
    assert kwargs["params"]["term"] == "cancer"
    assert kwargs["params"]["retmax"] == 5
