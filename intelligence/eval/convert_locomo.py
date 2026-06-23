"""Convert the raw LoCoMo dataset into the normalized benchmark schema.

Source: snap-research/locomo, ``data/locomo10.json`` (CC BY-NC 4.0) — 10
multi-session dialogues with QA annotations whose ``evidence`` references
dialogue turn ids (e.g. ``D1:3``).

Each dialogue turn becomes a memory (id namespaced by conversation so the 10
dialogues stay isolated under separate tenant scopes); each answerable QA pair
becomes a query whose ``relevant_ids`` are its evidence turns. Adversarial/
unanswerable questions (no usable evidence) are dropped — they have no retrieval
target.

    python -m eval.convert_locomo --in locomo10.json --out locomo.normalized.json
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def convert(raw: list[dict[str, Any]]) -> dict[str, Any]:
    memories: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []

    for sample in raw:
        sid = str(sample.get("sample_id", ""))
        conv = sample.get("conversation", {}) or {}
        emitted: set[str] = set()

        for value in conv.values():
            if not isinstance(value, list):  # skip speaker_a/b + *_date_time
                continue
            for turn in value:
                if not isinstance(turn, dict) or "dia_id" not in turn:
                    continue
                mem_id = f"{sid}:{turn['dia_id']}"
                text = f"{turn.get('speaker', '')}: {turn.get('text', '')}".strip()
                memories.append(
                    {"id": mem_id, "content": text, "kind": "episode", "scope": {"tenant_id": sid}}
                )
                emitted.add(mem_id)

        for qa in sample.get("qa", []) or []:
            relevant = [f"{sid}:{e}" for e in (qa.get("evidence") or [])]
            relevant = [r for r in relevant if r in emitted]  # keep only real turns
            if not relevant:
                continue
            queries.append(
                {
                    "query": qa.get("question", ""),
                    "relevant_ids": relevant,
                    "scope": {"tenant_id": sid},
                    "expected_answer": qa.get("answer", ""),
                    "category": qa.get("category"),
                }
            )

    return {"memories": memories, "queries": queries}


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert raw LoCoMo to the normalized benchmark schema.")
    parser.add_argument("--in", dest="inp", required=True, help="path to raw locomo10.json")
    parser.add_argument("--out", dest="out", required=True, help="path to write the normalized dataset")
    args = parser.parse_args()

    with open(args.inp, encoding="utf-8") as fh:
        raw = json.load(fh)
    dataset = convert(raw)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(dataset, fh)
    print(f"memories={len(dataset['memories'])} queries={len(dataset['queries'])}")


if __name__ == "__main__":
    main()
