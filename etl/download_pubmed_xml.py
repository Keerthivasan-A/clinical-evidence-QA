"""Download BioASQ PubMed documents from NCBI and store the raw XML.

Reads PMIDs from data/pmids_to_fetch.txt, fetches them from NCBI efetch in
batches and saves each response untouched as data/raw/batch_XXXX.xml.gz.
Nothing is parsed here: parsing and loading into Postgres is the job of
etl.load_pubmed_to_postgres, so the parser can change without re-downloading.

The run is resumable: PMIDs of finished batches are appended to
data/pmids_downloaded.txt and skipped next time. PMIDs from failed batches are
written to data/pmids_failed.txt.

Run from the project root:
    uv run python -m etl.download_pubmed_xml
"""
import gzip
import time
from itertools import batched
from pathlib import Path

import structlog

from api_clients.pubmed_api_client import PubMedAPIClient
from settings import load_env
from logs.logger_config import log_file_for, logged_job, setup_production_logging

logger = structlog.get_logger(__name__)

# CONFIG
DATA_DIR = Path(__file__).parent.parent / "data"
INPUT_FILE_PATH = DATA_DIR / "pmids_to_fetch.txt"
RAW_DIR = DATA_DIR / "raw"
DOWNLOADED_FILE_PATH = DATA_DIR / "pmids_downloaded.txt"
FAILED_FILE_PATH = DATA_DIR / "pmids_failed.txt"
N_BATCH_SIZE = 100  # NCBI recommends up to ~200 IDs per GET request
JOB_NAME = "download_pubmed_xml"
XML_MARKER = b"<PubmedArticleSet"  # a real efetch answer always contains this


class PubMedDownloader:
    """Downloads PubMed records as raw gzipped XML, one file per batch.

    All paths and the API client are constructor arguments, so tests can point
    the downloader at a temp folder and a fake client.

    Attributes:
        client: Client used to call NCBI efetch.
        input_path: Text file with one PMID per line.
        raw_dir: Folder that receives batch_XXXX.xml.gz files.
        downloaded_path: Text file listing PMIDs of finished batches.
        failed_path: Text file listing PMIDs of failed batches.
        batch_size: Number of PMIDs requested per call.
        delay_seconds: Pause between calls.
    """

    def __init__(
            self,
            client: PubMedAPIClient,
            *,
            input_path: Path = INPUT_FILE_PATH,
            raw_dir: Path = RAW_DIR,
            downloaded_path: Path = DOWNLOADED_FILE_PATH,
            failed_path: Path = FAILED_FILE_PATH,
            batch_size: int = N_BATCH_SIZE,
            delay_seconds: float | None = None,
    ) -> None:
        """Set up the downloader.

        Args:
            client: Client used to call NCBI efetch.
            input_path: Text file with one PMID per line.
            raw_dir: Folder that receives the raw XML files.
            downloaded_path: Where finished PMIDs are recorded.
            failed_path: Where PMIDs of failed batches are written.
            batch_size: Number of PMIDs requested per call.
            delay_seconds: Pause between calls. Defaults to the client's
                rate-limit-safe delay (depends on whether an API key is set).
        """
        self.client = client
        self.input_path = input_path
        self.raw_dir = raw_dir
        self.downloaded_path = downloaded_path
        self.failed_path = failed_path
        self.batch_size = batch_size
        self.delay_seconds = client.request_delay_seconds if delay_seconds is None else delay_seconds

    def read_pmids(self) -> list[str]:
        """Read the input PMIDs, skipping blank lines and duplicates.

        Returns:
            PMIDs in file order, each once.
        """
        with open(self.input_path) as f:
            return list(dict.fromkeys(line.strip() for line in f if line.strip()))

    def read_done_pmids(self) -> set[str]:
        """Read the PMIDs recorded as downloaded in an earlier run.

        Returns:
            Set of PMIDs, empty if nothing was downloaded yet.
        """
        if not self.downloaded_path.exists():
            return set()
        return {line.strip() for line in self.downloaded_path.read_text().splitlines() if line.strip()}

    def next_batch_number(self) -> int:
        """Find the number to use for the next raw file.

        Returns:
            One more than the highest existing batch number, or 1 if none.
        """
        numbers = [int(p.name.split("_")[1].split(".")[0]) for p in self.raw_dir.glob("batch_*.xml.gz")]
        return max(numbers, default=0) + 1

    def save_raw_batch(self, content: bytes, number: int) -> Path:
        """Gzip one efetch response into raw_dir/batch_XXXX.xml.gz.

        Writes to a temp file and renames it, so a crash never leaves a
        half-written batch that the loader would pick up.

        Args:
            content: Raw XML bytes exactly as returned by NCBI.
            number: Batch number used in the file name.

        Returns:
            Path of the saved file.
        """
        final = self.raw_dir / f"batch_{number:04d}.xml.gz"
        tmp = final.with_name(final.name + ".tmp")
        tmp.write_bytes(gzip.compress(content))
        tmp.replace(final)
        return final

    def download_batch(self, batch: list[str]) -> bytes:
        """Fetch one batch from NCBI and return the raw XML bytes.

        Args:
            batch: PMIDs to request.

        Returns:
            Raw XML bytes of the response.

        Raises:
            ValueError: If the response is not a PubmedArticleSet (NCBI
                sometimes answers 200 with an error document).
            Exception: Any network or HTTP error from the client.
        """
        response = self.client.fetch(id_list=batch, db="pubmed")
        response.raise_for_status()
        if XML_MARKER not in response.content:
            raise ValueError(f"response is not a PubmedArticleSet: {response.content[:120]!r}")
        return response.content

    def _write_failed(self, failed_pmids: list[str]) -> None:
        """Write the failed PMIDs file, or remove a stale one if nothing failed.

        Args:
            failed_pmids: PMIDs of batches that failed in this run.
        """
        if failed_pmids:
            self.failed_path.write_text("\n".join(failed_pmids) + "\n")
            logger.warning("failed_pmids_written", count=len(failed_pmids), path=str(self.failed_path))
        else:
            self.failed_path.unlink(missing_ok=True)

    def run(self) -> list[str]:
        """Download every PMID that is not downloaded yet.

        Returns:
            PMIDs from batches that failed (empty if all succeeded).
        """
        all_pmids = self.read_pmids()
        done = self.read_done_pmids()
        todo = [p for p in all_pmids if p not in done]
        logger.info(
            "download_started",
            total=len(all_pmids),
            already_done=len(done),
            to_fetch=len(todo),
            batch_size=self.batch_size,
            delay_seconds=self.delay_seconds,
            has_api_key=bool(getattr(self.client, "api_key", None)),
        )

        if not todo:
            logger.info("nothing_to_do")
            return []

        self.raw_dir.mkdir(parents=True, exist_ok=True)
        failed_pmids: list[str] = []
        n_batches = -(-len(todo) // self.batch_size)  # ceiling division
        file_number = self.next_batch_number()

        with open(self.downloaded_path, "a") as f_done:
            for i, batch in enumerate(batched(todo, self.batch_size), start=1):
                batch = list(batch)
                # every log line in this batch, from any module, gets these fields
                with structlog.contextvars.bound_contextvars(batch=i, n_batches=n_batches):
                    try:
                        content = self.download_batch(batch)
                        path = self.save_raw_batch(content, file_number)
                        file_number += 1
                        f_done.write("\n".join(batch) + "\n")
                        f_done.flush()  # record only after the file is safely on disk
                        logger.info("batch_done", requested=len(batch), bytes=len(content), file=path.name)
                    except Exception:
                        logger.exception("batch_failed", requested=len(batch), first_pmid=batch[0])
                        failed_pmids.extend(batch)
                time.sleep(self.delay_seconds)

        self._write_failed(failed_pmids)
        logger.info("download_done", requested=len(todo), failed_pmids=len(failed_pmids), raw_dir=str(self.raw_dir))
        return failed_pmids


def main() -> None:
    """Run the downloader with the default paths and a real NCBI client."""
    load_env()  # NCBI_API_KEY and NCBI_EMAIL_ADDRESS from .env
    with logged_job(JOB_NAME):
        PubMedDownloader(PubMedAPIClient()).run()


if __name__ == "__main__":
    setup_production_logging(log_file_for(JOB_NAME))  # once, at the application entry point
    main()
