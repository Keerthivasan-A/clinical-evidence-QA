import requests
import structlog
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)


class APIError(Exception):
    def __init__(self, status_code, message):
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}")


def is_retryable(exc: BaseException) -> bool:
    """True for errors worth retrying: 429, 5xx, timeouts and dropped connections."""
    if isinstance(exc, APIError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _log_retry(state: RetryCallState) -> None:
    logger.warning(
        "api_retry",
        attempt=state.attempt_number,
        error=str(state.outcome.exception()),
        sleep_seconds=round(state.next_action.sleep, 2),
    )


class BaseAPIClient:
    base_url = ""
    max_attempts = 5
    retry_wait = wait_exponential(multiplier=1, min=1, max=30)  # 1s, 2s, 4s, ...

    def __init__(self, base_url=None):
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.session = requests.Session()

    # Hooks: override only what differs
    def auth_headers(self) -> dict:
        return {}

    def default_params(self) -> dict:
        return {}

    def default_headers(self) -> dict:
        return {"Accept": "application/json"}

    def parse_response(self, response):
        return response.json()

    # Core logic lives here, once
    def call_api(self, method, endpoint, params=None, json=None, timeout=30):
        """Send a request, retrying 429/5xx and connection errors with backoff."""
        retrying = Retrying(
            retry=retry_if_exception(is_retryable),
            stop=stop_after_attempt(self.max_attempts),
            wait=self.retry_wait,
            before_sleep=_log_retry,
            reraise=True,
        )
        return retrying(self._send, method, endpoint, params, json, timeout)

    def _send(self, method, endpoint, params, json, timeout):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = self.session.request(
            method,
            url,
            headers={**self.default_headers(), **self.auth_headers()},
            params={**self.default_params(), **(params or {})},
            json=json,
            timeout=timeout,
        )
        if not response.ok:
            raise APIError(response.status_code, response.reason)
        return response

    def get(self, endpoint, params=None, **kw):
        return self.call_api("GET", endpoint, params=params, **kw)

    def post(self, endpoint, json=None, params=None, **kw):
        return self.call_api("POST", endpoint, params=params, json=json, **kw)
