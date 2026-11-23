"""Data augmentation for transaction categorization.

Three strategies:
1. Merchant variation (replace merchant name with synonym from KB)
2. Pattern variation (add/remove "Операция по карте", "RUS", city suffixes)
3. Case variation (UPPER, lower, Title)
"""
from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jarvis.finance.ml_local.merchant_kb import MERCHANT_KB


def load_samples(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_kb_from_samples(samples: list[dict[str, Any]]) -> dict[str, list[list[str]]]:
    """Auto-generate KB: category -> list of [merchant variants]."""
    kb_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    
    # 1. Populate from predefined MERCHANT_KB
    for entry in MERCHANT_KB:
        for pat in entry.patterns:
            kb_counts[entry.category][pat] += 5
            
    # 2. Populate from training samples
    for s in samples:
        text = s["text"].lower()
        label = s["label"]
        words = text.split()
        if words and len(words[0]) > 2:
            first_word = words[0].strip(".,!?:;*")
            kb_counts[label][first_word] += 1

    # Convert to category -> list of [merchant]
    result: dict[str, list[list[str]]] = {}
    for label, merchants in kb_counts.items():
        top = sorted(merchants.items(), key=lambda x: -x[1])[:20]
        result[label] = [[m] for m, _ in top]
    return result


def augment_sample(sample: dict[str, Any], kb: dict[str, Any], n_variations: int = 5, rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Generate n variations of a sample."""
    if rng is None:
        rng = random.Random(42)

    text = sample["text"]
    label = sample["label"]
    variations: list[dict[str, Any]] = []
    
    # Strategy 1: Case variations
    variations.append({"text": text.upper(), "label": label})
    variations.append({"text": text.lower(), "label": label})
    variations.append({"text": text.title(), "label": label})

    # Strategy 2: Pattern variations (strip/add suffixes/prefixes)
    normalized = " ".join(text.split())
    variations.append({"text": normalized, "label": label})

    suffix_patterns = [
        " MOSCOW RUS",
        " ST PETERSBURG RUS",
        " RUS",
        ". Операция по карте ****1234",
        ". Операция по счету ****5678",
    ]
    for sfx in suffix_patterns:
        if sfx not in text:
            variations.append({"text": text + sfx, "label": label})

    # Strategy 3: Replace merchant with synonym from KB
    synonyms = kb.get(label, [])
    if synonyms:
        words = text.split()
        if words:
            for variant_group in synonyms[:5]:
                for syn in variant_group:
                    new_text = syn.capitalize() + " " + " ".join(words[1:])
                    variations.append({"text": new_text, "label": label})

    rng.shuffle(variations)
    return variations[:n_variations]


def augment_dataset(
    input_path: str,
    output_path: str,
    target_size: int = 4000,
    seed: int = 42,
) -> dict[str, Any]:
    """Augment dataset until target_size is reached, deterministically."""
    rng = random.Random(seed)
    samples = load_samples(input_path)
    if not samples:
        raise ValueError(f"No samples loaded from {input_path}")

    kb = build_kb_from_samples(samples)
    
    augmented = list(samples)  # keep originals
    
    # Cycle through samples deterministically
    idx = 0
    while len(augmented) < target_size:
        sample = samples[idx % len(samples)]
        vars_gen = augment_sample(sample, kb, n_variations=3, rng=rng)
        for v in vars_gen:
            augmented.append(v)
            if len(augmented) >= target_size:
                break
        idx += 1
        
    augmented = augmented[:target_size]
    rng.shuffle(augmented)
    
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(s, ensure_ascii=False) + "\n" for s in augmented)
            
    dist = Counter(s["label"] for s in augmented)
    return {
        "original": len(samples),
        "augmented": len(augmented),
        "distribution": dict(dist.most_common()),
        "output_path": str(out_file),
    }
