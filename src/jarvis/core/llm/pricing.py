"""Operator-verified USD pricing evidence for cloud LLM preflight."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jarvis.core.llm.errors import LLMPricingUnavailable


@dataclass(frozen=True)
class PriceEvidence:
    model: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    source: str
    observed_at: datetime
    expires_at: datetime
    evidence_sha256: str
    max_input_tokens: int | None = None


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("missing timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def price_evidence(model: str, *, now: datetime | None = None) -> PriceEvidence:
    """Load a short-lived operator-verified price; reject unknown/stale data."""
    path = os.environ.get("JARVIS_LLM_PRICING_SNAPSHOT", "")
    if not path:
        raise LLMPricingUnavailable("LLM pricing evidence unavailable")
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65_536:
            raise ValueError("oversized pricing evidence")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("currency") != "USD":
            raise ValueError("USD pricing evidence required")
        source = data.get("source")
        verified_by = data.get("verified_by")
        if (not isinstance(source, str) or not source.startswith("https://") or
                not isinstance(verified_by, str) or not verified_by.strip()):
            raise ValueError("unverified pricing evidence")
        observed = _timestamp(data.get("observed_at"))
        expires = _timestamp(data.get("expires_at"))
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("current time must include timezone")
        if (observed > current or expires <= current or expires <= observed or
                expires - observed > timedelta(days=7)):
            raise ValueError("stale pricing evidence")
        models = data.get("models")
        rates = models.get(model) if isinstance(models, dict) else None
        if not isinstance(rates, dict):
            raise TypeError("unknown priced model")
        price_in = Decimal(str(rates["input_usd_per_million"]))
        price_out = Decimal(str(rates["output_usd_per_million"]))
        if not price_in.is_finite() or not price_out.is_finite() or price_in <= 0 or price_out <= 0:
            raise ValueError("invalid model rates")
        input_cap = rates.get("max_input_tokens")
        if input_cap is not None and (type(input_cap) is not int or input_cap <= 0):
            raise ValueError("invalid model input cap")
        return PriceEvidence(model, price_in, price_out, source, observed, expires,
                             hashlib.sha256(raw).hexdigest(), input_cap)
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as exc:
        raise LLMPricingUnavailable("LLM pricing evidence unavailable or invalid") from exc


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Calculate USD cost only with current explicit model rates."""
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError("token counts must be nonnegative")
    evidence = price_evidence(model)
    amount = (Decimal(tokens_in) * evidence.input_usd_per_million +
              Decimal(tokens_out) * evidence.output_usd_per_million) / Decimal(1_000_000)
    return float(amount)


def cost_rub(model: str, tokens_in: int, tokens_out: int) -> float:
    """Reject RUB conversion until a sourced, current FX contract exists."""
    raise LLMPricingUnavailable("RUB conversion requires verified FX evidence")


AITUNNEL_CATALOG = "https://api.aitunnel.ru/public/aitunnel/models/chat"
AITUNNEL_MODELS = frozenset({"qwen3.7-flash", "qwen3.7-plus", "qwen3.7-max", "gemini-2.5-flash-lite", "gpt-6-luna", "deepseek-v4-flash-0731"})


@dataclass(frozen=True)
class RubPriceEvidence:
    model: str
    reserved_rub: Decimal
    evidence_sha256: str
    input_cap: int
    output_cap: int


def aitunnel_price_evidence(path: Path, model: str, *, now: datetime,
                            input_cap: int = 3000, output_cap: int = 400,
                            conversation: bool = False) -> RubPriceEvidence:
    """Reserve full undiscounted RUB cost from a fresh public catalog snapshot."""
    try:
        raw = path.read_bytes()
        if len(raw) > 4_000_000 or model not in AITUNNEL_MODELS:
            raise ValueError("Unknown pinned model")
        data = json.loads(raw)
        observed = _timestamp(data["observed_at"])
        if (data["source"] != AITUNNEL_CATALOG or now.tzinfo is None or observed > now
                or now - observed > timedelta(hours=24) or type(input_cap) is not int
                or type(output_cap) is not int or not 1 <= input_cap <= (24000 if conversation else 6000) or not 1 <= output_cap <= 2048):
            raise ValueError("Invalid current price policy")
        row = data["models"][model]
        if ("text" not in row["modalities"]["input"] or "text" not in row["modalities"]["output"]
                or type(row["context_size"]) is not int or row["context_size"] < input_cap + output_cap
                or type(row["max_output"]) is not int or row["max_output"] < output_cap):
            raise ValueError("Unverified model capabilities")
        tiers = row.get("price_tiers") or [{"min_prompt_tokens": 0, "prompt_cost": row["prompt_cost"],
                                          "completion_cost": row["completion_cost"]}]
        if not isinstance(tiers, list) or not 1 <= len(tiers) <= 20:
            raise ValueError("Unknown tiers")
        rates = []
        previous = -1
        for tier in tiers:
            minimum = tier["min_prompt_tokens"]
            price_in, price_out = Decimal(str(tier["prompt_cost"])), Decimal(str(tier["completion_cost"]))
            if (type(minimum) is not int or minimum <= previous or minimum < 0
                    or not price_in.is_finite() or not price_out.is_finite() or price_in <= 0 or price_out <= 0):
                raise ValueError("Invalid tier")
            if minimum <= input_cap:
                rates.append((Decimal(input_cap) * price_in + Decimal(output_cap) * price_out) / Decimal(1_000_000))
            previous = minimum
        if tiers[0]["min_prompt_tokens"] != 0 or not rates:
            raise ValueError("Incomplete tiers")
        return RubPriceEvidence(model, max(rates), hashlib.sha256(raw).hexdigest(), input_cap, output_cap)
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise LLMPricingUnavailable("AITunnel RUB pricing or capabilities unavailable") from exc


def refresh_aitunnel_catalog(path: Path) -> None:
    """Explicit anonymous public refresh; never use credentials or a paid endpoint."""
    from urllib.request import urlopen
    with urlopen(AITUNNEL_CATALOG, timeout=30) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise LLMPricingUnavailable("Catalog exceeds size bound")
    models = json.loads(raw)
    selected = {model: models[model] for model in AITUNNEL_MODELS if model in models}
    body = {"source": AITUNNEL_CATALOG, "observed_at": datetime.now(UTC).isoformat(), "models": selected,
            "public_catalog_sha256": hashlib.sha256(raw).hexdigest()}
    # Exclusive creation avoids replacing an operator-approved snapshot silently.
    with path.open("x", encoding="utf-8") as target:
        json.dump(body, target, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Anonymous AITunnel pricing refresh; no paid requests")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    refresh_aitunnel_catalog(options.output)
