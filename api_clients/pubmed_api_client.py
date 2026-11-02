import os
from api_clients.base_api_client import BaseAPIClient


class PubMedAPIClient(BaseAPIClient):
    """Client for NCBI E-utilities (esearch and efetch).

    Reads NCBI_API_KEY and NCBI_EMAIL_ADDRESS from the environment. Without
    an API key NCBI allows 3 requests/second, with one 10.

    Attributes:
        api_key: NCBI API key, or None.
        email: Contact email sent to NCBI, or None.
        search_endpoint: Path of the search service.
        fetch_endpoint: Path of the fetch service.
    """

    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self):
        """Create the client and read credentials from the environment."""
        super().__init__()
        self.api_key = os.environ.get("NCBI_API_KEY")
        # NCBI_EMAI_ADDRESS is the old (misspelled) name, kept so existing envs work
        self.email = os.environ.get("NCBI_EMAIL_ADDRESS") or os.environ.get("NCBI_EMAI_ADDRESS")
        self.search_endpoint = "esearch.fcgi"
        self.fetch_endpoint = "efetch.fcgi"

    @property
    def request_delay_seconds(self) -> float:
        """Pause between requests: NCBI allows 10 req/s with an API key, 3 without."""
        return 0.11 if self.api_key else 0.34

    def default_params(self):
        """Return the api_key and email sent with every request."""
        return {"api_key": self.api_key, "email": self.email}

    def search(self, term, retmax=2):
        """Search PMC for a term.

        Args:
            term: Search query.
            retmax: Maximum number of ids to return.

        Returns:
            The response, whose JSON body holds the matching ids.
        """
        return self.get(endpoint=self.search_endpoint,
                        params={
                            "db": "pmc",
                            "term": term,
                            "retmax": retmax,
                            "retmode": "json",
                            "email": self.email,
                            "api_key": self.api_key,
                        })

    def fetch(self, id_list, db='pmc'):
        """Fetch full records as XML.

        Args:
            id_list: IDs to fetch (keep to ~200 per call to avoid long URLs).
            db: NCBI database, e.g. "pubmed" or "pmc".

        Returns:
            The response; response.content holds the raw XML bytes.
        """
        return self.get(endpoint=self.fetch_endpoint,
                        params={
                            "db": db,
                            "id": ",".join(id_list) if id_list else "",
                            "retmode": "xml",
                            "email": self.email,
                            "api_key": self.api_key,
                        })
