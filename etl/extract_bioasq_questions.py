"""Extract id, type and PubMed IDs of the gold documents from BioASQ_raw.json into an Excel file."""
import argparse
import json
import re
from pathlib import Path

import structlog
from openpyxl import Workbook
from openpyxl.styles import Font

from logs.logger_config import log_file_for, logged_job, setup_production_logging

logger = structlog.get_logger(__name__)

JOB_NAME = "extract_bioasq_questions"
PMID_RE = re.compile(r"/pubmed/(\d+)\s*$")


def extract_pmids(urls):
    """Return the PMIDs from a list of PubMed URLs, keeping order and dropping duplicates."""
    pmids = []
    for url in urls or []:
        match = PMID_RE.search(url)
        if match and match.group(1) not in pmids:
            pmids.append(match.group(1))
    return pmids


def main():
    """Read the BioASQ JSON and write the questions with their PMIDs to Excel."""
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "data" / "BioASQ_raw.json")
    parser.add_argument("--output", type=Path, default=root / "data" / "BioASQ_extracted.xlsx")
    args = parser.parse_args()

    with logged_job(JOB_NAME):
        write_questions_excel(args.input, args.output)


def write_questions_excel(input_path, output_path):
    """Write the questions of a BioASQ JSON file, with their PMIDs, to Excel.

    Args:
        input_path: BioASQ JSON file with a top-level "questions" list.
        output_path: Excel file to write.
    """
    logger.info("extract_started", input=str(input_path), output=str(output_path))
    with Path(input_path).open(encoding="utf-8") as f:
        questions = json.load(f)["questions"]

    wb = Workbook()
    ws = wb.active
    ws.title = "questions"
    ws.append(["id", "type", "pubmed_ids", "num_documents"])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for q in questions:
        pmids = extract_pmids(q.get("documents"))
        ws.append([q["id"], q["type"], ", ".join(pmids), len(pmids)])

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col, width in zip("ABCD", (28, 10, 60, 15)):
        ws.column_dimensions[col].width = width

    wb.save(output_path)
    logger.info("extract_done", questions=len(questions), output=str(output_path))


if __name__ == "__main__":
    setup_production_logging(log_file_for(JOB_NAME))  # once, at the application entry point
    main()
