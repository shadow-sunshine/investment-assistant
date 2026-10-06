"""一次性校验：v2 holdout 与 R0 双层独立性 + 样本计数 + SHA256。

只读取两个 JSON，不写任何文件、不跑检索。输出重定向到 _r3v2_indep_out.txt。
"""
import hashlib
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
V2 = DATA / "r3_holdout_eval_set_v2.json"
R0 = DATA / "bilingual_eval_set.json"


def shaf(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def facts(case: dict) -> set[tuple]:
    out = set()
    for p in case.get("target_pages", []):
        for kw in case.get("keywords", []):
            out.add((case["ticker"], int(p), norm(kw)))
    return out


v2 = json.loads(V2.read_text(encoding="utf-8-sig"))
r0 = json.loads(R0.read_text(encoding="utf-8-sig"))

v2_cases = v2["cases"]
r0_facts = set()
r0_questions = set()
for c in r0["cases"]:
    r0_questions.add((c["ticker"], norm(c["question"])))
    r0_facts |= facts(c)

# Level 1: (ticker, question)
lvl1 = []
for c in v2_cases:
    if (c["ticker"], norm(c["question"])) in r0_questions:
        lvl1.append((c["id"], c["ticker"], c["question"]))

# Level 2: (ticker, target_page, normalized keyword)
lvl2 = []
for c in v2_cases:
    for f in facts(c):
        if f in r0_facts:
            lvl2.append((c["id"], f))

# Counts
from collections import Counter
qcount = Counter(c["quadrant"] for c in v2_cases)

lines = []
lines.append(f"v2 file: {V2}")
lines.append(f"v2 sha256: {shaf(V2)}")
lines.append(f"v2 total cases: {len(v2_cases)}")
lines.append(f"v2 per-quadrant: {dict(qcount)}")
lines.append(f"level1_overlaps (ticker,question vs R0): {len(lvl1)}")
for x in lvl1:
    lines.append(f"  L1 DUP: {x}")
lines.append(f"level2_overlaps (ticker,page,keyword vs R0): {len(lvl2)}")
for x in lvl2:
    lines.append(f"  L2 DUP: {x}")
lines.append("RESULT: " + ("INDEPENDENT" if not lvl1 and not lvl2 and len(v2_cases) == 32 and dict(qcount) == {'zh-zh':8,'en-en':8,'zh-en':8,'en-zh':8} else "FAIL"))

Path(__file__).parent.joinpath("_r3v2_indep_out.txt").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
