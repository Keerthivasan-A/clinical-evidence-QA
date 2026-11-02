"""Download BioASQ PubMed documents from NCBI and store the raw XML.

Reads PMIDs from data/pmdids_to_fetct.txt, fetches them from NCBI efetch in
batches and saves each response untouched as data/raw/batch_XXXX.xml.gz.
Nothing is parsed here: parsing and loading into Postgres is the job of
etl.load_pubmed_to_postgres, so the parser can change without re-downloading.

The run is resumable: PMIDs of finished batches are appended to
data/downloaded_pmids.txt and skipped next time. PMIDs from failed batches are
written to data/pubmed_failed_pmids.txt.

Run from the project root:
    uv run python -m etl.bioasq_download_pubmed_docs
"""
import gzip
import time
from itertools import batched
from pathlib import Path

import structlog

from api_clients.pubmed_api_client import PubMedAPIClient
from logs.logger_config import setup_production_logging

logger = structlog.get_logger(__name__)

# CONFIG
DATA_DIR = Path(__file__).parent.parent / "data"
INPUT_FILE_PATH = DATA_DIR / "pmdids_to_fetct.txt"
RAW_DIR = DATA_DIR / "raw"
DOWNLOADED_FILE_PATH = DATA_DIR / "downloaded_pmids.txt"
FAILED_FILE_PATH = DATA_DIR / "pubmed_failed_pmids.txt"
N_BATCH_SIZE = 200  # NCBI recommends up to ~200 IDs per GET request
XML_MARKER = b"<PubmedArticleSet"  # a real efetch answer always contains this


def read_pmids(path: Path) -> list[str]:
    """Read PMIDs from a text file, one per line, skipping blanks and duplicates."""
    with open(path) as f:
        return list(dict.fromkeys(line.strip() for line in f if line.strip()))


def read_done_pmids(path: Path) -> set[str]:
    """Return the PMIDs recorded as downloaded in an earlier run."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def next_batch_number(raw_dir: Path) -> int:
    """Return the number for the next raw file, one past the highest existing."""
    numbers = [int(p.name.split("_")[1].split(".")[0]) for p in raw_dir.glob("batch_*.xml.gz")]
    return max(numbers, default=0) + 1


def save_raw_batch(content: bytes, raw_dir: Path, number: int) -> Path:
    """Gzip one efetch response into raw_dir/batch_XXXX.xml.gz.

    Writes to a temp file and renames it, so a crash never leaves a
    half-written batch that the loader would pick up.
    """
    final = raw_dir / f"batch_{number:04d}.xml.gz"
    tmp = final.with_name(final.name + ".tmp")
    tmp.write_bytes(gzip.compress(content))
    tmp.replace(final)
    return final


def download_batch(client: PubMedAPIClient, batch: list[str]) -> bytes:
    """Fetch one batch from NCBI and return the raw XML bytes.

    Raises:
        ValueError: If the response is not a PubmedArticleSet (NCBI sometimes
            answers 200 with an error document).
        Exception: Any network or HTTP error from the client.
    """
    response = client.fetch(id_list=batch, db="pubmed")
    response.raise_for_status()
    if XML_MARKER not in response.content:
        raise ValueError(f"response is not a PubmedArticleSet: {response.content[:120]!r}")
    return response.content


def main() -> None:
    all_pmids = read_pmids(INPUT_FILE_PATH)
    done = read_done_pmids(DOWNLOADED_FILE_PATH)
    todo = [p for p in all_pmids if p not in done]
    logger.info("pmids_loaded", total=len(all_pmids), already_done=len(done), to_fetch=len(todo))

    if not todo:
        logger.info("nothing_to_do")
        return

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client = PubMedAPIClient()
    failed_pmids: list[str] = []
    n_batches = -(-len(todo) // N_BATCH_SIZE)  # ceiling division
    file_number = next_batch_number(RAW_DIR)

    with open(DOWNLOADED_FILE_PATH, "a") as f_done:
        for i, batch in enumerate(batched(todo, N_BATCH_SIZE), start=1):
            batch = list(batch)
            structlog.contextvars.bind_contextvars(batch=i, n_batches=n_batches)
            try:
                content = download_batch(client, batch)
                path = save_raw_batch(content, RAW_DIR, file_number)
                file_number += 1
                f_done.write("\n".join(batch) + "\n")
                f_done.flush()  # record only after the file is safely on disk
                logger.info("batch_done", requested=len(batch), bytes=len(content), file=path.name)
            except Exception:
                logger.exception("batch_failed", first_pmid=batch[0])
                failed_pmids.extend(batch)
            finally:
                structlog.contextvars.clear_contextvars()
            time.sleep(client.request_delay_seconds)

    if failed_pmids:
        FAILED_FILE_PATH.write_text("\n".join(failed_pmids) + "\n")
    else:
        FAILED_FILE_PATH.unlink(missing_ok=True)  # no stale failures from an older run

    logger.info("download_done", requested=len(todo), failed_pmids=len(failed_pmids), raw_dir=str(RAW_DIR))


if __name__ == "__main__":
    setup_production_logging()  # once, at the application entry point
    main()
