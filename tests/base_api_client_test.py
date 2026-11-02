import pytest
import requests
from tenacity import wait_none

from api_clients.base_api_client import APIError, BaseAPIClient, is_retryable


class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.ok = status < 400
        self.reason = "reason"


def make_client(outcomes):
    """Client whose session returns/raises the given outcomes in order."""
    client = BaseAPIClient(base_url="http://x")
    client.retry_wait = wait_none()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    client.session.request = request
    return client, calls


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_then_succeeds(status):
    client, calls = make_client([status, status, 200])
    assert client.get("e").ok
    assert len(calls) == 3


def test_retries_connection_error():
    client, calls = make_client([requests.ConnectionError(), 200])
    assert client.get("e").ok
    assert len(calls) == 2


def test_gives_up_after_max_attempts():
    client, calls = make_client([500] * 10)
    with pytest.raises(APIError):
        client.get("e")
    assert len(calls) == client.max_attempts


@pytest.mark.parametrize("status", [400, 404])
def test_no_retry_on_client_error(status):
    client, calls = make_client([status, 200])
    with pytest.raises(APIError):
        client.get("e")
    assert len(calls) == 1


class Recorder:
    """Fake session.request that records its calls and returns status 200."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return type("R", (), {"ok": True, "status_code": 200, "reason": "OK"})()


def test_api_error_message_and_status():
    err = APIError(404, "Not Found")
    assert err.status_code == 404
    assert str(err) == "[404] Not Found"


@pytest.mark.parametrize("exc,expected", [
    (APIError(429, "x"), True),
    (APIError(503, "x"), True),
    (APIError(404, "x"), False),
    (requests.ConnectionError(), True),
    (requests.Timeout(), True),
    (ValueError(), False),
])
def test_is_retryable(exc, expected):
    assert is_retryable(exc) is expected


def test_base_client_defaults_and_url_building():
    client = BaseAPIClient(base_url="http://x/")
    client.session.request = rec = Recorder()
    assert client.auth_headers() == {}
    assert client.default_params() == {}
    assert client.default_headers() == {"Accept": "application/json"}
    client.get("/path", params={"a": 1})
    method, url, kwargs = rec.calls[0]
    assert (method, url) == ("GET", "http://x/path")
    assert kwargs["params"] == {"a": 1}


def test_post_sends_json_body():
    client = BaseAPIClient(base_url="http://x")
    client.session.request = rec = Recorder()
    client.post("p", json={"k": "v"})
    assert rec.calls[0][0] == "POST"
    assert rec.calls[0][2]["json"] == {"k": "v"}


def test_parse_response_decodes_json():
    class R:
        def json(self):
            return {"a": 1}

    assert BaseAPIClient(base_url="http://x").parse_response(R()) == {"a": 1}
