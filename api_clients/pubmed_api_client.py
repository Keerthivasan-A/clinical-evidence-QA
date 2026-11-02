import os
from api_clients.base_api_client import BaseAPIClient


class PubMedAPIClient(BaseAPIClient):
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self):
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
        return {"api_key": self.api_key, "email": self.email}

    def search(self, term, retmax=2):
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
        return self.get(endpoint=self.fetch_endpoint,
                        params={
                            "db": db,
                            "id": ",".join(id_list) if id_list else "",
                            "retmode": "xml",
                            "email": self.email,
                            "api_key": self.api_key,
                        })
