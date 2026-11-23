from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from jarvis.finance.actions import approve_action, execute_local_action
from jarvis.finance.db import FinanceDB
from jarvis.finance.goals import (
    build_goal_contribution_plan,
    goal_progress,
    plan_goal_contributions,
)
from jarvis.finance.rules import DEFAULT_RULES
from jarvis.finance.savings import plan_month_end_reserve_sweep


class GoalsSavingsTests(unittest.TestCase):
    def make_db(self) -> tuple[tempfile.TemporaryDirectory[str], FinanceDB]:
        tmp = tempfile.TemporaryDirectory()
        db = FinanceDB(Path(tmp.name) / "finance.db")
        db.init()
        db.set_setting("rules", DEFAULT_RULES)
        return tmp, db

    def test_goals_allocate_by_priority_and_update_local_progress(self) -> None:
        tmp, db = self.make_db()
        self.addCleanup(tmp.cleanup)
        pc = db.add_or_update_savings_goal("Новый ПК", Decimal(100000), Decimal(10000), priority=1)
        vacation = db.add_or_update_savings_goal("Отпуск", Decimal(30000), Decimal(0), priority=3)

        plan = build_goal_contribution_plan(db, Decimal(12000))
        self.assertEqual(plan.allocated_amount, Decimal("12000.00"))
        self.assertEqual(plan.allocations[0].goal_id, pc)
        self.assertEqual(plan.allocations[0].amount, Decimal("12000.00"))

        action_id = plan_goal_contributions(db, Decimal(12000))
        ok, _ = approve_action(db, action_id)
        self.assertTrue(ok)
        ok, msg = execute_local_action(db, action_id)
        self.assertTrue(ok, msg)
        updated = db.get_savings_goal(pc)
        assert updated is not None
        self.assertEqual(Decimal(updated["current_amount"]), Decimal("22000.00"))
        untouched = db.get_savings_goal(vacation)
        assert untouched is not None
        self.assertEqual(Decimal(untouched["current_amount"]), Decimal(0))
        self.assertEqual(goal_progress(updated), Decimal("22.00"))

    def test_reserve_sweep(self) -> None:
        tmp, db = self.make_db()
        self.addCleanup(tmp.cleanup)
        db.add_budget_snapshot(date(2026, 6, 30), Decimal(10000), Decimal(70000), Decimal(5000))
        action_id, msg = plan_month_end_reserve_sweep(db, DEFAULT_RULES)
        self.assertIsNotNone(action_id)
        self.assertIn("20000", msg)


if __name__ == "__main__":
    unittest.main()
