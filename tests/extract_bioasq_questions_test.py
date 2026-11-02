import json

import structlog.testing

from openpyxl import load_workbook

from etl import extract_bioasq_questions
from etl.extract_bioasq_questions import extract_pmids


def test_extract_pmids_dedupes_and_keeps_order():
    urls = ["http://www.ncbi.nlm.nih.gov/pubmed/22", "http://www.ncbi.nlm.nih.gov/pubmed/11",
            "http://www.ncbi.nlm.nih.gov/pubmed/22"]
    assert extract_pmids(urls) == ["22", "11"]


def test_extract_pmids_ignores_non_pubmed_and_none():
    assert extract_pmids(["http://example.com/x"]) == []
    assert extract_pmids(None) == []


def test_main_writes_excel(tmp_path, monkeypatch):
    src, dst = tmp_path / "in.json", tmp_path / "out.xlsx"
    src.write_text(json.dumps({"questions": [
        {"id": "q1", "type": "yesno",
         "documents": ["http://www.ncbi.nlm.nih.gov/pubmed/1", "http://www.ncbi.nlm.nih.gov/pubmed/2"]},
        {"id": "q2", "type": "list"},
    ]}))
    monkeypatch.setattr("sys.argv", ["x", "--input", str(src), "--output", str(dst)])
    extract_bioasq_questions.main()
    rows = list(load_workbook(dst)["questions"].iter_rows(values_only=True))
    assert rows[0] == ("id", "type", "pubmed_ids", "num_documents")
    assert rows[1] == ("q1", "yesno", "1, 2", 2)
    assert rows[2] == ("q2", "list", None, 0)


def test_main_logs_events_instead_of_printing(tmp_path, monkeypatch, capsys):
    src, dst = tmp_path / "in.json", tmp_path / "out.xlsx"
    src.write_text(json.dumps({"questions": []}))
    monkeypatch.setattr("sys.argv", ["x", "--input", str(src), "--output", str(dst)])
    with structlog.testing.capture_logs() as logs:
        extract_bioasq_questions.main()
    assert [e["event"] for e in logs] == ["job_started", "extract_started", "extract_done", "job_finished"]
    assert capsys.readouterr().out == ""
