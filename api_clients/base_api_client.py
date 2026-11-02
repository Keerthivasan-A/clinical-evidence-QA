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
    """Raised when an API answers with a non-success HTTP status.

    Attributes:
        status_code: HTTP status code of the response.
    """

    def __init__(self, status_code, message):
        """Build the error.

        Args:
            status_code: HTTP status code of the response.
            message: Short description, usually the HTTP reason phrase.
        """
        self.status_code = status_code
        super().__init__(f"[{status_code}] {message}")


def is_retryable(exc: BaseException) -> bool:
    """Decide whether a failed request is worth retrying.

    Args:
        exc: The exception raised by the request.

    Returns:
        True for 429, 5xx, timeouts and dropped connections; False otherwise.
    """
    if isinstance(exc, APIError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _log_retry(state: RetryCallState) -> None:
    """Log a warning before tenacity sleeps between attempts.

    Args:
        state: Tenacity's state for the current retry.
    """
    logger.warning(
        "api_retry",
        attempt=state.attempt_number,
        error=str(state.outcome.exception()),
        sleep_seconds=round(state.next_action.sleep, 2),
    )


class BaseAPIClient:
    """Generic JSON API client with a shared session and retry with backoff.

    Subclasses set base_url and override the hook methods (auth_headers,
    default_params, default_headers, parse_response) where they differ.

    Attributes:
        base_url: Root URL without a trailing slash.
        max_attempts: Total tries per request, including the first.
        retry_wait: Tenacity wait strategy between attempts.
        session: Shared requests session (keeps connections open).
    """

    base_url = ""
    max_attempts = 5
    retry_wait = wait_exponential(multiplier=1, min=1, max=30)  # 1s, 2s, 4s, ...

    def __init__(self, base_url=None):
        """Create the client and its session.

        Args:
            base_url: Overrides the class-level base_url when given.
        """
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.session = requests.Session()

    # Hooks: override only what differs
    def auth_headers(self) -> dict:
        """Return authentication headers; none by default."""
        return {}

    def default_params(self) -> dict:
        """Return query parameters added to every request; none by default."""
        return {}

    def default_headers(self) -> dict:
        """Return headers added to every request (Accept: application/json)."""
        return {"Accept": "application/json"}

    def parse_response(self, response):
        """Turn a response into data; the default decodes JSON.

        Args:
            response: A requests response.

        Returns:
            The decoded JSON body.
        """
        return response.json()

    # Core logic lives here, once
    def call_api(self, method, endpoint, params=None, json=None, timeout=30):
        """Send a request, retrying 429/5xx and connection errors with backoff.

        Args:
            method: HTTP method, e.g. "GET".
            endpoint: Path relative to base_url.
            params: Query parameters, merged over default_params().
            json: JSON body, if any.
            timeout: Seconds before a single attempt times out.

        Returns:
            The successful requests response.

        Raises:
            APIError: On a non-success status once retries are exhausted or
                the status is not retryable.
            requests.RequestException: On network errors after retries.
        """
        retrying = Retrying(
            retry=retry_if_exception(is_retryable),
            stop=stop_after_attempt(self.max_attempts),
            wait=self.retry_wait,
            before_sleep=_log_retry,
            reraise=True,
        )
        return retrying(self._send, method, endpoint, params, json, timeout)

    def _send(self, method, endpoint, params, json, timeout):
        """Make one request attempt and raise APIError on a bad status."""
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
        """Send a GET request; see call_api for arguments and errors."""
        return self.call_api("GET", endpoint, params=params, **kw)

    def post(self, endpoint, json=None, params=None, **kw):
        """Send a POST request; see call_api for arguments and errors."""
        return self.call_api("POST", endpoint, params=params, json=json, **kw)
