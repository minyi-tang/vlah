"""The rubric judge: scores a candidate prompt against hidden support situations.

    python3 judge.py <candidate_dir> --dataset dev|audit|judge

Prints one JSON line: {"score": mean, "per_seed": [...], "n": count,
"constraint_ok": bool, "words": count}. Each situation scores the share of its
required points the prompt covers, minus half a point per phrase it must avoid,
clamped to [0, 1]. A prompt over the word limit fails the constraint and scores 0.
Workers never see this file or cases.json.
"""

import argparse
import json
import re
from pathlib import Path

CASES = Path(__file__).resolve().parent / "cases.json"
AVOID_PENALTY = 0.5


def normalize(text: str) -> str:
    text = text.lower().replace("don't", "do not").replace("can't", "cannot")
    return re.sub(r"\s+", " ", text)


def case_score(prompt: str, case: dict) -> float:
    required = case["must_mention"]
    hits = sum(1 for phrase in required if phrase.lower() in prompt)
    misses = sum(1 for phrase in case["must_avoid"] if phrase.lower() in prompt)
    score = hits / len(required) - AVOID_PENALTY * misses
    return max(0.0, min(1.0, score))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("--dataset", default="dev", choices=["dev", "audit", "judge"])
    arguments = parser.parse_args()
    prompt_path = arguments.candidate_dir / "prompt.md"
    if not prompt_path.is_file():
        print(json.dumps({"score": None, "error": "the candidate has no prompt.md"}))
        return 0
    else:
        pass
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    dataset = "audit" if arguments.dataset == "audit" else "dev"
    prompt_text = prompt_path.read_text(encoding="utf-8")
    words = len(prompt_text.split())
    prompt = normalize(prompt_text)
    per_case = [case_score(prompt, case) for case in cases[dataset]]
    constraint_ok = words <= cases["max_words"]
    score = sum(per_case) / len(per_case) if constraint_ok else 0.0
    print(
        json.dumps(
            {
                "score": score,
                "per_seed": per_case if constraint_ok else [0.0] * len(per_case),
                "n": len(per_case),
                "constraint_ok": constraint_ok,
                "words": words,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
