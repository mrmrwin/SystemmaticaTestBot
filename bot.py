"""Telegram-бот «Статусы заказов» для производителя кухонь.

Запуск: python3 bot.py  (токен — в переменной TELEGRAM_BOT_TOKEN или в файле .env рядом).
Только стандартная библиотека Python 3.9+, без внешних зависимостей.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import core
from core import ValidationError, fmt_date, fmt_money

HERE = Path(__file__).resolve().parent
log = logging.getLogger("status-bot")
h = html.escape

LEVEL_ICON = {"red": "🔴", "yellow": "🟡", "green": "🟢", "done": "🏁"}
LEVEL_NAME = {"red": "критично", "yellow": "внимание", "green": "по плану", "done": "сдан"}
LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2, "done": 3}

BTN_DIGEST = "📊 Сводка"
BTN_BOARD = "📋 Все заказы"
BTN_PING = "⏰ Утренний опрос"
BTN_EXPORT = "📤 Выгрузка"
BTN_MY = "📋 Мои заказы"
BTN_ROLE = "👥 Сменить роль"
MENUS = {
    "manager": [[BTN_DIGEST, BTN_BOARD], [BTN_PING, BTN_EXPORT], [BTN_ROLE]],
    "staff": [[BTN_MY], [BTN_ROLE]],
    "dealer": [[BTN_MY], [BTN_ROLE]],
}
MENU_TEXTS = {BTN_DIGEST, BTN_BOARD, BTN_PING, BTN_EXPORT, BTN_MY, BTN_ROLE}

HELP = (
    "<b>Бот статусов заказов</b> — один канал, через который замерщики, дизайнеры, производство "
    "и монтажники сообщают статус, а руководитель и дилеры видят риски без звонков собственнику.\n\n"
    "• Сотрудник: каждое утро получает свои заказы. «✅ По плану» — одна кнопка; "
    "«⚠️ Есть риск» или «⛔ Срыв срока» — три коротких вопроса: что мешает, прогноз, следующий шаг.\n"
    "• Плановую дату бот сам не меняет: если прогноз позже плана, решение принимает руководитель.\n"
    "• Заказ переходит на следующую стадию только по чек-листу передачи.\n"
    "• Дилер видит статус своих заказов и задаёт вопрос ответственному. Нет ответа за 2 часа — вопрос уходит руководителю.\n\n"
    "Команды: /start — выбрать роль, /orders — мои заказы, /digest — сводка, "
    "/cancel — прервать ввод, /reset — сбросить демо-данные. Номер заказа (например K-1042) — открыть карточку."
)


def btn(text, data):
    assert len(data.encode()) <= 64, data
    return {"text": text, "callback_data": data}


def menu_kind(emp):
    if emp["role"] == "manager":
        return "manager"
    if emp["role"] == "dealer":
        return "dealer"
    return "staff"


def who(emp):
    return f"{emp['name']} ({core.ROLE_TITLES[emp['role']]})"


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, params=None, files=None, timeout=15):
        for attempt in range(3):
            if files:
                body, ctype = _multipart(params or {}, files)
            else:
                body, ctype = json.dumps(params or {}).encode(), "application/json"
            req = urllib.request.Request(self.base + method, data=body, headers={"Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read())
                except ValueError:
                    raise TelegramError(f"{method}: HTTP {e.code}") from None
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry and attempt < 2:
                time.sleep(retry)
                continue
            raise TelegramError(f"{method}: {data.get('description')}")


def _multipart(params, files):
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    for key, (filename, content, mime) in files.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {mime}\r\n\r\n".encode())
        out.write(content)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class Bot:
    def __init__(self, api, store: core.Store, clock, demo=True, schedule=False,
                 ping_at="09:00", digest_at="10:00", remind_at="14:00", escalate_minutes=120):
        self.api = api
        self.store = store
        self.clock = clock
        self.demo = demo
        self.schedule = schedule
        self.times = {
            "ping": dtime.fromisoformat(ping_at),
            "digest": dtime.fromisoformat(digest_at),
            "remind": dtime.fromisoformat(remind_at),
        }
        self.escalate_after = timedelta(minutes=escalate_minutes)
        if store.is_empty():
            store.reset_demo(clock())

    # --- отправка --------------------------------------------------------------------
    def send(self, chat_id, text, kb=None, menu=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if kb is not None:
            params["reply_markup"] = {"inline_keyboard": kb}
        elif menu is not None:
            params["reply_markup"] = {"keyboard": menu, "resize_keyboard": True}
        return self.api.call("sendMessage", params)

    def edit(self, chat_id, message_id, text, kb=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": kb or []}}
        try:
            self.api.call("editMessageText", params)
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def edit_kb(self, chat_id, message_id, kb):
        try:
            self.api.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                     "reply_markup": {"inline_keyboard": kb}})
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def notify(self, emp_id, text, kb=None, demo_header=True):
        """Сообщение сотруднику. В демо, если сотрудник не подключён к боту,
        сообщение приходит во все демо-чаты с пометкой, кому оно на самом деле."""
        chats = self.store.chats_for(emp_id)
        if not chats and self.demo:
            chats = self.store.all_chat_ids()
            if demo_header:
                text = f"📨 <i>Демо: так это увидит {h(who(self.store.employee(emp_id)))}</i>\n\n{text}"
        for chat_id in chats:
            self.send(chat_id, text, kb)

    def notify_managers(self, text, kb=None):
        for m in self.store.employees("manager"):
            self.notify(m["id"], text, kb)

    # --- кто действует -----------------------------------------------------------------
    def order_or_fail(self, order_id):
        order = self.store.order(order_id)
        if not order:
            raise ValidationError([f"Заказ {order_id} не найден."])
        return order

    def actor_for_order(self, emp, order):
        """Статус по заказу даёт его ответственный. В демо можно ответить за него."""
        if emp and emp["id"] == order["owner_id"]:
            return emp, False
        if not order["owner_id"]:
            raise ValidationError(["У заказа нет ответственного — сначала его назначает руководитель."])
        if self.demo:
            return self.store.employee(order["owner_id"]), True
        raise ValidationError(["Статус по заказу даёт только его ответственный."])

    def manager_actor(self, emp):
        if emp and emp["role"] == "manager":
            return emp
        if self.demo:
            return self.store.employees("manager")[0]
        raise ValidationError(["Это решение принимает руководитель."])

    def dealer_actor(self, emp, order):
        if emp and emp["id"] == order["dealer_id"]:
            return emp
        if self.demo and order["dealer_id"]:
            return self.store.employee(order["dealer_id"])
        raise ValidationError(["Это не ваш заказ."])

    def now(self):
        return self.clock()

    # --- карточки ----------------------------------------------------------------------
    def order_card(self, o, view="staff"):
        now = self.now()
        level, reasons = core.assess(o, now)
        owner = self.store.employee(o["owner_id"])
        plan = date.fromisoformat(o["plan_date"])
        days = (plan - now.date()).days
        when = f"через {days} дн." if days > 0 else ("сегодня" if days == 0 else f"просрочен на {-days} дн.")
        lines = [
            f"{LEVEL_ICON[level]} <b>{o['id']}</b> · {h(o['client'])}, {h(o['city'])}",
            f"Стадия: <b>{o['stage']}</b> · ответственный: " + (h(who(owner)) if owner else "<b>не назначен</b>"),
            f"Монтаж по плану: {fmt_date(plan)}" + ("" if level == "done" else f" ({when})"),
        ]
        if o["forecast_date"]:
            lines.append(f"Прогноз: <b>{fmt_date(o['forecast_date'])}</b>")
        if view != "dealer" and o["blocker"]:
            lines.append(f"Блокер: {h(o['blocker'])}")
        lines.append(f"Следующий шаг: {h(o['next_action'] or '—')}")
        if view != "dealer" and reasons:
            lines.append(f"<i>Почему «{LEVEL_NAME[level]}»: {h('; '.join(reasons))}</i>")
        if view == "manager":
            margin = o["revenue"] - o["cost"]
            lines.append(f"Плановая маржа: {fmt_money(margin)} ({round(margin * 100 / o['revenue'])}%)")
        return "\n".join(lines)

    def status_kb(self, o):
        oid = o["id"]
        rows = [[btn("✅ По плану", f"ok:{oid}"), btn("⚠️ Есть риск", f"rk:{oid}")], [btn("⛔ Срыв срока", f"lt:{oid}")]]
        nxt = core.next_stage(o["stage"])
        if nxt:
            rows.append([btn("🏁 Сдать клиенту" if nxt == "Сдан" else f"➡️ Передать: {nxt}", f"ho:{oid}")])
        return rows

    def manager_kb(self, o):
        oid = o["id"]
        rows = []
        if o["decision_pending"]:
            rows.append([btn(f"📅 Перенести план на {fmt_date(o['forecast_date'])}", f"dc:a:{oid}")])
            rows.append([btn(f"🚑 Держать план {fmt_date(o['plan_date'])}", f"dc:k:{oid}")])
        if not o["owner_id"] and o["stage"] in core.STAGE_ROLE:
            rows.append([btn("👤 Назначить ответственного", f"as:{oid}")])
        return rows

    def history_text(self, order_id):
        lines = []
        for e in self.store.history(order_id):
            ts = datetime.fromisoformat(e["ts"])
            actor = self.store.employee(e["actor_id"])
            d = e["details"]
            kind = e["kind"]
            if kind == "ok":
                what = "✅ по плану"
            elif kind in ("risk", "late"):
                what = ("⚠️ риск" if kind == "risk" else "⛔ срыв") + f": {d['blocker']}; прогноз {fmt_date(d['forecast'])}"
            elif kind == "plan_moved":
                what = f"📅 план перенесён {fmt_date(d['old'])} → {fmt_date(d['new'])}"
            elif kind == "plan_kept":
                what = f"🚑 план {fmt_date(d['plan'])} сохранён"
            elif kind == "handoff":
                what = f"➡️ {d['from_stage']} → {d['to_stage']} по чек-листу"
            elif kind == "assign":
                what = f"👤 назначен {self.store.employee(d['owner'])['name']}"
            elif kind == "dealer_q":
                what = f"❓ вопрос дилера: {d['text']}"
            elif kind == "dealer_a":
                what = f"💬 ответ дилеру: {d['text']}"
            else:
                what = kind
            lines.append(f"{fmt_date(ts.date())} {ts:%H:%M} · {h(actor['name'] if actor else '—')}: {h(what)}")
        return "\n".join(lines) or "событий пока нет"

    # --- входящие ------------------------------------------------------------------------
    def handle_update(self, upd):
        if "callback_query" in upd:
            self.on_callback(upd["callback_query"])
        elif "message" in upd and "text" in upd["message"]:
            self.on_message(upd["message"])

    def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        chat = self.store.chat(chat_id)
        emp = self.store.employee(chat["employee_id"]) if chat else None

        if text.startswith("/start") or text.startswith("/role") or text == BTN_ROLE:
            return self.ask_role(chat_id)
        if text.startswith("/help"):
            return self.send(chat_id, HELP)
        if not emp:
            return self.ask_role(chat_id)
        if text.startswith("/cancel"):
            self.store.set_state(chat_id, {})
            return self.send(chat_id, "Ввод отменён.", menu=MENUS[menu_kind(emp)])
        if text.startswith("/reset"):
            self.store.reset_demo(self.now())
            return self.send(chat_id, "Демо-данные восстановлены.", menu=MENUS[menu_kind(emp)])

        state = chat["state"]
        if state.get("flow") and not text.startswith("/") and text not in MENU_TEXTS:
            try:
                return self.on_flow_text(chat_id, state, text)
            except ValidationError as e:
                return self.send(chat_id, "❗ " + h("\n".join(e.errors)))
        if state.get("flow"):
            self.store.set_state(chat_id, {})

        if text in (BTN_MY, "/orders"):
            return self.show_my_orders(chat_id, emp)
        if text in (BTN_DIGEST, "/digest"):
            return self.send_digest(chat_id)
        if text in (BTN_BOARD, "/board"):
            return self.show_board(chat_id)
        if text in (BTN_PING, "/ping"):
            count = self.morning_ping(self.now())
            return self.send(chat_id, f"Опрос отправлен: {count}. Ответьте за сотрудников кнопками под карточками, затем откройте «{BTN_DIGEST}».")
        if text in (BTN_EXPORT, "/export"):
            return self.send_export(chat_id)
        order_id = core.find_order_id(text)
        if order_id:
            return self.show_order(chat_id, emp, order_id)
        self.send(chat_id, "Не понял сообщение. Выберите действие в меню или пришлите номер заказа, например K-1042. /help — как работает бот.",
                  menu=MENUS[menu_kind(emp)])

    def on_callback(self, cq):
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq.get("data", "")
        chat = self.store.chat(chat_id)
        emp = self.store.employee(chat["employee_id"]) if chat else None
        alert = None
        try:
            if data.startswith("rl:"):
                self.choose_role(chat_id, data[3:])
            elif not emp:
                self.ask_role(chat_id)
            else:
                alert = self.dispatch(chat_id, message_id, emp, chat["state"], data)
        except ValidationError as e:
            alert = "\n".join(e.errors)
        params = {"callback_query_id": cq["id"]}
        if alert:
            params.update(text=alert[:200], show_alert=True)
        self.api.call("answerCallbackQuery", params)

    def dispatch(self, chat_id, message_id, emp, state, data):
        kind, _, arg = data.partition(":")
        if kind == "ok":
            return self.status_ok(chat_id, message_id, emp, arg)
        if kind in ("rk", "lt"):
            return self.start_status(chat_id, emp, arg, "risk" if kind == "rk" else "late")
        if kind == "fc":
            return self.choose_forecast(chat_id, state, arg)
        if kind == "cf":
            return self.confirm_status(chat_id, message_id, state, arg)
        if kind == "ho":
            return self.start_handoff(chat_id, emp, arg)
        if kind == "hc":
            return self.toggle_check(chat_id, message_id, state, int(arg))
        if kind == "hd":
            return self.finish_checklist(chat_id, message_id, state)
        if kind == "hn":
            return self.complete_handoff(chat_id, message_id, state, arg)
        if kind == "hx":
            self.store.set_state(chat_id, {})
            self.edit(chat_id, message_id, "Передача отменена.")
            return None
        if kind == "dc":
            choice, _, order_id = arg.partition(":")
            return self.decide(chat_id, message_id, emp, order_id, choice == "a")
        if kind == "as":
            return self.ask_assignee(chat_id, message_id, emp, arg)
        if kind == "an":
            order_id, _, owner_id = arg.partition(":")
            return self.assign(chat_id, message_id, emp, order_id, owner_id)
        if kind == "dq":
            return self.start_dealer_question(chat_id, emp, arg)
        if kind == "dr":
            return self.start_dealer_reply(chat_id, emp, int(arg))
        if kind == "or":
            return self.show_order(chat_id, emp, arg)
        return "Кнопка устарела."

    # --- роли ------------------------------------------------------------------------------
    def ask_role(self, chat_id):
        rows = [[btn(f"{e['name']} — {core.ROLE_TITLES[e['role']]}", f"rl:{e['id']}")] for e in self.store.employees()]
        self.send(chat_id,
                  "👋 Демо-бот статусов заказов кухонной фабрики.\n\n"
                  "Выберите, за кого вы сейчас. В реальной работе роль привязывается к сотруднику один раз; "
                  "здесь её можно менять, чтобы пройти весь путь заказа.", rows)

    def choose_role(self, chat_id, emp_id):
        emp = self.store.employee(emp_id)
        if not emp:
            raise ValidationError(["Сотрудник не найден, нажмите /start."])
        self.store.bind_chat(chat_id, emp_id)
        kind = menu_kind(emp)
        if kind == "manager":
            text = (f"Вы — <b>{h(emp['name'])}</b>, руководитель.\n\n"
                    f"1. «{BTN_PING}» — бот разошлёт сотрудникам их заказы. В демо всё придёт сюда, и вы ответите за каждого.\n"
                    f"2. Отметьте риск или срыв по паре заказов, передайте заказ дальше по чек-листу.\n"
                    f"3. «{BTN_DIGEST}» — что горит, кто молчит, сколько маржи под риском и какие решения ждут вас.\n"
                    f"4. «{BTN_EXPORT}» — реестр и журнал в CSV для Excel.")
        elif kind == "dealer":
            text = (f"Вы — дилер <b>{h(emp['name'])}</b>.\n\n"
                    "Здесь статус ваших заказов и ответственный по каждому. Вопрос по заказу уходит ответственному; "
                    "если ответа нет 2 часа — руководителю. Звонить собственнику больше не нужно.")
        else:
            text = (f"Вы — <b>{h(emp['name'])}</b>, {core.ROLE_TITLES[emp['role']]}.\n\n"
                    "Каждое утро бот присылает ваши заказы. Всё по плану — одна кнопка. Есть риск — три коротких вопроса. "
                    "Передать заказ на следующую стадию можно только по чек-листу.")
        self.send(chat_id, text, menu=MENUS[kind])

    # --- просмотр --------------------------------------------------------------------------
    def show_my_orders(self, chat_id, emp):
        if emp["role"] == "manager":
            return self.show_board(chat_id)
        if emp["role"] == "dealer":
            orders = self.store.dealer_orders(emp["id"])
            if not orders:
                return self.send(chat_id, "У вас нет заказов.")
            for o in orders:
                self.send(chat_id, self.order_card(o, "dealer"), [[btn("❓ Вопрос по заказу", f"dq:{o['id']}")]])
            return None
        orders = self.store.orders_of(emp["id"])
        if not orders:
            return self.send(chat_id, "Сейчас за вами нет активных заказов.")
        for o in orders:
            self.send(chat_id, self.order_card(o), self.status_kb(o))

    def show_order(self, chat_id, emp, order_id):
        o = self.store.order(order_id)
        if not o:
            return self.send(chat_id, f"Заказ {order_id} не найден.")
        if emp["role"] == "dealer":
            if o["dealer_id"] != emp["id"]:
                return self.send(chat_id, "Это не ваш заказ — по нему ответит ответственный шоурума.")
            return self.send(chat_id, self.order_card(o, "dealer"), [[btn("❓ Вопрос по заказу", f"dq:{o['id']}")]])
        if emp["role"] == "manager":
            text = self.order_card(o, "manager") + "\n\n<b>История</b>\n" + self.history_text(order_id)
            return self.send(chat_id, text, self.manager_kb(o) or None)
        kb = self.status_kb(o) if o["owner_id"] == emp["id"] and o["stage"] != "Сдан" else None
        self.send(chat_id, self.order_card(o), kb)

    def show_board(self, chat_id):
        now = self.now()
        rows = sorted(((o, *core.assess(o, now)) for o in self.store.orders()), key=lambda r: (LEVEL_ORDER[r[1]], r[0]["plan_date"]))
        lines = ["<b>Все активные заказы</b> (сначала критичные)"]
        kb, line = [], []
        for o, level, _ in rows:
            owner = self.store.employee(o["owner_id"])
            lines.append(f"{LEVEL_ICON[level]} <b>{o['id']}</b> · {o['stage']} · {h(owner['name']) if owner else 'нет ответственного'} · монтаж {fmt_date(o['plan_date'])}")
            line.append(btn(f"{LEVEL_ICON[level]} {o['id']}", f"or:{o['id']}"))
            if len(line) == 3:
                kb.append(line)
                line = []
        if line:
            kb.append(line)
        self.send(chat_id, "\n".join(lines), kb)

    # --- статус «по плану» -----------------------------------------------------------------
    def status_ok(self, chat_id, message_id, emp, order_id):
        o = self.order_or_fail(order_id)
        actor, _ = self.actor_for_order(emp, o)
        had_problem = bool(o["blocker"] or o["forecast_date"])
        core.report_ok(self.store, order_id, actor["id"], self.now())
        o = self.store.order(order_id)
        self.edit(chat_id, message_id, self.order_card(o) + f"\n\n✅ <i>Статус принят в {self.now():%H:%M}</i>", self.status_kb(o))
        if had_problem:
            self.notify_managers(f"✅ {h(who(actor))}: по {order_id} проблема снята, заказ идёт по плану.")
        return "Принято: по плану"

    # --- статус «риск» / «срыв»: три шага и подтверждение -------------------------------------
    def start_status(self, chat_id, emp, order_id, kind):
        o = self.order_or_fail(order_id)
        if o["stage"] == "Сдан":
            raise ValidationError(["Заказ уже сдан."])
        actor, as_demo = self.actor_for_order(emp, o)
        self.store.set_state(chat_id, {"flow": "status", "step": "blocker", "order": order_id, "kind": kind, "actor": actor["id"]})
        title = "⚠️ Риск" if kind == "risk" else "⛔ Срыв срока"
        demo = f"<i>Демо: отвечаете за {h(who(actor))}</i>\n" if as_demo else ""
        self.send(chat_id, f"{demo}{title} по <b>{order_id}</b> · шаг 1 из 3\n\n"
                           "Что мешает? Одним сообщением, например: «Поставщик перенёс фасады на 3 дня».\n/cancel — отменить")

    def on_flow_text(self, chat_id, state, text):
        flow, step = state.get("flow"), state.get("step")
        if flow == "status" and step == "blocker":
            err = core.validate_blocker(text)
            if err:
                raise ValidationError([err])
            state.update(blocker=text.strip(), step="forecast")
            self.store.set_state(chat_id, state)
            return self.ask_forecast(chat_id, state)
        if flow == "status" and step == "forecast_text":
            o = self.store.order(state["order"])
            forecast = core.parse_date(text, self.now().date())
            if not forecast:
                raise ValidationError(["Не понял дату. Напишите в формате ДД.ММ, например 15.10."])
            err = core.validate_forecast(state["kind"], forecast, date.fromisoformat(o["plan_date"]), self.now().date())
            if err:
                raise ValidationError([err])
            state.update(forecast=forecast.isoformat(), step="next")
            self.store.set_state(chat_id, state)
            return self.ask_next(chat_id)
        if flow == "status" and step == "next":
            err = core.validate_next_action(text)
            if err:
                raise ValidationError([err])
            state.update(next_action=text.strip(), step="confirm")
            self.store.set_state(chat_id, state)
            return self.ask_confirm(chat_id, state)
        if flow == "status" and step in ("forecast", "confirm"):
            raise ValidationError(["Выберите вариант кнопкой выше или /cancel."])
        if flow == "dealer_q":
            return self.finish_dealer_question(chat_id, state, text)
        if flow == "dealer_reply":
            return self.finish_dealer_reply(chat_id, state, text)
        self.store.set_state(chat_id, {})
        return self.send(chat_id, "Ввод сброшен, начните заново.")

    def ask_forecast(self, chat_id, state):
        o = self.store.order(state["order"])
        plan = date.fromisoformat(o["plan_date"])
        today = self.now().date()
        rows = []
        if state["kind"] == "risk" and plan >= today:
            rows.append([btn(f"Успеваем к плану ({fmt_date(plan)})", "fc:0")])
        shifts = [n for n in (2, 5, 7) if plan + timedelta(days=n) >= today]
        rows.append([btn(f"+{n} дн. → {fmt_date(plan + timedelta(days=n))}", f"fc:{n}") for n in shifts])
        rows.append([btn("📅 Своя дата", "fc:own")])
        self.send(chat_id, f"Шаг 2 из 3 · когда реально будет монтаж?\nПлан: <b>{fmt_date(plan)}</b>", rows)

    def ask_next(self, chat_id):
        self.send(chat_id, "Шаг 3 из 3 · какой следующий шаг и к какому сроку?\n"
                           "Например: «Позвонить поставщику до 12:00 и договориться о частичной отгрузке».")

    def ask_confirm(self, chat_id, state):
        o = self.store.order(state["order"])
        plan = date.fromisoformat(o["plan_date"])
        forecast = date.fromisoformat(state["forecast"])
        shift = (forecast - plan).days
        lines = [
            f"Проверьте статус по <b>{o['id']}</b>:",
            "Тип: " + ("⚠️ есть риск" if state["kind"] == "risk" else "⛔ срыв срока"),
            f"Что мешает: {h(state['blocker'])}",
            f"Прогноз монтажа: <b>{fmt_date(forecast)}</b> (план {fmt_date(plan)}" + (f", +{shift} дн.)" if shift > 0 else ")"),
            f"Следующий шаг: {h(state['next_action'])}",
        ]
        if shift > 0:
            lines.append("\n❗ Прогноз позже плана. Плановую дату бот не меняет — решение примет руководитель.")
        self.send(chat_id, "\n".join(lines), [[btn("✅ Отправить", "cf:yes")], [btn("✏️ Заново", "cf:redo"), btn("✖️ Отмена", "cf:no")]])

    def choose_forecast(self, chat_id, state, arg):
        if state.get("flow") != "status" or state.get("step") not in ("forecast", "forecast_text"):
            return "Кнопка устарела — начните статус заново."
        if arg == "own":
            state["step"] = "forecast_text"
            self.store.set_state(chat_id, state)
            self.send(chat_id, "Напишите дату в формате ДД.ММ, например 15.10.")
            return None
        o = self.store.order(state["order"])
        forecast = date.fromisoformat(o["plan_date"]) + timedelta(days=int(arg))
        err = core.validate_forecast(state["kind"], forecast, date.fromisoformat(o["plan_date"]), self.now().date())
        if err:
            return err
        state.update(forecast=forecast.isoformat(), step="next")
        self.store.set_state(chat_id, state)
        self.ask_next(chat_id)
        return None

    def confirm_status(self, chat_id, message_id, state, arg):
        if state.get("flow") != "status" or state.get("step") != "confirm":
            return "Кнопка устарела — начните статус заново."
        if arg == "no":
            self.store.set_state(chat_id, {})
            self.edit(chat_id, message_id, "Статус не отправлен.")
            return None
        if arg == "redo":
            state = {k: state[k] for k in ("flow", "order", "kind", "actor")}
            state["step"] = "blocker"
            self.store.set_state(chat_id, state)
            self.edit(chat_id, message_id, "Заполняем заново.")
            self.send(chat_id, "Шаг 1 из 3 · что мешает?")
            return None
        order_id = state["order"]
        actor = self.store.employee(state["actor"])
        needs_decision = core.report_problem(self.store, order_id, actor["id"], state["kind"], state["blocker"],
                                             date.fromisoformat(state["forecast"]), state["next_action"], self.now())
        self.store.set_state(chat_id, {})
        o = self.store.order(order_id)
        self.edit(chat_id, message_id, "📨 Статус отправлен.\n\n" + self.order_card(o), self.status_kb(o))
        title = "⛔ Срыв срока" if state["kind"] == "late" else "⚠️ Риск"
        text = f"{title} по {order_id} — сообщает {h(who(actor))}\n\n{self.order_card(o, 'manager')}"
        if needs_decision:
            text += "\n\n<b>Нужно ваше решение:</b> перенести плановую дату и предупредить клиента — или держать план?"
        self.notify_managers(text, self.manager_kb(o) or None)
        return "Статус отправлен руководителю"

    # --- передача между стадиями по чек-листу -------------------------------------------------
    def start_handoff(self, chat_id, emp, order_id):
        o = self.order_or_fail(order_id)
        actor, _ = self.actor_for_order(emp, o)
        nxt = core.next_stage(o["stage"])
        if not nxt:
            raise ValidationError(["Заказ уже сдан."])
        state = {"flow": "handoff", "order": order_id, "actor": actor["id"], "checked": []}
        self.store.set_state(chat_id, state)
        target = "сдача клиенту" if nxt == "Сдан" else f"{o['stage']} → {nxt}"
        self.send(chat_id, f"➡️ <b>{order_id}</b>: {target}\n\nОтметьте, что передаёте. Пока чек-лист не заполнен, "
                           "заказ не уйдёт дальше: именно на стыках сейчас теряется информация.", self.checklist_kb(o, []))

    def checklist_kb(self, o, checked):
        items = core.HANDOFF_CHECKLISTS[o["stage"]]
        rows = [[btn(("☑️ " if i in checked else "⬜️ ") + item, f"hc:{i}")] for i, item in enumerate(items)]
        rows.append([btn("Передать →", "hd"), btn("✖️ Отмена", "hx")])
        return rows

    def toggle_check(self, chat_id, message_id, state, index):
        if state.get("flow") != "handoff":
            return "Кнопка устарела — начните передачу заново."
        checked = set(state["checked"])
        checked ^= {index}
        state["checked"] = sorted(checked)
        self.store.set_state(chat_id, state)
        self.edit_kb(chat_id, message_id, self.checklist_kb(self.store.order(state["order"]), state["checked"]))
        return None

    def finish_checklist(self, chat_id, message_id, state):
        if state.get("flow") != "handoff":
            return "Кнопка устарела — начните передачу заново."
        o = self.store.order(state["order"])
        items = core.HANDOFF_CHECKLISTS[o["stage"]]
        missing = [item for i, item in enumerate(items) if i not in state["checked"]]
        if missing:
            return f"Не отмечено {len(missing)} из {len(items)}: " + "; ".join(missing)
        nxt = core.next_stage(o["stage"])
        if nxt == "Сдан":
            return self.complete_handoff(chat_id, message_id, state, "")
        candidates = self.store.employees(core.STAGE_ROLE[nxt])
        rows = [[btn(e["name"], f"hn:{e['id']}")] for e in candidates] + [[btn("✖️ Отмена", "hx")]]
        self.edit(chat_id, message_id, f"✅ Чек-лист заполнен. Кому передать <b>{o['id']}</b> на стадию «{nxt}»?", rows)
        return None

    def complete_handoff(self, chat_id, message_id, state, new_owner_id):
        if state.get("flow") != "handoff":
            return "Кнопка устарела — начните передачу заново."
        order_id = state["order"]
        before = self.store.order(order_id)
        actor = self.store.employee(state["actor"])
        nxt = core.handoff(self.store, order_id, actor["id"], state["checked"], new_owner_id or None, self.now())
        self.store.set_state(chat_id, {})
        o = self.store.order(order_id)
        passed = "\n".join(f"✓ {h(item)}" for item in core.HANDOFF_CHECKLISTS[before["stage"]])
        if nxt == "Сдан":
            self.edit(chat_id, message_id, f"🏁 {order_id} сдан клиенту.\n{passed}")
            self.notify_managers(f"🏁 {order_id} сдан клиенту — {h(who(actor))}.\n{passed}")
            if o["dealer_id"]:
                self.notify(o["dealer_id"], f"🏁 Заказ {order_id} смонтирован и сдан клиенту.")
            return "Заказ сдан"
        new_owner = self.store.employee(new_owner_id)
        self.edit(chat_id, message_id, f"✅ {order_id} передан: {before['stage']} → {nxt}, ответственный {h(new_owner['name'])}.")
        self.notify(new_owner_id, f"📥 Вам передан заказ от {h(who(actor))}. Передано по чек-листу:\n{passed}\n\n{self.order_card(o)}",
                    self.status_kb(o))
        self.notify_managers(f"➡️ {order_id}: {before['stage']} → {nxt}. Передал {h(actor['name'])}, принял {h(new_owner['name'])}.")
        if o["dealer_id"]:
            self.notify(o["dealer_id"], f"Заказ {order_id} перешёл на стадию «{nxt}». Ответственный: {h(who(new_owner))}.")
        return "Заказ передан"

    # --- решения руководителя -----------------------------------------------------------------
    def decide(self, chat_id, message_id, emp, order_id, accept):
        actor = self.manager_actor(emp)
        before = self.order_or_fail(order_id)
        core.decide(self.store, order_id, actor["id"], accept, self.now())
        o = self.store.order(order_id)
        if accept:
            result = f"📅 План {order_id} перенесён на {fmt_date(o['plan_date'])}."
            owner_text = f"{result} Предупредите клиента о новой дате."
            if o["dealer_id"]:
                self.notify(o["dealer_id"], f"📅 Дата монтажа по заказу {order_id} перенесена на <b>{fmt_date(o['plan_date'])}</b>. "
                                            f"Причина: {h(before['blocker'])}.")
        else:
            result = f"🚑 По {order_id} держим план {fmt_date(o['plan_date'])}."
            owner_text = f"{result} Нужен план, как успеть: обновите статус, когда будет решение."
        self.edit(chat_id, message_id, f"{self.order_card(o, 'manager')}\n\n<b>Решение:</b> {result}")
        if o["owner_id"]:
            self.notify(o["owner_id"], f"{owner_text}\n\n{self.order_card(o)}", self.status_kb(o))
        return None

    def ask_assignee(self, chat_id, message_id, emp, order_id):
        self.manager_actor(emp)
        o = self.order_or_fail(order_id)
        role = core.STAGE_ROLE[o["stage"]]
        rows = [[btn(e["name"], f"an:{order_id}:{e['id']}")] for e in self.store.employees(role)]
        self.send(chat_id, f"Кто ведёт {order_id} на стадии «{o['stage']}»?", rows)
        return None

    def assign(self, chat_id, message_id, emp, order_id, owner_id):
        actor = self.manager_actor(emp)
        core.assign(self.store, order_id, owner_id, actor["id"], self.now())
        o = self.store.order(order_id)
        owner = self.store.employee(owner_id)
        self.edit(chat_id, message_id, f"👤 {order_id}: ответственный — {h(who(owner))}.")
        self.notify(owner_id, f"📥 Вас назначили ответственным за заказ.\n\n{self.order_card(o)}", self.status_kb(o))
        if o["dealer_id"]:
            self.notify(o["dealer_id"], f"По заказу {order_id} назначен ответственный: {h(who(owner))}.")
        return "Назначено"

    # --- вопросы дилеров ----------------------------------------------------------------------
    def start_dealer_question(self, chat_id, emp, order_id):
        o = self.order_or_fail(order_id)
        dealer = self.dealer_actor(emp, o)
        self.store.set_state(chat_id, {"flow": "dealer_q", "order": order_id, "actor": dealer["id"]})
        self.send(chat_id, f"Напишите вопрос по заказу <b>{order_id}</b> одним сообщением.\n/cancel — отменить")
        return None

    def finish_dealer_question(self, chat_id, state, text):
        o = self.store.order(state["order"])
        dealer = self.store.employee(state["actor"])
        event_id = self.store.log(o["id"], dealer["id"], "dealer_q", self.now(), text=text, answered=False, escalated=False)
        self.store.set_state(chat_id, {})
        target = self.store.employee(o["owner_id"]) or self.store.employees("manager")[0]
        self.notify(target["id"], f"❓ Вопрос дилера {h(dealer['name'])} по {o['id']}:\n«{h(text)}»\n\nОтветить нужно в течение 2 часов.",
                    [[btn("↩️ Ответить дилеру", f"dr:{event_id}")]])
        self.send(chat_id, f"Вопрос передан: {h(who(target))}. Срок ответа — 2 часа; если ответа не будет, "
                           "бот передаст вопрос руководителю.")

    def start_dealer_reply(self, chat_id, emp, event_id):
        e = self.store.event(event_id)
        if not e or e["kind"] != "dealer_q":
            raise ValidationError(["Вопрос не найден."])
        if e["details"]["answered"]:
            return "На этот вопрос уже ответили."
        o = self.store.order(e["order_id"])
        if self.demo or emp["role"] == "manager" or emp["id"] == o["owner_id"]:
            actor = emp if emp["role"] != "dealer" else (self.store.employee(o["owner_id"]) or self.store.employees("manager")[0])
        else:
            raise ValidationError(["Ответить может ответственный за заказ или руководитель."])
        self.store.set_state(chat_id, {"flow": "dealer_reply", "event": event_id, "actor": actor["id"]})
        self.send(chat_id, f"Напишите ответ дилеру по {o['id']}.\n/cancel — отменить")
        return None

    def finish_dealer_reply(self, chat_id, state, text):
        e = self.store.event(state["event"])
        actor = self.store.employee(state["actor"])
        self.store.update_event(e["id"], answered=True)
        self.store.log(e["order_id"], actor["id"], "dealer_a", self.now(), text=text, question=e["id"])
        self.store.set_state(chat_id, {})
        self.notify(e["actor_id"], f"💬 Ответ по {e['order_id']} от {h(who(actor))}:\n«{h(text)}»")
        self.send(chat_id, "Ответ отправлен дилеру.")

    def open_dealer_questions(self):
        return [e for e in self.store.events("dealer_q") if not e["details"]["answered"]]

    # --- сводка руководителю -------------------------------------------------------------------
    def digest(self, now):
        rows = [(o, *core.assess(o, now)) for o in self.store.orders()]
        reds = [r for r in rows if r[1] == "red"]
        yellows = [r for r in rows if r[1] == "yellow"]
        greens = [r for r in rows if r[1] == "green"]
        total = sum(o["revenue"] - o["cost"] for o, _, _ in rows)
        at_risk = sum(o["revenue"] - o["cost"] for o, _, _ in reds + yellows)
        lines = [
            f"📊 <b>Сводка на {fmt_date(now.date())}, {now:%H:%M}</b>",
            f"Активных заказов: {len(rows)} · 🔴 {len(reds)} · 🟡 {len(yellows)} · 🟢 {len(greens)}",
            f"Плановая маржа в зоне риска: <b>{fmt_money(at_risk)}</b> из {fmt_money(total)}",
        ]
        for title, group in (("🔴 <b>Критично</b>", reds), ("🟡 <b>Внимание</b>", yellows)):
            if group:
                lines += ["", title]
                for o, _, reasons in group:
                    owner = self.store.employee(o["owner_id"])
                    lines.append(f"• <b>{o['id']}</b> — {h('; '.join(reasons))}\n   → {h(owner['name']) if owner else 'никто'}: {h(o['next_action'])}")
        silent = {}
        for o, _, reasons in rows:
            if "нет статуса больше суток" in reasons and o["owner_id"]:
                silent.setdefault(o["owner_id"], []).append(o["id"])
        if silent:
            lines += ["", "🤐 <b>Молчат больше суток:</b> " + ", ".join(
                f"{h(self.store.employee(e)['name'])} ({', '.join(ids)})" for e, ids in silent.items())]
        questions = self.open_dealer_questions()
        if questions:
            lines += ["", f"❓ Вопросов дилеров без ответа: {len(questions)}"]
        kb = []
        for o, _, _ in reds:
            kb += self.manager_kb(o)
        if kb:
            lines += ["", "<b>Ждут вашего решения</b> — кнопки ниже."]
        return "\n".join(lines), kb

    def send_digest(self, chat_id):
        text, kb = self.digest(self.now())
        self.send(chat_id, text, kb or None)

    # --- выгрузка -----------------------------------------------------------------------------
    def export_files(self, now):
        registry = io.StringIO()
        w = csv.writer(registry, delimiter=";")
        w.writerow(["Заказ", "Клиент", "Город", "Дилер", "Стадия", "Ответственный", "План монтажа", "Прогноз",
                    "Риск", "Причины", "Блокер", "Следующий шаг", "Выручка", "Себестоимость", "Маржа", "Маржа %", "Последний статус"])
        for o in self.store.orders(active=False):
            level, reasons = core.assess(o, now)
            owner = self.store.employee(o["owner_id"])
            dealer = self.store.employee(o["dealer_id"])
            margin = o["revenue"] - o["cost"]
            w.writerow([o["id"], o["client"], o["city"], dealer["name"] if dealer else "", o["stage"],
                        owner["name"] if owner else "", o["plan_date"], o["forecast_date"] or "", LEVEL_NAME[level],
                        "; ".join(reasons), o["blocker"], o["next_action"], o["revenue"], o["cost"], margin,
                        round(margin * 100 / o["revenue"]), (o["last_status_at"] or "")[:16].replace("T", " ")])
        journal = io.StringIO()
        w = csv.writer(journal, delimiter=";")
        w.writerow(["Время", "Заказ", "Кто", "Событие", "Детали"])
        for e in self.store.events():
            actor = self.store.employee(e["actor_id"])
            w.writerow([e["ts"][:16].replace("T", " "), e["order_id"], actor["name"] if actor else "", e["kind"],
                        json.dumps(e["details"], ensure_ascii=False)])
        stamp = now.strftime("%Y-%m-%d_%H%M")
        # utf-8-sig — чтобы Excel сразу открыл кириллицу без мастера импорта
        return [(f"orders_{stamp}.csv", registry.getvalue().encode("utf-8-sig")),
                (f"journal_{stamp}.csv", journal.getvalue().encode("utf-8-sig"))]

    def send_export(self, chat_id):
        for name, content in self.export_files(self.now()):
            self.api.call("sendDocument", {"chat_id": chat_id}, files={"document": (name, content, "text/csv")})

    # --- расписание ---------------------------------------------------------------------------
    def morning_ping(self, now):
        count = 0
        for emp in self.store.employees():
            if emp["role"] in ("manager", "dealer"):
                continue
            orders = self.store.orders_of(emp["id"])
            if not orders:
                continue
            self.notify(emp["id"], f"☀️ Доброе утро, {h(emp['name'])}! Отметьте статус по своим заказам ({len(orders)}) — это минута.")
            for o in orders:
                self.notify(emp["id"], self.order_card(o), self.status_kb(o), demo_header=False)
            count += 1
        self.store.meta_set("last_ping", now.isoformat())
        return f"{count} сотрудникам"

    def remind_silent(self, now):
        ping = self.store.meta_get("last_ping")
        if not ping:
            return
        ping_at = datetime.fromisoformat(ping)
        for emp in self.store.employees():
            pending = [o for o in self.store.orders_of(emp["id"])
                       if not o["last_status_at"] or datetime.fromisoformat(o["last_status_at"]) < ping_at]
            if pending:
                self.notify(emp["id"], "⏰ Напоминание: нет статуса по " + ", ".join(o["id"] for o in pending) + ".")
                for o in pending:
                    self.notify(emp["id"], self.order_card(o), self.status_kb(o), demo_header=False)

    def escalate_questions(self, now):
        for e in self.open_dealer_questions():
            if e["details"]["escalated"] or now - datetime.fromisoformat(e["ts"]) < self.escalate_after:
                continue
            self.store.update_event(e["id"], escalated=True)
            dealer = self.store.employee(e["actor_id"])
            self.notify_managers(f"⏰ Дилер {h(dealer['name'])} ждёт ответа по {e['order_id']} дольше 2 часов:\n«{h(e['details']['text'])}»",
                                 [[btn("↩️ Ответить дилеру", f"dr:{e['id']}")]])

    def tick(self, now):
        self.escalate_questions(now)
        if not self.schedule:
            return
        today = now.date().isoformat()
        jobs = (("ping", self.morning_ping), ("digest", self.digest_to_managers), ("remind", self.remind_silent))
        for key, job in jobs:
            if now.time() >= self.times[key] and self.store.meta_get(f"done:{key}") != today:
                self.store.meta_set(f"done:{key}", today)
                job(now)

    def skip_missed_jobs(self, now):
        """После запуска не догоняем пропущенные за сегодня рассылки — иначе старт бота в обед разошлёт всё сразу."""
        for key, at in self.times.items():
            if now.time() >= at:
                self.store.meta_set(f"done:{key}", now.date().isoformat())

    def digest_to_managers(self, now):
        text, kb = self.digest(now)
        self.notify_managers(text, kb or None)

    # --- цикл ---------------------------------------------------------------------------------
    def run(self):
        self.api.call("deleteWebhook")
        self.api.call("setMyCommands", {"commands": [
            {"command": "start", "description": "Выбрать роль"},
            {"command": "orders", "description": "Мои заказы"},
            {"command": "digest", "description": "Сводка руководителя"},
            {"command": "cancel", "description": "Прервать ввод"},
            {"command": "help", "description": "Как работает бот"},
            {"command": "reset", "description": "Сбросить демо-данные"},
        ]})
        me = self.api.call("getMe")
        log.info("Бот @%s запущен (демо=%s, расписание=%s)", me["username"], self.demo, self.schedule)
        self.skip_missed_jobs(self.now())
        offset = None
        while True:
            try:
                updates = self.api.call("getUpdates", {"timeout": 20, "offset": offset,
                                                       "allowed_updates": ["message", "callback_query"]}, timeout=30)
            except (urllib.error.URLError, TimeoutError, TelegramError) as e:
                log.warning("Не удалось получить обновления: %s", e)
                time.sleep(3)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    self.handle_update(upd)
                except Exception:
                    log.exception("Ошибка при обработке обновления %s", upd.get("update_id"))
            try:
                self.tick(self.now())
            except Exception:
                log.exception("Ошибка в расписании")


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(HERE / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Нет токена: положите TELEGRAM_BOT_TOKEN=... в файл .env (см. .env.example).")
    tz = ZoneInfo(os.environ.get("BOT_TZ", "Asia/Yekaterinburg"))
    demo = env_flag("DEMO_MODE", "1")
    db_path = os.environ.get("DB_PATH", str(HERE / "data" / "bot.db"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    bot = Bot(
        TelegramAPI(token),
        core.Store(db_path),
        clock=lambda: datetime.now(tz),
        demo=demo,
        schedule=env_flag("SCHEDULE", "0" if demo else "1"),
        ping_at=os.environ.get("PING_AT", "09:00"),
        digest_at=os.environ.get("DIGEST_AT", "10:00"),
        remind_at=os.environ.get("REMIND_AT", "14:00"),
        escalate_minutes=int(os.environ.get("ESCALATE_MINUTES", "120")),
    )
    bot.run()


if __name__ == "__main__":
    main()
