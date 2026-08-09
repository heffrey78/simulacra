"""Probe and benchmark the local Ollama setup.

    python -m simulacra.llm.doctor            # probe + benchmark the configured model
    python -m simulacra.llm.doctor --all      # benchmark every installed chat model
    python -m simulacra.llm.doctor --think    # include a thinking-enabled run

Exists because plan.md's entire latency budget rests on measured tok/s. When the
hardware, the model or the Ollama version changes, re-run this before trusting
any of the tier assignments in §2.
"""

from __future__ import annotations

import argparse
import sys
import time

from ..config import ModelPolicy, Settings
from ..engine.judge import VERDICT_SCHEMA
from .client import OllamaClient

ROOM_PROMPT = [
    {"role": "system", "content": "You narrate a dungeon that is an imperfect copy of a "
     "vanished place. Second person, present tense. Two sentences, max 40 words."},
    {"role": "user", "content": "The Threshold. Exits: north. A jar of clean water lies here."},
]

JUDGE_PROMPT = [
    {"role": "system", "content": "You are a strict, fair referee. Be terse."},
    {"role": "user", "content": "Room: flooded crypt, a lit brazier on a plinth. "
     "Action: tip the brazier into the water."},
]

# From plan.md §2. A run that misses these means the budget needs revisiting.
TIER_TARGETS = {"tier 2 (streamed prose)": 12.0, "tier 1 (structured verdict)": 5.0}


def _bench(client: OllamaClient, policy: ModelPolicy, *, think: bool) -> None:
    policy = policy.with_(think=think)
    label = f"{policy.name}{' +think' if think else ''}"
    print(f"\n\033[1m{label}\033[0m")

    t0 = time.perf_counter()
    load = client.warm(policy)
    print(f"  warm/load        {load:6.2f}s")

    t0 = time.perf_counter()
    out = []
    for tok in client.stream(ROOM_PROMPT, policy.with_(num_predict=180), kind="tier2"):
        out.append(tok)
    wall2 = time.perf_counter() - t0
    text = "".join(out).strip()
    print(f"  tier 2 stream    {wall2:6.2f}s   {text[:150] or '(no content)'}")

    t0 = time.perf_counter()
    try:
        verdict = client.structured(
            JUDGE_PROMPT, VERDICT_SCHEMA, policy.with_(num_predict=120, temperature=0.3),
            kind="tier1",
        )
        wall1 = time.perf_counter() - t0
        print(f"  tier 1 verdict   {wall1:6.2f}s   {verdict}")
    except Exception as e:
        wall1 = time.perf_counter() - t0
        print(f"  tier 1 verdict   {wall1:6.2f}s   FAILED: {e}")

    for name, budget in zip(TIER_TARGETS, (wall2, wall1), strict=True):
        target = TIER_TARGETS[name]
        mark = "\033[32mOK\033[0m" if budget <= target else "\033[33mOVER\033[0m"
        print(f"    {mark:<18} {name}: {budget:.1f}s vs {target:.0f}s budget")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simulacra.llm.doctor")
    ap.add_argument("--all", action="store_true", help="benchmark every installed chat model")
    ap.add_argument("--think", action="store_true", help="also run with thinking enabled")
    ap.add_argument("--model", help="benchmark one specific model")
    args = ap.parse_args(argv)

    settings = Settings.load()
    with OllamaClient(settings) as client:
        ok, msg = client.health()
        print(f"host    {client.host}")
        print(f"health  {'OK' if ok else 'FAIL'} -- {msg}")
        if not ok:
            return 1

        installed = client.available_models()
        print(f"models  {', '.join(installed)}")

        if args.model:
            targets = [ModelPolicy(name=args.model)]
        elif args.all:
            targets = [
                ModelPolicy(name=m) for m in installed if "embed" not in m
            ]
        else:
            targets = [settings.chat]

        for policy in targets:
            _bench(client, policy, think=False)
            if args.think:
                _bench(client, policy, think=True)

        print("\n\033[1mtelemetry\033[0m")
        for kind, stats in client.stats().items():
            print(f"  {kind:<12} {stats}")

    print("\nCompare against plan.md §1-2. If tok/s has moved, the tier budgets move with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
