"""Sample BioASQ questions per type and write their unique PMIDs to a text file.

Reads data/BioASQ_extracted.xlsx (made by etl.extract_bioasq_questions), takes
N_SAMPLES_PER_GROUP questions of each question type and writes the unique PMIDs
of those questions to data/pmids_to_fetch.txt, one per line.

Run from the project root:
    uv run python -m etl.extract_bioasq_pmids
"""
from pathlib import Path

import pandas as pd
import structlog

from logs.logger_config import log_file_for, logged_job, setup_production_logging

logger = structlog.get_logger(__name__)

# CONFIG
DATA_DIR = Path(__file__).parent.parent / "data"
INPUT_FILE_PATH = DATA_DIR / "BioASQ_extracted.xlsx"
OUTPUT_FILE_PATH = DATA_DIR / "pmids_to_fetch.txt"
JOB_NAME = "extract_bioasq_pmids"
PUBMED_ID_COLUMN = "pubmed_ids"
N_SAMPLES_PER_GROUP = 2


def sample_questions(df: pd.DataFrame, n_per_group: int = N_SAMPLES_PER_GROUP) -> pd.DataFrame:
    """Pick the same number of random questions from each question type.

    Args:
        df: Questions with a "type" column.
        n_per_group: Questions to take per type (fixed random seed).

    Returns:
        The sampled rows.

    Raises:
        ValueError: If a type has fewer than n_per_group questions.
    """
    return df.groupby("type", group_keys=False).apply(lambda x: x.sample(n=n_per_group, random_state=42))


def unique_pmids(df: pd.DataFrame) -> pd.Series:
    """Flatten the comma-separated PMID column into unique PMIDs.

    Args:
        df: Questions whose PUBMED_ID_COLUMN holds "1, 2, 3" style strings.

    Returns:
        Each PMID once, in first-seen order.
    """
    lists = df[PUBMED_ID_COLUMN].apply(lambda cell: [p.strip() for p in str(cell).split(",") if p.strip()])
    return lists.explode().dropna().drop_duplicates()


def extract_pmids_file(
        input_path: Path = INPUT_FILE_PATH,
        output_path: Path = OUTPUT_FILE_PATH,
        n_per_group: int = N_SAMPLES_PER_GROUP,
) -> int:
    """Sample questions and write their unique PMIDs to a text file.

    Args:
        input_path: Excel file made by etl.extract_bioasq_questions.
        output_path: Text file to write, one PMID per line.
        n_per_group: Questions to sample per question type.

    Returns:
        Number of PMIDs written.

    Raises:
        FileNotFoundError: If input_path does not exist.
    """
    if not Path(input_path).is_file():
        logger.error("input_file_not_found", path=str(input_path))
        raise FileNotFoundError(f"File not found: {input_path}")

    df = pd.read_excel(input_path)
    sampled = sample_questions(df, n_per_group)
    pmids = unique_pmids(sampled)
    pmids.to_csv(output_path, index=False, header=False)
    logger.info(
        "pmids_extracted",
        questions=len(df),
        sampled=len(sampled),
        pmids=len(pmids),
        output=str(output_path),
    )
    return len(pmids)


def main() -> None:
    """Run the extraction with the default input and output paths."""
    with logged_job(JOB_NAME):
        extract_pmids_file()


if __name__ == "__main__":
    setup_production_logging(log_file_for(JOB_NAME))  # once, at the application entry point
    main()
