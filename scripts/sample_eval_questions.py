"""Stratified sample of BioASQ questions for the retrieval evaluation set.

90 standard questions: equal per question type, spread evenly over gold-doc-count buckets.
Questions whose gold PMIDs are all in the already-downloaded set are taken first, so
existing downloads are reused. Outputs go to data/ (existing files are not touched).
"""
import json
import random
from collections import Counter
from pathlib import Path

SEED = 42
DATA = Path(__file__).resolve().parent.parent / "data"
QUOTA = {"factoid": 23, "yesno": 23, "summary": 22, "list": 22}  # 90 total
BUCKETS = [(1, 1), (2, 4), (5, 10), (11, 20)]  # gold-doc-count buckets
MAX_DOCS = 20


def pmid(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[1]


def bucket_of(n: int):
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= n <= hi:
            return i
    return None


def main() -> None:
    qs = json.load(open(DATA / "BioASQ_raw.json"))["questions"]
    have = {
        l.strip() for l in open(DATA / "pmids_downloaded.txt") if l.strip()
    }

    ok = []
    for q in qs:
        body = q.get("body")  # drops the malformed `_body` records
        docs = {pmid(u) for u in q.get("documents", [])}
        if not body or len(body.split()) < 4 or not q.get("snippets"):
            continue
        if not 1 <= len(docs) <= MAX_DOCS:
            continue
        ok.append((q, sorted(docs)))

    rng = random.Random(SEED)
    rng.shuffle(ok)
    # questions already fully downloaded go first
    ok.sort(key=lambda t: not set(t[1]) <= have)

    picked = []
    for qtype, total in QUOTA.items():
        per = [total // len(BUCKETS) + (i < total % len(BUCKETS)) for i in range(len(BUCKETS))]
        pool = [t for t in ok if t[0]["type"] == qtype]
        chosen_ids = set()
        for b, want in enumerate(per):
            for q, docs in pool:
                if want == 0:
                    break
                if q["id"] in chosen_ids or bucket_of(len(docs)) != b:
                    continue
                chosen_ids.add(q["id"])
                picked.append((q, docs))
                want -= 1

    out = [
        {
            "id": q["id"],
            "type": q["type"],
            "input": q["body"],
            "gold_pmids": docs,
        }
        for q, docs in picked
    ]
    all_pmids = sorted({p for r in out for p in r["gold_pmids"]}, key=int)
    missing = [p for p in all_pmids if p not in have]

    (DATA / "eval_questions_sample.json").write_text(json.dumps(out, indent=2))
    (DATA / "eval_pmids_all.txt").write_text("\n".join(all_pmids) + "\n")
    (DATA / "eval_pmids_to_fetch.txt").write_text("\n".join(missing) + "\n")

    print("questions:", len(out), dict(Counter(r["type"] for r in out)))
    print("gold-count buckets:", dict(sorted(Counter(bucket_of(len(r["gold_pmids"])) for r in out).items())))
    print("unique gold PMIDs:", len(all_pmids), "| already downloaded:", len(all_pmids) - len(missing), "| to fetch:", len(missing))


if __name__ == "__main__":
    main()
