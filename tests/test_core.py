import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402

NOW = datetime(2026, 10, 1, 9, 30, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
TODAY = NOW.date()


def make_store():
    store = core.Store(":memory:")
    store.reset_demo(NOW)
    return store


class RiskRulesTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def level(self, order_id):
        return core.assess(self.store.order(order_id), NOW)

    def test_seed_covers_every_level(self):
        levels = {o["id"]: core.assess(o, NOW)[0] for o in self.store.orders()}
        self.assertEqual(levels["K-1039"], "red")      # просрочен
        self.assertEqual(levels["K-1051"], "red")      # нет ответственного
        self.assertEqual(levels["K-1042"], "yellow")   # блокер
        self.assertEqual(levels["K-1061"], "yellow")   # молчит и отстаёт от графика
        self.assertEqual(levels["K-1048"], "green")
        self.assertEqual(levels["K-1063"], "green")

    def test_every_non_green_level_has_reasons(self):
        for o in self.store.orders():
            level, reasons = core.assess(o, NOW)
            self.assertEqual(level == "green", reasons == [], o["id"])

    def test_stage_lag_is_detected_before_anyone_reports(self):
        level, reasons = self.level("K-1061")
        self.assertTrue(any("ещё на стадии «Дизайн»" in r for r in reasons), reasons)

    def test_forecast_after_plan_is_red_until_decided(self):
        plan = date.fromisoformat(self.store.order("K-1042")["plan_date"])
        needs = core.report_problem(self.store, "K-1042", "ilya", "late", "Поставщик перенёс фасады",
                                    plan + timedelta(days=3), "Позвонить поставщику до 12:00", NOW)
        self.assertTrue(needs)
        level, reasons = self.level("K-1042")
        self.assertEqual(level, "red")
        self.assertIn("ждёт решения руководителя", reasons[0])

    def test_status_never_moves_plan_date_by_itself(self):
        before = self.store.order("K-1042")["plan_date"]
        core.report_problem(self.store, "K-1042", "ilya", "late", "Поставщик перенёс фасады",
                            date.fromisoformat(before) + timedelta(days=5), "Позвонить поставщику до 12:00", NOW)
        self.assertEqual(self.store.order("K-1042")["plan_date"], before)

    def test_manager_decision_moves_plan(self):
        plan = date.fromisoformat(self.store.order("K-1042")["plan_date"])
        core.report_problem(self.store, "K-1042", "ilya", "late", "Поставщик перенёс фасады",
                            plan + timedelta(days=3), "Позвонить поставщику до 12:00", NOW)
        core.decide(self.store, "K-1042", "olga", True, NOW)
        o = self.store.order("K-1042")
        self.assertEqual(o["plan_date"], (plan + timedelta(days=3)).isoformat())
        self.assertIsNone(o["forecast_date"])
        with self.assertRaises(core.ValidationError):
            core.decide(self.store, "K-1042", "olga", True, NOW)

    def test_ok_clears_blocker(self):
        core.report_ok(self.store, "K-1042", "ilya", NOW)
        self.assertEqual(self.level("K-1042"), ("green", []))


class ValidationTest(unittest.TestCase):
    plan = date(2026, 10, 8)

    def test_vague_answers_rejected(self):
        for vague in ("нет", "-", "ок", "работаем"):
            self.assertIsNotNone(core.validate_blocker(vague), vague)
            self.assertIsNotNone(core.validate_next_action(vague), vague)

    def test_concrete_answers_accepted(self):
        self.assertIsNone(core.validate_blocker("Поставщик перенёс фасады"))
        self.assertIsNone(core.validate_next_action("Позвонить поставщику до 12:00"))

    def test_late_requires_date_after_plan(self):
        self.assertIsNotNone(core.validate_forecast("late", self.plan, self.plan, TODAY))
        self.assertIsNone(core.validate_forecast("late", self.plan + timedelta(days=1), self.plan, TODAY))
        self.assertIsNone(core.validate_forecast("risk", self.plan, self.plan, TODAY))

    def test_forecast_in_past_rejected(self):
        self.assertIsNotNone(core.validate_forecast("risk", TODAY - timedelta(days=1), self.plan, TODAY))

    def test_parse_date(self):
        self.assertEqual(core.parse_date("15.10", TODAY), date(2026, 10, 15))
        self.assertEqual(core.parse_date("5/1", TODAY), date(2027, 1, 5))
        self.assertEqual(core.parse_date("15.10.26", TODAY), date(2026, 10, 15))
        self.assertEqual(core.parse_date("2026-10-15", TODAY), date(2026, 10, 15))
        self.assertIsNone(core.parse_date("31.02", TODAY))
        self.assertIsNone(core.parse_date("завтра", TODAY))

    def test_find_order_id(self):
        self.assertEqual(core.find_order_id("что по к-1042?"), "K-1042")
        self.assertEqual(core.find_order_id("K1051"), "K-1051")
        self.assertIsNone(core.find_order_id("заказ 1042"))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_incomplete_checklist_blocks_handoff(self):
        with self.assertRaises(core.ValidationError) as ctx:
            core.handoff(self.store, "K-1048", "anna", [0, 1], "ilya", NOW)
        self.assertIn("Предоплата получена", ctx.exception.errors[0])
        self.assertEqual(self.store.order("K-1048")["stage"], "Дизайн")

    def test_wrong_role_rejected(self):
        with self.assertRaises(core.ValidationError):
            core.handoff(self.store, "K-1048", "anna", [0, 1, 2, 3], "roman", NOW)

    def test_full_checklist_moves_order_and_logs(self):
        core.handoff(self.store, "K-1048", "anna", [0, 1, 2, 3], "ilya", NOW)
        o = self.store.order("K-1048")
        self.assertEqual((o["stage"], o["owner_id"]), ("Производство", "ilya"))
        self.assertEqual(self.store.history("K-1048")[-1]["kind"], "handoff")

    def test_last_stage_closes_order(self):
        core.handoff(self.store, "K-1054", "roman", [0, 1, 2, 3], None, NOW)
        self.assertEqual(self.store.order("K-1054")["stage"], "Сдан")
        self.assertNotIn("K-1054", [o["id"] for o in self.store.orders()])

    def test_assign_requires_matching_role(self):
        with self.assertRaises(core.ValidationError):
            core.assign(self.store, "K-1051", "anna", "olga", NOW)
        core.assign(self.store, "K-1051", "pavel", "olga", NOW)
        self.assertEqual(self.store.order("K-1051")["owner_id"], "pavel")


if __name__ == "__main__":
    unittest.main()
