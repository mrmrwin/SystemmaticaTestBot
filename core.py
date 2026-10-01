"""Данные и правила бота статусов заказов.

Здесь нет ничего про Telegram: правила риска, проверка статусов и передача заказа
между стадиями живут отдельно от транспорта и проверяются тестами (см. tests/).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

STAGES = ["Замер", "Дизайн", "Производство", "Монтаж", "Сдан"]
STAGE_ROLE = {"Замер": "measurer", "Дизайн": "designer", "Производство": "production", "Монтаж": "installer"}
ROLE_TITLES = {
    "manager": "руководитель",
    "measurer": "замерщик",
    "designer": "дизайнер",
    "production": "производство",
    "installer": "монтажник",
    "dealer": "дилер",
}

# Минимальный запас дней до монтажа, чтобы заказ успел пройти оставшиеся стадии.
# Если запаса меньше — заказ отстаёт от графика ещё до того, как кто-то сообщил о проблеме.
STAGE_LEAD_DAYS = {"Замер": 21, "Дизайн": 14, "Производство": 3, "Монтаж": 0}

# Через сколько часов без статуса заказ считается «молчащим».
SILENCE_HOURS = 24

# Что обязательно передаётся на каждом стыке. Пока не отмечено всё — заказ дальше не уходит.
HANDOFF_CHECKLISTS = {
    "Замер": [
        "Размеры и высоты внесены в лист замера",
        "Фото стен и коммуникаций (вода, газ, розетки)",
        "Пожелания клиента записаны",
        "Клиент подписал лист замера",
    ],
    "Дизайн": [
        "Проект подписан клиентом",
        "Спецификация материалов и фурнитуры готова",
        "Модели и размеры техники клиента внесены",
        "Предоплата получена",
    ],
    "Производство": [
        "Все позиции изготовлены и упакованы",
        "Комплектность сверена со спецификацией",
        "Дата доставки согласована с клиентом",
        "Техника клиента на объекте",
    ],
    "Монтаж": [
        "Монтаж завершён",
        "Клиент подписал акт",
        "Фото результата загружены",
        "Замечаний нет или оформлена рекламация",
    ],
}

DEFAULT_NEXT_ACTION = {
    "Дизайн": "Принять замер и назначить встречу с клиентом",
    "Производство": "Проверить спецификацию и поставить заказ в график",
    "Монтаж": "Согласовать с клиентом время доставки и монтажа",
    "Сдан": "Заказ сдан клиенту",
}

EMPLOYEES = [
    ("olga", "Ольга", "manager"),
    ("sergey", "Сергей", "measurer"),
    ("pavel", "Павел", "measurer"),
    ("anna", "Анна", "designer"),
    ("ilya", "Илья", "production"),
    ("maxim", "Максим", "production"),
    ("roman", "Роман", "installer"),
    ("dlr_izh", "«Мебель+», Ижевск", "dealer"),
    ("dlr_kir", "«Дом кухни», Киров", "dealer"),
]

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

# Ответы, которые выглядят как заполненное поле, но ничего не сообщают.
VAGUE_ANSWERS = {"нет", "-", "—", "ничего", "не знаю", "хз", "ок", "ok", "всё", "все", "работаем", "делаем", "в процессе"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    city TEXT NOT NULL,
    dealer_id TEXT,
    stage TEXT NOT NULL,
    owner_id TEXT,
    plan_date TEXT NOT NULL,
    forecast_date TEXT,
    blocker TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    revenue INTEGER NOT NULL,
    cost INTEGER NOT NULL,
    decision_pending INTEGER NOT NULL DEFAULT 0,
    last_status_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    actor_id TEXT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, employee_id TEXT, state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class ValidationError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def fmt_date(value) -> str:
    d = value if isinstance(value, date) else date.fromisoformat(value)
    return f"{d.day} {MONTHS[d.month - 1]}"


def fmt_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def next_stage(stage: str):
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


ORDER_RE = re.compile(r"(?<![\w-])[KК]-?(\d{3,5})(?!\d)", re.IGNORECASE)


def find_order_id(text: str):
    m = ORDER_RE.search(text or "")
    return f"K-{m.group(1)}" if m else None


def parse_date(text: str, today: date):
    """Понимает 15.10, 15/10, 15.10.2026, 15.10.26 и 2026-10-15. Без года — ближайшая такая дата."""
    text = (text or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?", text)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        if m.group(3) is None:
            y = today.year
            try:
                if date(y, mo, d) < today - timedelta(days=60):
                    y += 1
            except ValueError:
                return None
        else:
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def validate_blocker(text: str):
    t = (text or "").strip()
    if len(t) < 5 or t.lower() in VAGUE_ANSWERS:
        return "Опишите, что именно мешает: без причины руководитель не сможет помочь. Например: «Поставщик перенёс фасады на 3 дня»."
    return None


def validate_next_action(text: str):
    t = (text or "").strip()
    if len(t) < 10 or t.lower() in VAGUE_ANSWERS or not re.search(r"[а-яёa-z]{3}", t.lower()):
        return "Следующий шаг должен быть конкретным: что сделать и к какому сроку. Например: «Позвонить поставщику фасадов до 12:00»."
    return None


def validate_forecast(kind: str, forecast, plan: date, today: date):
    if forecast is None:
        return "Укажите прогноз даты монтажа."
    if forecast < today:
        return "Прогноз не может быть в прошлом."
    if kind == "late" and forecast <= plan:
        return (f"Вы отметили срыв срока, но прогноз {fmt_date(forecast)} укладывается в план {fmt_date(plan)}. "
                "Укажите новую дату или выберите «Есть риск».")
    return None


def validate_status(kind, blocker, forecast, next_action, plan: date, today: date):
    errors = [validate_blocker(blocker), validate_forecast(kind, forecast, plan, today), validate_next_action(next_action)]
    return [e for e in errors if e]


def assess(order: dict, now: datetime):
    """Уровень риска заказа и причины. Уровни: red, yellow, green, done.

    Правила намеренно простые и объяснимые: каждая причина попадает в карточку,
    чтобы руководитель видел не только цвет, но и почему он такой.
    """
    if order["stage"] == "Сдан":
        return "done", []
    today = now.date()
    plan = date.fromisoformat(order["plan_date"])
    days = (plan - today).days
    red, yellow = [], []

    if days < 0:
        red.append(f"монтаж просрочен на {-days} дн.")
    if not order["owner_id"]:
        red.append("нет ответственного")
    if order["forecast_date"]:
        forecast = date.fromisoformat(order["forecast_date"])
        if forecast > plan:
            note = " — ждёт решения руководителя" if order["decision_pending"] else ""
            red.append(f"прогноз {fmt_date(forecast)} позже плана {fmt_date(plan)}{note}")

    lead = STAGE_LEAD_DAYS.get(order["stage"], 0)
    if 0 <= days < lead:
        yellow.append(f"до монтажа {days} дн., а заказ ещё на стадии «{order['stage']}» (нужно от {lead} дн.)")
    if order["blocker"]:
        yellow.append(f"блокер: {order['blocker']}")
    last = order["last_status_at"]
    if not last or now - datetime.fromisoformat(last) > timedelta(hours=SILENCE_HOURS):
        yellow.append("нет статуса больше суток")

    if red:
        return "red", red + yellow
    if yellow:
        return "yellow", yellow
    return "green", []


def seed_orders(now: datetime):
    today = now.date()

    def day(n):
        return (today + timedelta(days=n)).isoformat()

    def ago(hours):
        return (now - timedelta(hours=hours)).isoformat()

    return [
        dict(id="K-1039", client="Кухня на Комсомольском", city="Пермь", dealer_id=None, stage="Монтаж",
             owner_id="roman", plan_date=day(-2), blocker="Не хватило двух фасадов, ждём допечать",
             next_action="Забрать фасады с производства и закончить монтаж", revenue=520000, cost=401000, last_status_at=ago(20)),
        dict(id="K-1042", client="Кухня на Садовой", city="Пермь", dealer_id=None, stage="Производство",
             owner_id="ilya", plan_date=day(7), blocker="Поставщик перенёс фасады на 2 дня",
             next_action="Подтвердить у поставщика новую дату фасадов", revenue=640000, cost=456000, last_status_at=ago(18)),
        dict(id="K-1048", client="Кухня для семьи Орловых", city="Пермь", dealer_id=None, stage="Дизайн",
             owner_id="anna", plan_date=day(16), blocker="", next_action="Получить подпись клиента под проектом",
             revenue=410000, cost=284000, last_status_at=ago(5)),
        dict(id="K-1051", client="Кухня для клиента дилера", city="Ижевск", dealer_id="dlr_izh", stage="Замер",
             owner_id=None, plan_date=day(19), blocker="", next_action="Назначить замерщика",
             revenue=330000, cost=231000, last_status_at=ago(30)),
        dict(id="K-1054", client="Кухня в ЖК «Паруса»", city="Пермь", dealer_id=None, stage="Монтаж",
             owner_id="roman", plan_date=day(1), blocker="Не подтверждена доставка техники клиента",
             next_action="Позвонить клиенту и подтвердить технику до 15:00", revenue=760000, cost=548000, last_status_at=ago(6)),
        dict(id="K-1057", client="Кухня для клиента дилера", city="Киров", dealer_id="dlr_kir", stage="Производство",
             owner_id="maxim", plan_date=day(20), blocker="", next_action="Передать карту раскроя в цех",
             revenue=590000, cost=422000, last_status_at=ago(4)),
        dict(id="K-1061", client="Кухня на Мира", city="Пермь", dealer_id=None, stage="Дизайн",
             owner_id="anna", plan_date=day(11), blocker="", next_action="Внести правки клиента в проект",
             revenue=470000, cost=327000, last_status_at=ago(70)),
        dict(id="K-1063", client="Кухня на Ленина", city="Пермь", dealer_id=None, stage="Замер",
             owner_id="sergey", plan_date=day(32), blocker="", next_action="Выехать на замер в четверг в 10:00",
             revenue=380000, cost=262000, last_status_at=ago(3)),
    ]


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- демо-данные ---------------------------------------------------------------
    def is_empty(self) -> bool:
        return self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    def reset_demo(self, now: datetime):
        with self.db:
            for table in ("employees", "orders", "events", "meta"):
                self.db.execute(f"DELETE FROM {table}")
            self.db.execute("UPDATE chats SET state = '{}'")
            self.db.executemany("INSERT INTO employees (id, name, role) VALUES (?, ?, ?)", EMPLOYEES)
            for o in seed_orders(now):
                cols = ", ".join(o)
                marks = ", ".join("?" for _ in o)
                self.db.execute(f"INSERT INTO orders ({cols}) VALUES ({marks})", list(o.values()))
                if o["owner_id"]:
                    # последний статус, который сотрудник якобы дал до запуска бота — чтобы у карточки была история
                    kind = "risk" if o["blocker"] else "ok"
                    details = (dict(blocker=o["blocker"], forecast=o["plan_date"], next_action=o["next_action"])
                               if o["blocker"] else {})
                    self.db.execute("INSERT INTO events (order_id, actor_id, ts, kind, details) VALUES (?, ?, ?, ?, ?)",
                                    (o["id"], o["owner_id"], o["last_status_at"], kind, json.dumps(details, ensure_ascii=False)))

    # --- сотрудники и заказы -------------------------------------------------------
    def employee(self, emp_id):
        if not emp_id:
            return None
        row = self.db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
        return dict(row) if row else None

    def employees(self, role=None):
        if role:
            rows = self.db.execute("SELECT * FROM employees WHERE role = ? ORDER BY rowid", (role,))
        else:
            rows = self.db.execute("SELECT * FROM employees ORDER BY rowid")
        return [dict(r) for r in rows]

    def order(self, order_id):
        row = self.db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def orders(self, active=True):
        sql = "SELECT * FROM orders" + (" WHERE stage != 'Сдан'" if active else "") + " ORDER BY plan_date, id"
        return [dict(r) for r in self.db.execute(sql)]

    def orders_of(self, owner_id):
        return [o for o in self.orders() if o["owner_id"] == owner_id]

    def dealer_orders(self, dealer_id):
        return [o for o in self.orders(active=False) if o["dealer_id"] == dealer_id]

    def update_order(self, order_id, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE orders SET {sets} WHERE id = ?", [*fields.values(), order_id])

    # --- журнал событий ------------------------------------------------------------
    def log(self, order_id, actor_id, kind, now: datetime, **details) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO events (order_id, actor_id, ts, kind, details) VALUES (?, ?, ?, ?, ?)",
                (order_id, actor_id, now.isoformat(), kind, json.dumps(details, ensure_ascii=False)),
            )
        return cur.lastrowid

    def _event(self, row):
        e = dict(row)
        e["details"] = json.loads(e["details"])
        return e

    def event(self, event_id):
        row = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def update_event(self, event_id, **details):
        e = self.event(event_id)
        e["details"].update(details)
        with self.db:
            self.db.execute("UPDATE events SET details = ? WHERE id = ?", (json.dumps(e["details"], ensure_ascii=False), event_id))

    def history(self, order_id, limit=6):
        rows = self.db.execute("SELECT * FROM events WHERE order_id = ? ORDER BY id DESC LIMIT ?", (order_id, limit))
        return [self._event(r) for r in rows][::-1]

    def events(self, kind=None):
        if kind:
            rows = self.db.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))
        else:
            rows = self.db.execute("SELECT * FROM events ORDER BY id")
        return [self._event(r) for r in rows]

    # --- чаты и состояние диалога --------------------------------------------------
    def chat(self, chat_id):
        row = self.db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return None
        c = dict(row)
        c["state"] = json.loads(c["state"] or "{}")
        return c

    def bind_chat(self, chat_id, employee_id):
        with self.db:
            self.db.execute(
                "INSERT INTO chats (chat_id, employee_id, state) VALUES (?, ?, '{}') "
                "ON CONFLICT(chat_id) DO UPDATE SET employee_id = excluded.employee_id, state = '{}'",
                (chat_id, employee_id),
            )

    def set_state(self, chat_id, state: dict):
        with self.db:
            self.db.execute("UPDATE chats SET state = ? WHERE chat_id = ?", (json.dumps(state, ensure_ascii=False), chat_id))

    def chats_for(self, employee_id):
        return [r[0] for r in self.db.execute("SELECT chat_id FROM chats WHERE employee_id = ?", (employee_id,))]

    def all_chat_ids(self):
        return [r[0] for r in self.db.execute("SELECT chat_id FROM chats WHERE employee_id IS NOT NULL")]

    def meta_get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --- действия над заказом: всё, что меняет данные, проходит через эти функции ------------

def report_ok(store: Store, order_id, actor_id, now: datetime):
    store.update_order(order_id, blocker="", forecast_date=None, decision_pending=0, last_status_at=now.isoformat())
    store.log(order_id, actor_id, "ok", now)


def report_problem(store: Store, order_id, actor_id, kind, blocker, forecast: date, next_action, now: datetime) -> bool:
    """Сохраняет риск или срыв. Плановую дату не меняет: если прогноз позже плана,
    заказ ждёт решения руководителя. Возвращает True, если решение нужно."""
    order = store.order(order_id)
    plan = date.fromisoformat(order["plan_date"])
    errors = validate_status(kind, blocker, forecast, next_action, plan, now.date())
    if errors:
        raise ValidationError(errors)
    needs_decision = forecast > plan
    store.update_order(
        order_id,
        blocker=blocker.strip(),
        forecast_date=forecast.isoformat() if forecast != plan else None,
        next_action=next_action.strip(),
        decision_pending=int(needs_decision),
        last_status_at=now.isoformat(),
    )
    store.log(order_id, actor_id, kind, now, blocker=blocker.strip(), forecast=forecast.isoformat(), next_action=next_action.strip())
    return needs_decision


def decide(store: Store, order_id, actor_id, accept: bool, now: datetime):
    order = store.order(order_id)
    if not order["decision_pending"]:
        raise ValidationError(["Решение по этому заказу уже принято."])
    if accept:
        store.update_order(order_id, plan_date=order["forecast_date"], forecast_date=None, decision_pending=0)
        store.log(order_id, actor_id, "plan_moved", now, old=order["plan_date"], new=order["forecast_date"])
    else:
        store.update_order(order_id, decision_pending=0)
        store.log(order_id, actor_id, "plan_kept", now, plan=order["plan_date"])


def handoff(store: Store, order_id, actor_id, checked, new_owner_id, now: datetime) -> str:
    order = store.order(order_id)
    stage = order["stage"]
    nxt = next_stage(stage)
    if nxt is None:
        raise ValidationError(["Заказ уже сдан."])
    items = HANDOFF_CHECKLISTS[stage]
    missing = [item for i, item in enumerate(items) if i not in set(checked)]
    if missing:
        raise ValidationError(["Не подтверждено: " + "; ".join(missing)])
    if nxt != "Сдан":
        new_owner = store.employee(new_owner_id)
        if not new_owner or new_owner["role"] != STAGE_ROLE[nxt]:
            raise ValidationError([f"На стадию «{nxt}» заказ передаётся сотруднику с ролью «{ROLE_TITLES[STAGE_ROLE[nxt]]}»."])
    else:
        new_owner_id = order["owner_id"]
    store.update_order(
        order_id, stage=nxt, owner_id=new_owner_id, blocker="", forecast_date=None, decision_pending=0,
        next_action=DEFAULT_NEXT_ACTION[nxt], last_status_at=now.isoformat(),
    )
    store.log(order_id, actor_id, "handoff", now, from_stage=stage, to_stage=nxt, owner=new_owner_id, checklist=items)
    return nxt


def assign(store: Store, order_id, owner_id, actor_id, now: datetime):
    order = store.order(order_id)
    owner = store.employee(owner_id)
    role = STAGE_ROLE.get(order["stage"])
    if not owner or owner["role"] != role:
        raise ValidationError([f"На стадию «{order['stage']}» нужен сотрудник с ролью «{ROLE_TITLES.get(role, role)}»."])
    store.update_order(order_id, owner_id=owner_id)
    store.log(order_id, actor_id, "assign", now, owner=owner_id)
