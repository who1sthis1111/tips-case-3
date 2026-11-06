#!/usr/bin/env python3
"""
CLI-прототип платформы чаевых — кейс 3.  Python stdlib only.

Команды:
    create  req.json        # проверки QR/сумм -> платёж CREATED
    confirm <payment_id>    # CREATED -> PENDING -> эмулятор -> SUCCESS/DECLINED/PENDING
    cancel  <payment_id>    # только CREATED -> CANCELED
    auto    req.json        # create + confirm

req.json:
    {"qr_id": "qr_001", "receipt_id": "r_1001", "base_amount": 637.00,
     "tip_pct": 5, "idempotency_key": "k-1", "now": "2025-11-01T18:00:00Z"}
    now — необязательное тестовое время; иначе env TIP_NOW; иначе реальное UTC.

Пишет ТОЛЬКО: payments_out.csv, idempotency.json, used_nonces.json.
Исходные CSV (qr_links, merchants, employees, payments) только читаются.

env: TIP_NOW       — тестовое "сейчас" (ISO 8601)
     TIP_DATA_DIR  — где лежат исходные CSV (иначе ./01_данные, затем ./)
     TIP_OUT_DIR   — куда писать выходные файлы (иначе рядом со скриптом)

Коды выхода: 0 — CREATED/SUCCESS/CANCELED; 1 — DECLINED/PENDING;
             2 — бизнес-ошибка; 3 — сбой данных/ввода-вывода; 64 — неверный вызов.
"""
import csv
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SECRET = b"demo-secret-k3"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("TIP_OUT_DIR") or BASE_DIR
OUT_CSV = os.path.join(OUT_DIR, "payments_out.csv")
IDEM_JSON = os.path.join(OUT_DIR, "idempotency.json")
NONCES_JSON = os.path.join(OUT_DIR, "used_nonces.json")

OUT_FIELDS = ["payment_id", "merchant_id", "employee_id", "amount", "tip", "status", "timestamp"]

MAX_BASE = Decimal("1000000")
MAX_TOTAL = Decimal("2000000")
MAX_TIP_PCT = Decimal("50")
CENT = Decimal("0.01")

# HTTP-коды. Источники: RECEIPT_MISMATCH 422 и AMOUNT_OUT_OF_RANGE 400 — блок-схема;
# INVALID_QR 422 и QR_EXPIRED 410 — 03_тесты (T3-3); CANCEL_NOT_ALLOWED 409 — 03_тесты (T3-5).
# ПРЕДПОЛОЖЕНИЯ (в источниках нет, см. вопросы Архитектору): QR_USED, MERCHANT_BLOCKED,
# TIP_OUT_OF_RANGE, а также служебные PAYMENT_NOT_FOUND, INVALID_STATE, BAD_REQUEST.
HTTP = {
    "INVALID_QR": 422,
    "QR_EXPIRED": 410,
    "QR_USED": 409,             # предположение
    "MERCHANT_BLOCKED": 403,    # предположение
    "RECEIPT_MISMATCH": 422,
    "AMOUNT_OUT_OF_RANGE": 400,
    "TIP_OUT_OF_RANGE": 400,    # предположение
    "CANCEL_NOT_ALLOWED": 409,
    "PAYMENT_NOT_FOUND": 404,   # служебный, не из каталога
    "INVALID_STATE": 409,       # служебный, не из каталога
    "BAD_REQUEST": 400,         # служебный, не из каталога
}
RETRYABLE = {"GATEWAY_TIMEOUT"}  # предположение: таблица retryable в шаблоне не заполнена


class ApiError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message


# ---------- время ----------

def parse_dt(value):
    """ISO 8601 -> aware datetime (UTC). 'Z' поддержан; без пояса считаем UTC."""
    if not isinstance(value, str):
        raise ValueError("ожидается строка ISO 8601")
    s = value.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dt(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_now(req=None):
    """Тестовое время: req['now'] -> env TIP_NOW -> реальное UTC."""
    raw = (req or {}).get("now") or os.environ.get("TIP_NOW")
    if raw:
        try:
            return parse_dt(raw)
        except ValueError as e:
            raise ApiError("BAD_REQUEST", f"now: {e}")
    return datetime.now(timezone.utc)


# ---------- справочники (только чтение) ----------

def data_path(name):
    dirs = []
    if os.environ.get("TIP_DATA_DIR"):
        dirs.append(os.environ["TIP_DATA_DIR"])
    dirs += [os.path.join(BASE_DIR, "01_данные"), BASE_DIR]
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(dirs[0], name)  # open() поднимет FileNotFoundError


def load_qr(qr_id):
    with open(data_path("qr_links.csv"), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["qr_id"] == qr_id:
                return row
    return None


def load_merchant(merchant_id):
    with open(data_path("merchants.csv"), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["merchant_id"] == merchant_id:
                return row
    return None


# ---------- проверки QR ----------

def verify_signature(qr):
    payload = f"{qr['merchant_id']}|{qr['employee_id']}|{qr['receipt_id']}|{qr['nonce']}|{qr['exp']}"
    expected = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(expected.encode(), qr["signature"].encode())


def check_exp(qr, now):
    """exp >= now, иначе QR_EXPIRED."""
    try:
        exp = parse_dt(qr["exp"])
    except ValueError:
        raise ApiError("INVALID_QR", "exp не разобран")
    if exp < now:
        raise ApiError("QR_EXPIRED", f"exp={qr['exp']} < now={fmt_dt(now)}")


def check_nonce(qr, nonces):
    if qr["nonce"] in nonces["nonces"]:
        raise ApiError("QR_USED", f"nonce {qr['nonce']} уже использован")


def check_merchant(qr):
    m = load_merchant(qr["merchant_id"])
    status = m["status"] if m else None
    if status != "ACTIVE":
        raise ApiError("MERCHANT_BLOCKED", f"{qr['merchant_id']}: status={status}")


def check_receipt(qr, receipt_id):
    if receipt_id != qr["receipt_id"]:
        raise ApiError("RECEIPT_MISMATCH", f"запрос={receipt_id!r}, QR={qr['receipt_id']!r}")


# ---------- суммы ----------

def to_decimal(value, field):
    if value is None or isinstance(value, bool):
        raise ApiError("BAD_REQUEST", f"{field}: ожидается число")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise ApiError("BAD_REQUEST", f"{field}: не число")
    if not d.is_finite():
        raise ApiError("BAD_REQUEST", f"{field}: не конечное число")
    return d


def calc_amounts(base, pct):
    """tip = round(base*pct/100, 2); total = round(base+tip, 2). Decimal, HALF_UP."""
    tip = (base * pct / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    total = (base + tip).quantize(CENT, rounding=ROUND_HALF_UP)
    return tip, total


def validate_amounts(base, pct):
    """Возвращает (tip, total) или поднимает AMOUNT_/TIP_OUT_OF_RANGE."""
    if base <= 0 or base > MAX_BASE:
        raise ApiError("AMOUNT_OUT_OF_RANGE", f"base_amount={base}")
    if pct < 0 or pct > MAX_TIP_PCT:
        raise ApiError("TIP_OUT_OF_RANGE", f"tip_pct={pct}")
    if base != base.quantize(CENT):
        raise ApiError("BAD_REQUEST", "base_amount: не более 2 знаков после запятой")
    tip, total = calc_amounts(base, pct)
    if total > MAX_TOTAL:
        raise ApiError("AMOUNT_OUT_OF_RANGE", f"amount_total={total}")
    return tip, total


# ---------- эмулятор шлюза ----------

def gateway_h(qr_id, nonce):
    return int(hashlib.md5((qr_id + nonce).encode()).hexdigest(), 16) % 10


def emulate_gateway(qr_id, nonce):
    """0..7 SUCCESS; 8 DECLINED/CARD_DECLINED; 9 PENDING/GATEWAY_TIMEOUT."""
    h = gateway_h(qr_id, nonce)
    if h <= 7:
        return "SUCCESS", None
    if h == 8:
        return "DECLINED", "CARD_DECLINED"
    return "PENDING", "GATEWAY_TIMEOUT"


# ---------- JSON-хранилища ----------

def load_json(path, default):
    """Нет файла -> default. Битый файл -> ошибка (не затираем молча)."""
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_state():
    st = load_json(IDEM_JSON, {})
    st.setdefault("keys", {})       # idempotency_key -> payment_id
    st.setdefault("payments", {})   # payment_id -> полная запись платежа
    return st


def load_nonces():
    n = load_json(NONCES_JSON, {})
    n.setdefault("nonces", {})      # nonce -> {payment_id, at}
    return n


def check_idempotency(state, key):
    """Запись платежа по ключу или None. Вызывается ДО любых проверок QR/nonce."""
    if not key:
        return None
    pid = state["keys"].get(key)
    return state["payments"].get(pid) if pid else None


def mark_nonce_used(nonce, payment_id, now):
    nonces = load_nonces()
    nonces["nonces"][nonce] = {"payment_id": payment_id, "at": fmt_dt(now)}
    save_json(NONCES_JSON, nonces)


# ---------- payments_out.csv ----------

def write_payment(payment):
    """Upsert строки по payment_id: одна строка на платёж, статус обновляется на месте."""
    rows = []
    if os.path.exists(OUT_CSV):
        with open(OUT_CSV, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    row = {k: payment[k] for k in OUT_FIELDS}
    for i, r in enumerate(rows):
        if r["payment_id"] == row["payment_id"]:
            rows[i] = row
            break
    else:
        rows.append(row)
    tmp = OUT_CSV + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUT_CSV)


def persist(state, rec):
    state["payments"][rec["payment_id"]] = rec
    save_json(IDEM_JSON, state)
    write_payment(rec)


def next_payment_id(state):
    """p_NNNN: максимум по своим платежам и по исходному payments.csv (чтобы id не пересекались)."""
    nums = [int(p[2:]) for p in state["payments"] if p[2:].isdigit()]
    src = data_path("payments.csv")
    if os.path.exists(src):
        with open(src, encoding="utf-8", newline="") as f:
            nums += [int(r["payment_id"][2:]) for r in csv.DictReader(f)
                     if r["payment_id"][2:].isdigit()]
    return f"p_{(max(nums) if nums else 0) + 1:04d}"


# ---------- операции ----------

def create_payment(req, now):
    state = load_state()
    key = req.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise ApiError("BAD_REQUEST", "idempotency_key: ожидается непустая строка")

    existing = check_idempotency(state, key)          # 1. идемпотентность — ДО nonce
    if existing:
        return existing, True

    qr_id = req.get("qr_id")
    if not isinstance(qr_id, str):
        raise ApiError("BAD_REQUEST", "qr_id обязателен")
    qr = load_qr(qr_id)
    if not qr or not verify_signature(qr):             # 2. QR найден, подпись
        raise ApiError("INVALID_QR", f"{qr_id}: не найден или подпись не совпала")
    check_exp(qr, now)                                 # 3. TTL
    check_nonce(qr, load_nonces())                     # 4. nonce не использован
    check_merchant(qr)                                 # 5. мерчант ACTIVE
    check_receipt(qr, req.get("receipt_id"))           # 6. чек совпадает

    base = to_decimal(req.get("base_amount"), "base_amount")
    pct = to_decimal(req.get("tip_pct"), "tip_pct")
    tip, total = validate_amounts(base, pct)           # 7. суммы

    pid = next_payment_id(state)
    rec = {
        "payment_id": pid,
        "merchant_id": qr["merchant_id"],
        "employee_id": qr["employee_id"],
        "amount": format(base, ".2f"),
        "tip": format(tip, ".2f"),
        "status": "CREATED",
        "timestamp": fmt_dt(now),
        # контекст для confirm/cancel (в payments_out.csv не попадает)
        "qr_id": qr_id,
        "receipt_id": qr["receipt_id"],
        "nonce": qr["nonce"],
        "tip_pct": str(pct),
        "amount_total": format(total, ".2f"),
        "error_code": None,
    }
    if key:
        state["keys"][key] = pid
    persist(state, rec)
    return rec, False


def confirm_payment(payment_id, now):
    state = load_state()
    rec = state["payments"].get(payment_id)
    if not rec:
        raise ApiError("PAYMENT_NOT_FOUND", payment_id)
    if rec["status"] != "CREATED":
        raise ApiError("INVALID_STATE", f"confirm из {rec['status']} не определён")
    check_nonce({"nonce": rec["nonce"]}, load_nonces())  # другой платёж по этому QR мог пройти

    rec["status"], rec["timestamp"] = "PENDING", fmt_dt(now)
    persist(state, rec)                                  # CREATED -> PENDING

    status, error = emulate_gateway(rec["qr_id"], rec["nonce"])
    if status == "SUCCESS":
        mark_nonce_used(rec["nonce"], payment_id, now)   # nonce — раньше, чем SUCCESS в записи
    rec["status"], rec["error_code"], rec["timestamp"] = status, error, fmt_dt(now)
    persist(state, rec)
    return rec


def cancel_payment(payment_id, now):
    state = load_state()
    rec = state["payments"].get(payment_id)
    if not rec:
        raise ApiError("PAYMENT_NOT_FOUND", payment_id)
    if rec["status"] != "CREATED":
        raise ApiError("CANCEL_NOT_ALLOWED", f"отмена из {rec['status']}")
    rec["status"], rec["error_code"], rec["timestamp"] = "CANCELED", None, fmt_dt(now)
    persist(state, rec)
    return rec


# ---------- CLI ----------

def build_response(rec, replay=False, with_h=False):
    out = {
        "payment_id": rec["payment_id"],
        "status": rec["status"],
        "amount_total": float(Decimal(rec["amount_total"])),
        "tip_amount": float(Decimal(rec["tip"])),
        "error_code": rec.get("error_code"),
    }
    if with_h:
        out["gateway_h"] = gateway_h(rec["qr_id"], rec["nonce"])
    if replay:
        out["idempotent_replay"] = True
        out["http"] = 200
    return out


def read_request(path):
    try:
        with open(path, encoding="utf-8") as f:
            req = json.load(f)
    except (OSError, ValueError) as e:
        raise ApiError("BAD_REQUEST", f"{path}: {e}")
    if not isinstance(req, dict):
        raise ApiError("BAD_REQUEST", "корень JSON должен быть объектом")
    return req


def run(cmd, arg):
    if cmd in ("create", "auto"):
        req = read_request(arg)
        now = get_now(req)
        rec, replay = create_payment(req, now)
        if cmd == "auto" and not replay:
            rec = confirm_payment(rec["payment_id"], now)
        return build_response(rec, replay, with_h=(cmd == "auto" or rec["status"] != "CREATED"))
    now = get_now()
    if cmd == "confirm":
        return build_response(confirm_payment(arg, now), with_h=True)
    if cmd == "cancel":
        return build_response(cancel_payment(arg, now))
    raise ApiError("BAD_REQUEST", f"неизвестная команда {cmd}")


def main(argv):
    if len(argv) != 3 or argv[1] not in ("create", "confirm", "cancel", "auto"):
        print("Использование: 04_прототип.py create|auto req.json | confirm|cancel <payment_id>",
              file=sys.stderr)
        return 64
    try:
        out = run(argv[1], argv[2])
        code = 0 if out["status"] in ("CREATED", "SUCCESS", "CANCELED") else 1
    except ApiError as e:
        out = {"status": "ERROR", "error_code": e.code, "http": HTTP.get(e.code, 400),
               "retryable": e.code in RETRYABLE, "message": e.message}
        code = 2
    except (OSError, ValueError, KeyError) as e:
        out = {"status": "ERROR", "error_code": "INTERNAL_ERROR", "message": repr(e)}
        code = 3
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
