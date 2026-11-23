from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, cast

import pandas as pd

from jarvis.invest.config.settings import get_settings
from jarvis.invest.core.interfaces import OrderRequest, PortfolioAllocator
from jarvis.invest.portfolio.plan import PortfolioPlan, load_portfolio_plan


def _normalize_positions(portfolio: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_positions = portfolio.get("positions", {})
    if isinstance(raw_positions, dict):
        return {
            instrument_id: {
                "quantity": int(payload.get("quantity", 0)),
                "average_price": float(payload.get("average_price", 0.0)),
                "market_value": float(payload.get("market_value", 0.0)),
                "sector_name": str(payload.get("sector_name", "UNKNOWN")),
            }
            for instrument_id, payload in raw_positions.items()
            if isinstance(payload, dict)
        }
    if isinstance(raw_positions, list):
        normalized: dict[str, dict[str, object]] = {}
        for payload in raw_positions:
            if not isinstance(payload, dict) or "instrument_id" not in payload:
                continue
            instrument_id = str(payload["instrument_id"])
            normalized[instrument_id] = {
                "quantity": int(payload.get("quantity", 0)),
                "average_price": float(payload.get("average_price", 0.0)),
                "market_value": float(payload.get("market_value", 0.0)),
                "sector_name": str(payload.get("sector_name", "UNKNOWN")),
            }
        return normalized
    return {}


def _skip_reason_bucket(reason: str) -> str:
    if reason.startswith("excluded_") or reason == "missing_market_data":
        return "data_filters_triggered"
    return "risk_filters_triggered"


class TargetWeightAllocator(PortfolioAllocator):
    def __init__(self, plan: PortfolioPlan | None = None) -> None:
        self.settings = get_settings()
        self.plan = plan or load_portfolio_plan()
        self.last_summary: dict[str, object] = {}

    @staticmethod
    def _upsert_skip(
        skipped: dict[str, dict[str, object]],
        instrument_id: str,
        *,
        reason: str,
        target_weight: float,
        current_weight: float,
        sector_name: str,
    ) -> None:
        entry = skipped.setdefault(
            instrument_id,
            {
                "instrument_id": instrument_id,
                "target_weight": target_weight,
                "current_weight": current_weight,
                "deviation_from_target": current_weight - target_weight,
                "sector_name": sector_name,
                "reasons": [],
                "data_filters_triggered": [],
                "risk_filters_triggered": [],
                "why_not_selected": "",
                "why_not_in_candidates": "",
            },
        )
        reasons = entry["reasons"]
        if isinstance(reasons, list) and reason not in reasons:
            reasons.append(reason)
        bucket = _skip_reason_bucket(reason)
        bucket_values = entry.get(bucket, [])
        if isinstance(bucket_values, list) and reason not in bucket_values:
            bucket_values.append(reason)
        entry["why_not_selected"] = "; ".join(str(item) for item in cast(list[Any], entry.get("reasons", [])))

    def allocate(self, rankings: pd.DataFrame, portfolio: dict[str, object], cash: float) -> list[OrderRequest]:
        universe = rankings.copy()
        candidate_lookup_raw = portfolio.get("candidate_lookup", {})
        candidate_lookup = (
            {str(key).upper(): value for key, value in candidate_lookup_raw.items()}
            if isinstance(candidate_lookup_raw, dict)
            else {}
        )
        portfolio.get("model_overlay", {})
        precomputed_skips_raw = portfolio.get("precomputed_skips", {})
        precomputed_skips = (
            {str(key).upper(): value for key, value in precomputed_skips_raw.items()}
            if isinstance(precomputed_skips_raw, dict)
            else {}
        )
        if universe.empty:
            self.last_summary = {
                "plan": self.plan.as_dict(),
                "current_cash": float(cast(Any, portfolio.get("cash", 0.0) or 0.0)),
                "reserve_cash_target": 0.0,
                "investable_cash": 0.0,
                "remaining_investable_cash": 0.0,
                "estimated_total_fees": 0.0,
                "estimated_total_notional": 0.0,
                "why_proposal_generated": "Нет рыночных данных для расчёта предложений.",
                "risk_flags": ["empty_market_universe"],
                "user_warnings": ["Нет рыночных данных: сначала обновите локальный набор цен и инструментов."],
                "skipped_instruments": [],
            }
            return []
        universe["instrument_id"] = universe["instrument_id"].astype(str).str.upper()
        if "lot_size" not in universe.columns:
            universe["lot_size"] = 1
        latest = universe.drop_duplicates("instrument_id", keep="last").set_index("instrument_id")
        positions = _normalize_positions(portfolio)
        current_cash = float(cast(Any, portfolio.get("cash") or 0.0))
        equity_raw = portfolio.get("equity")
        if equity_raw is not None:
            equity = float(cast(Any, equity_raw))
        else:
            equity = current_cash + sum(float(cast(Any, item.get("market_value", 0.0))) for item in positions.values())
        reserve_cash_raw = portfolio.get("reserve_cash_target")
        reserve_cash_target = float(cast(Any, reserve_cash_raw)) if reserve_cash_raw is not None else equity * self.plan.reserve_cash_pct
        deployable_cash = float(min(self.plan.monthly_budget, max(0.0, cash - reserve_cash_target)))
        investable_cash_before_orders = deployable_cash
        commission_rate = float(cast(Any, portfolio.get("commission_bps", self.settings.paper_fee_bps))) / 10_000
        turnover_budget = equity * self.settings.risk_max_daily_turnover

        current_notional_by_instrument: dict[str, float] = {}
        current_sector_notional: defaultdict[str, float] = defaultdict(float)
        current_quantity_by_instrument: dict[str, int] = {}
        for instrument_id, payload in positions.items():
            if instrument_id in latest.index:
                row = latest.loc[instrument_id]
                current_price = float(row["close"])
                sector_name = str(row.get("sector_name", payload.get("sector_name", "UNKNOWN")))
            else:
                current_price = float(cast(Any, payload.get("average_price", 0.0)))
                sector_name = str(payload.get("sector_name", "UNKNOWN"))
            quantity = int(cast(Any, payload.get("quantity", 0)))
            notional = quantity * current_price
            current_quantity_by_instrument[instrument_id] = quantity
            current_notional_by_instrument[instrument_id] = notional
            current_sector_notional[sector_name] += notional

        investable_base = sum(current_notional_by_instrument.values()) + deployable_cash
        if investable_base <= 0:
            self.last_summary = {
                "plan": self.plan.as_dict(),
                "current_cash": current_cash,
                "reserve_cash_target": reserve_cash_target,
                "investable_cash": investable_cash_before_orders,
                "remaining_investable_cash": deployable_cash,
                "estimated_total_fees": 0.0,
                "estimated_total_notional": 0.0,
                "why_proposal_generated": "После reserve cash свободных средств для инвестирования не осталось.",
                "risk_flags": ["no_deployable_cash"],
                "user_warnings": ["После reserve cash инвестировать в этом месяце нечего."],
                "skipped_instruments": [],
            }
            return []

        target_weights = self.plan.target_weights
        tracked_instruments = sorted(set(target_weights) | set(current_notional_by_instrument))
        current_weights = {
            instrument_id: current_notional_by_instrument.get(instrument_id, 0.0) / investable_base
            for instrument_id in tracked_instruments
            if investable_base > 0
        }
        proposal_flags: list[str] = []
        skipped_instruments: dict[str, dict[str, object]] = {}
        for instrument_id, payload in precomputed_skips.items():
            target_weight = float(self.plan.target_weights.get(instrument_id, cast(Any, payload.get("target_weight", 0.0) if isinstance(payload, dict) else 0.0)))
            current_weight = current_weights.get(instrument_id, 0.0)
            sector_name = (
                str(payload.get("sector_name", "UNKNOWN"))
                if isinstance(payload, dict)
                else "UNKNOWN"
            )
            reasons = payload.get("reasons", []) if isinstance(payload, dict) else []
            for reason in reasons if isinstance(reasons, list) else []:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason=str(reason),
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
            entry = skipped_instruments.get(instrument_id)
            if isinstance(entry, dict) and isinstance(payload, dict):
                entry["why_not_selected"] = str(payload.get("why_not_selected", entry.get("why_not_selected", "")))
                entry["why_not_in_candidates"] = str(payload.get("why_not_in_candidates", entry.get("why_not_in_candidates", "")))
                for bucket in ("data_filters_triggered", "risk_filters_triggered"):
                    entry.setdefault(bucket, [])
                    values = payload.get(bucket, [])
                    if isinstance(values, list) and isinstance(entry[bucket], list):
                        for value in values:
                            if value not in cast(list[Any], entry[bucket]):
                                cast(list[Any], entry[bucket]).append(value)
        orders: list[OrderRequest] = []
        estimated_total_fees = 0.0
        estimated_total_notional = 0.0

        sell_candidates: list[tuple[float, str]] = []
        for instrument_id, quantity in current_quantity_by_instrument.items():
            if quantity <= 0 or instrument_id not in latest.index:
                continue
            target_weight = target_weights.get(instrument_id, 0.0)
            current_weight = current_weights.get(instrument_id, 0.0)
            deviation = current_weight - target_weight
            if deviation > self.plan.rebalance_band_pct:
                sell_candidates.append((deviation, instrument_id))
            else:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="within_rebalance_band",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=str(latest.loc[instrument_id].get("sector_name", "UNKNOWN")),
                )

        for _, instrument_id in sorted(sell_candidates, reverse=True):
            row = latest.loc[instrument_id]
            price = float(row["close"])
            lot_size = max(1, int(row.get("lot_size", 1)))
            sector_name = str(row.get("sector_name", "UNKNOWN"))
            current_quantity = current_quantity_by_instrument.get(instrument_id, 0)
            current_notional = current_notional_by_instrument.get(instrument_id, 0.0)
            current_weight = current_notional / investable_base
            target_notional = investable_base * target_weights.get(instrument_id, 0.0)
            desired_notional = max(0.0, current_notional - target_notional)
            max_liquidity_quantity = max(
                lot_size,
                math.floor(float(row.get("volume", 0.0)) * self.settings.risk_liquidity_fraction / lot_size) * lot_size,
            )
            quantity = min(current_quantity, math.floor(desired_notional / price / lot_size) * lot_size, max_liquidity_quantity)
            notional = quantity * price
            if quantity < lot_size:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_small_order",
                    target_weight=target_weights.get(instrument_id, 0.0),
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if notional < self.plan.min_order_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_min_order_notional",
                    target_weight=target_weights.get(instrument_id, 0.0),
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if notional > turnover_budget:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_turnover_limit",
                    target_weight=target_weights.get(instrument_id, 0.0),
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            commission = notional * commission_rate
            current_quantity_by_instrument[instrument_id] = current_quantity - quantity
            current_notional_by_instrument[instrument_id] = current_notional - notional
            current_sector_notional[sector_name] = max(0.0, current_sector_notional[sector_name] - notional)
            deployable_cash += max(0.0, notional - commission)
            turnover_budget -= notional
            estimated_total_fees += commission
            estimated_total_notional += notional
            orders.append(
                OrderRequest(
                    instrument_id=instrument_id,
                    side="sell",
                    quantity=quantity,
                    order_type="market",
                    price=price,
                    metadata={
                        "reason": f"Текущий вес {current_weight:.1%} выше целевого {target_weights.get(instrument_id, 0.0):.1%}.",
                        "target_weight": target_weights.get(instrument_id, 0.0),
                        "current_weight": current_weight,
                        "deviation_from_target": current_weight - target_weights.get(instrument_id, 0.0),
                        "sector_name": sector_name,
                        "estimated_fee": commission,
                        "estimated_notional": notional,
                    },
                )
            )

        buy_candidates: list[tuple[float, float, str]] = []
        for instrument_id, target_weight in target_weights.items():
            if instrument_id in precomputed_skips:
                continue
            if instrument_id not in latest.index:
                proposal_flags.append(f"missing_market_data:{instrument_id}")
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="missing_market_data",
                    target_weight=target_weight,
                    current_weight=0.0,
                    sector_name="UNKNOWN",
                )
                continue
            current_weight = current_notional_by_instrument.get(instrument_id, 0.0) / investable_base
            underweight = target_weight - current_weight
            if underweight > self.plan.rebalance_band_pct:
                candidate_meta = candidate_lookup.get(instrument_id, {})
                action_gate = str(candidate_meta.get("action_gate", "none") or "none").strip().lower()
                if action_gate in {"block_buy", "abstain"}:
                    self._upsert_skip(
                        skipped_instruments,
                        instrument_id,
                        reason=("model_blocked_buy" if action_gate == "block_buy" else "model_abstain"),
                        target_weight=target_weight,
                        current_weight=current_weight,
                        sector_name=str(latest.loc[instrument_id].get("sector_name", "UNKNOWN")),
                    )
                    continue
                decision_score = float(candidate_lookup.get(instrument_id, {}).get("decision_score", candidate_lookup.get(instrument_id, {}).get("candidate_score", 0.0)))
                buy_candidates.append((underweight, decision_score, instrument_id))
            else:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="within_rebalance_band",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=str(latest.loc[instrument_id].get("sector_name", "UNKNOWN")),
                )

        for _, _, instrument_id in sorted(buy_candidates, reverse=True):
            row = latest.loc[instrument_id]
            price = float(row["close"])
            sector_name = str(row.get("sector_name", "UNKNOWN"))
            lot_size = max(1, int(row.get("lot_size", 1)))
            current_notional = current_notional_by_instrument.get(instrument_id, 0.0)
            current_weight = current_notional / investable_base
            target_weight = target_weights[instrument_id]
            desired_notional = max(0.0, investable_base * target_weight - current_notional)
            max_position_headroom = max(0.0, investable_base * self.plan.max_position_pct - current_notional)
            max_sector_headroom = max(0.0, investable_base * self.plan.max_sector_pct - current_sector_notional[sector_name])
            max_liquidity_notional = max(0.0, float(row.get("volume", 0.0)) * self.settings.risk_liquidity_fraction * price)
            minimum_actionable_notional = max(self.plan.min_order_notional, price * lot_size)
            if desired_notional < self.plan.min_order_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_small_order",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if deployable_cash < minimum_actionable_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_fees",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if max_position_headroom < minimum_actionable_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_max_position",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if max_sector_headroom < minimum_actionable_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_sector_limit",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if max_liquidity_notional < minimum_actionable_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_liquidity",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            budget_cap = min(
                desired_notional,
                deployable_cash,
                turnover_budget,
                max_position_headroom,
                max_sector_headroom,
                max_liquidity_notional,
            )
            if budget_cap < self.plan.min_order_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_small_order",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            quantity = math.floor(budget_cap / (price * (1 + commission_rate)) / lot_size) * lot_size
            if quantity < lot_size:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_fees",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            notional = quantity * price
            commission = notional * commission_rate
            total_cost = notional + commission
            if total_cost < self.plan.min_order_notional:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_min_order_notional",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            if total_cost > deployable_cash:
                self._upsert_skip(
                    skipped_instruments,
                    instrument_id,
                    reason="skipped_due_to_fees",
                    target_weight=target_weight,
                    current_weight=current_weight,
                    sector_name=sector_name,
                )
                continue
            deployable_cash -= total_cost
            turnover_budget -= notional
            current_notional_by_instrument[instrument_id] = current_notional + notional
            current_sector_notional[sector_name] += notional
            estimated_total_fees += commission
            estimated_total_notional += notional
            orders.append(
                OrderRequest(
                    instrument_id=instrument_id,
                    side="buy",
                    quantity=quantity,
                    order_type="market",
                    price=price,
                    metadata={
                        "reason": f"Текущий вес {current_weight:.1%} ниже целевого {target_weight:.1%}.",
                        "target_weight": target_weight,
                        "current_weight": current_weight,
                        "deviation_from_target": current_weight - target_weight,
                        "sector_name": sector_name,
                        "estimated_fee": commission,
                        "estimated_notional": notional,
                    },
                )
            )

        current_weights_summary = {
            instrument_id: {
                "current_weight": current_weights.get(instrument_id, 0.0),
                "target_weight": target_weights.get(instrument_id, 0.0),
                "deviation_from_target": current_weights.get(instrument_id, 0.0) - target_weights.get(instrument_id, 0.0),
            }
            for instrument_id in tracked_instruments
        }
        self.last_summary = {
            "plan": self.plan.as_dict(),
            "current_cash": current_cash,
            "reserve_cash_target": reserve_cash_target,
            "investable_cash": investable_cash_before_orders,
            "remaining_investable_cash": deployable_cash,
            "investable_base": investable_base,
            "estimated_total_fees": estimated_total_fees,
            "estimated_total_notional": estimated_total_notional,
            "proposed_buy_count": sum(1 for order in orders if order.side == "buy"),
            "proposed_sell_count": sum(1 for order in orders if order.side == "sell"),
            "skipped_action_count": len(skipped_instruments),
            "current_weights": current_weights_summary,
            "skipped_instruments": sorted(skipped_instruments.values(), key=lambda item: str(item["instrument_id"])),
        }
        return orders


class EqualWeightAllocator(TargetWeightAllocator):
    pass
