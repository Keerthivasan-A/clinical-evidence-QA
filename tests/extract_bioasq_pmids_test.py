import pandas as pd
import pytest
import structlog.testing

from etl.extract_bioasq_pmids import (
    extract_pmids_file,
    sample_questions,
    unique_pmids,
)


@pytest.fixture
def questions():
    return pd.DataFrame({
        "type": ["yesno", "yesno", "yesno", "list", "list", "list"],
        "pubmed_ids": ["1, 2", "2, 3", "4", "5", "6, 7", "8"],
    })


def test_sample_questions_takes_n_per_type(questions):
    sampled = sample_questions(questions, 2)
    assert len(sampled) == 4


def test_sample_questions_too_few_raises(questions):
    with pytest.raises(ValueError):
        sample_questions(questions, 5)


def test_unique_pmids_flattens_dedupes_and_strips():
    df = pd.DataFrame({"pubmed_ids": ["1, 2", "2,3 ", 4, ""]})
    assert unique_pmids(df).tolist() == ["1", "2", "3", "4"]


def test_extract_pmids_file_writes_one_per_line(questions, tmp_path):
    src, dst = tmp_path / "in.xlsx", tmp_path / "out.txt"
    questions.to_excel(src, index=False)
    n = extract_pmids_file(src, dst, n_per_group=3)
    lines = dst.read_text().split()
    assert n == len(lines) == len(set(lines)) == 8


def test_extract_pmids_file_missing_input_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_pmids_file(tmp_path / "none.xlsx", tmp_path / "out.txt")


def test_missing_input_logs_error(tmp_path):
    with structlog.testing.capture_logs() as logs, pytest.raises(FileNotFoundError):
        extract_pmids_file(tmp_path / "none.xlsx", tmp_path / "out.txt")
    assert logs[0]["event"] == "input_file_not_found"
    assert logs[0]["log_level"] == "error"
