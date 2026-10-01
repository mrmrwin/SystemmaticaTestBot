"""Сквозной сценарий через поддельный Telegram API: бот получает те же update-объекты,
что и от Telegram, а мы проверяем, какие сообщения и кнопки он отправил."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from bot import Bot  # noqa: E402

TZ = ZoneInfo("Asia/Yekaterinburg")
CHAT = 1001


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.next_id = 500

    def call(self, method, params=None, files=None, timeout=None):
        self.calls.append((method, params or {}, files))
        if method in ("sendMessage", "sendDocument"):
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": (params or {}).get("chat_id")}}
        return True

    def messages(self):
        return [p for m, p, _ in self.calls if m in ("sendMessage", "editMessageText")]

    def last_text(self):
        return self.messages()[-1]["text"]

    def alerts(self):
        return [p.get("text") for m, p, _ in self.calls if m == "answerCallbackQuery" and p.get("show_alert")]

    def buttons(self):
        """Все callback_data из последнего сообщения с инлайн-клавиатурой."""
        for p in reversed(self.messages()):
            kb = (p.get("reply_markup") or {}).get("inline_keyboard")
            if kb:
                return {b["text"]: b["callback_data"] for row in kb for b in row}
        return {}


class Scenario(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 9, 5, tzinfo=TZ)
        self.api = FakeAPI()
        self.store = core.Store(":memory:")
        self.bot = Bot(self.api, self.store, clock=lambda: self.now, demo=True, escalate_minutes=120)
        self.update_id = 0

    def text(self, text, chat=CHAT):
        self.update_id += 1
        self.bot.handle_update({"update_id": self.update_id, "message": {
            "message_id": self.update_id, "chat": {"id": chat}, "text": text}})

    def press(self, data, chat=CHAT, message_id=1):
        self.update_id += 1
        self.bot.handle_update({"update_id": self.update_id, "callback_query": {
            "id": str(self.update_id), "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}}}})

    def login(self, emp_id):
        self.text("/start")
        self.press(f"rl:{emp_id}")

    def test_late_status_goes_to_manager_and_plan_waits_for_decision(self):
        self.login("olga")
        self.text("⏰ Утренний опрос")
        self.assertTrue(any("Демо: так это увидит Илья" in m["text"] for m in self.api.messages()))

        plan_before = self.store.order("K-1042")["plan_date"]
        self.press("lt:K-1042")
        self.assertIn("Демо: отвечаете за Илья", self.api.last_text())
        self.text("нет")                                   # пустая отговорка не проходит
        self.assertIn("Опишите, что именно мешает", self.api.last_text())
        self.text("Поставщик перенёс фасады ещё на 3 дня")
        self.assertNotIn("fc:0", self.api.buttons().values())  # при срыве нельзя выбрать «в план»
        self.press("fc:5")
        self.text("ок")
        self.assertIn("Следующий шаг должен быть конкретным", self.api.last_text())
        self.text("Позвонить поставщику до 12:00 и договориться о частичной отгрузке")
        self.assertIn("бот не меняет", self.api.last_text())
        self.press("cf:yes")

        self.assertEqual(self.store.order("K-1042")["plan_date"], plan_before)
        decision = next(m for m in self.api.messages() if "Нужно ваше решение" in m["text"])
        data = {b["callback_data"] for row in decision["reply_markup"]["inline_keyboard"] for b in row}
        self.assertIn("dc:a:K-1042", data)

        self.text("📊 Сводка")
        digest = self.api.last_text()
        self.assertIn("K-1042", digest)
        self.assertIn("ждёт решения руководителя", digest)

        self.press("dc:a:K-1042")
        self.assertNotEqual(self.store.order("K-1042")["plan_date"], plan_before)
        self.assertFalse(self.store.order("K-1042")["decision_pending"])

    def test_handoff_blocked_until_checklist_complete(self):
        self.login("anna")
        self.press("ho:K-1048")
        self.press("hc:0")
        self.press("hc:1")
        self.press("hd")
        self.assertTrue(self.api.alerts()[-1].startswith("Не отмечено 2 из 4"))
        self.assertEqual(self.store.order("K-1048")["stage"], "Дизайн")
        self.press("hc:2")
        self.press("hc:3")
        self.press("hd")
        self.assertIn("hn:ilya", self.api.buttons().values())
        self.press("hn:ilya")
        o = self.store.order("K-1048")
        self.assertEqual((o["stage"], o["owner_id"]), ("Производство", "ilya"))
        self.assertTrue(any("Вам передан заказ" in m["text"] and "Предоплата получена" in m["text"]
                            for m in self.api.messages()))

    def test_dealer_question_escalates_after_two_hours(self):
        self.login("dlr_kir")
        self.text("📋 Мои заказы")
        self.assertIn("K-1057", self.api.last_text())
        self.assertNotIn("Блокер", self.api.last_text())
        self.text("K-1042")
        self.assertIn("Это не ваш заказ", self.api.last_text())

        self.press("dq:K-1057")
        self.text("Клиент спрашивает, можно ли сдвинуть монтаж на выходные?")
        self.assertIn("Вопрос передан: Максим", self.api.last_text())

        self.now += timedelta(minutes=90)
        self.bot.tick(self.now)
        self.assertFalse(any("ждёт ответа" in m["text"] for m in self.api.messages()))
        self.now += timedelta(minutes=40)
        self.bot.tick(self.now)
        self.assertTrue(any("ждёт ответа по K-1057" in m["text"] for m in self.api.messages()))
        self.bot.tick(self.now)
        self.assertEqual(sum("ждёт ответа" in m["text"] for m in self.api.messages()), 1)

        event_id = self.store.events("dealer_q")[0]["id"]
        self.press(f"dr:{event_id}")
        self.text("Да, можем в субботу с 10:00")
        self.assertTrue(any("Ответ по K-1057" in m["text"] for m in self.api.messages()))
        self.assertEqual(self.bot.open_dealer_questions(), [])

    def test_unassigned_order_assigned_from_digest(self):
        self.login("olga")
        self.text("/digest")
        self.assertIn("as:K-1051", self.api.buttons().values())
        self.press("as:K-1051")
        self.press("an:K-1051:pavel")
        self.assertEqual(self.store.order("K-1051")["owner_id"], "pavel")

    def test_export_sends_two_csv_files(self):
        self.login("olga")
        self.text("📤 Выгрузка")
        docs = [f["document"] for m, _, f in self.api.calls if m == "sendDocument"]
        self.assertEqual(len(docs), 2)
        name, content, _ = docs[0]
        self.assertTrue(name.startswith("orders_"))
        self.assertTrue(content.startswith("﻿".encode()))
        self.assertIn("K-1042", content.decode("utf-8-sig"))

    def test_stale_buttons_do_not_break_anything(self):
        self.login("olga")
        self.press("fc:2")
        self.press("cf:yes")
        self.press("hc:1")
        self.assertEqual(len(self.api.alerts()), 3)

    def test_schedule_does_not_fire_missed_jobs_on_start(self):
        self.bot.schedule = True
        self.login("olga")
        self.now = self.now.replace(hour=12)
        self.bot.skip_missed_jobs(self.now)
        sent = len(self.api.messages())
        self.bot.tick(self.now)
        self.assertEqual(len(self.api.messages()), sent)
        self.now += timedelta(days=1)
        self.now = self.now.replace(hour=9, minute=1)
        self.bot.tick(self.now)
        self.assertTrue(any("Доброе утро" in m["text"] for m in self.api.messages()[sent:]))


if __name__ == "__main__":
    unittest.main()
