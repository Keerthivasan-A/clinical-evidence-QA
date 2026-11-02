"""Extract id, type and PubMed IDs of the gold documents from BioASQ_raw.json into an Excel file."""
import argparse
import json
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

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
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "data" / "BioASQ_raw.json")
    parser.add_argument("--output", type=Path, default=root / "data" / "BioASQ_extracted.xlsx")
    args = parser.parse_args()

    with args.input.open(encoding="utf-8") as f:
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

    wb.save(args.output)
    print(f"Wrote {len(questions)} questions to {args.output}")


if __name__ == "__main__":
    main()
