"""SQLite 存取：运行清单、运价版本、运单、隔离区、计费重行、账单行。

只放数据访问，不放业务规则。每次调用开一个连接，用完即关。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  rules_version TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  rows_in INTEGER NOT NULL DEFAULT 0,
  rows_ok INTEGER NOT NULL DEFAULT 0,
  rows_quarantined INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  rate_digest TEXT NOT NULL DEFAULT '',
  stages TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rate_sets (
  carrier TEXT PRIMARY KEY,
  rate_set_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  version_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  first_weight_kg TEXT NOT NULL,
  first_price TEXT NOT NULL,
  step_weight_kg TEXT NOT NULL,
  step_price TEXT NOT NULL,
  min_charge TEXT NOT NULL,
  volume_divisor INTEGER NOT NULL,
  fuel_pct TEXT NOT NULL,
  remote_fee TEXT NOT NULL,
  insurance_pct TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (carrier, service, zone, version_id)
);
CREATE TABLE IF NOT EXISTS waybills (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  pickup_utc TEXT NOT NULL,
  weight_kg TEXT NOT NULL,
  length_cm TEXT NOT NULL,
  width_cm TEXT NOT NULL,
  height_cm TEXT NOT NULL,
  declared_value TEXT NOT NULL,
  pieces INTEGER NOT NULL,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS quarantine (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, line_no, code)
);
CREATE TABLE IF NOT EXISTS weight_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000',
  source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS charge_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS service_changes (
  run_id TEXT NOT NULL,
  change_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL DEFAULT '',
  from_service TEXT NOT NULL DEFAULT '',
  to_service TEXT NOT NULL DEFAULT '',
  zone TEXT NOT NULL DEFAULT '',
  change_type TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  seq INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'received',
  detail TEXT NOT NULL DEFAULT '',
  digest TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, change_id)
);
CREATE TABLE IF NOT EXISTS cost_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  change_id TEXT NOT NULL DEFAULT '',
  billed_service TEXT NOT NULL DEFAULT '',
  actual_service TEXT NOT NULL DEFAULT '',
  version_id TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  receivable_amount TEXT NOT NULL DEFAULT '0.00',
  downgrade_loss TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    got = rows(sql, params)
    return got[0] if got else None


def execute(sql: str, params: tuple = ()) -> None:
    with session() as conn:
        conn.execute(sql, params)


def executemany(sql: str, seq) -> None:
    with session() as conn:
        conn.executemany(sql, list(seq))


# --- 运行清单 -------------------------------------------------------------

RUN_COLUMNS = (
    "input_name", "input_sha256", "rules_version", "profile",
    "rows_in", "rows_ok", "rows_quarantined", "amount_total",
    "rate_digest", "stages", "notes", "created_at", "updated_at",
)


def get_run(run_id: str) -> dict | None:
    return one("SELECT * FROM runs WHERE run_id = ?", (run_id,))


def list_runs(limit: int = 20) -> list[dict]:
    return rows("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,))


def upsert_run(run_id: str, data: dict) -> None:
    with session() as conn:
        existing = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if existing:
            fields = [c for c in RUN_COLUMNS if c in data]
            assignments = ", ".join(f"{c} = ?" for c in fields)
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                tuple(data[c] for c in fields) + (run_id,),
            )
        else:
            payload = {c: data.get(c, "") for c in RUN_COLUMNS}
            payload["rows_in"] = int(payload.get("rows_in") or 0)
            payload["rows_ok"] = int(payload.get("rows_ok") or 0)
            payload["rows_quarantined"] = int(payload.get("rows_quarantined") or 0)
            cols = ("run_id",) + RUN_COLUMNS
            conn.execute(
                f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (run_id,) + tuple(payload[c] for c in RUN_COLUMNS),
            )


def add_stage(run_id: str, stage: str, updated_at: str) -> None:
    run = get_run(run_id)
    if not run:
        return
    parts = [p for p in (run.get("stages") or "").split(",") if p]
    if stage not in parts:
        parts.append(stage)
    execute(
        "UPDATE runs SET stages = ?, updated_at = ? WHERE run_id = ?",
        (",".join(parts), updated_at, run_id),
    )


# --- 运价版本 -------------------------------------------------------------

CARD_COLUMNS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)


def replace_rate_cards(cards: list[dict], rate_set_version: int, updated_at: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM rate_cards")
        conn.executemany(
            f"INSERT INTO rate_cards ({', '.join(CARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in CARD_COLUMNS)})",
            [tuple(card[c] for c in CARD_COLUMNS) for card in cards],
        )
        carriers = sorted({card["carrier"] for card in cards})
        conn.execute("DELETE FROM rate_sets")
        conn.executemany(
            "INSERT INTO rate_sets (carrier, rate_set_version, updated_at) VALUES (?, ?, ?)",
            [(c, rate_set_version, updated_at) for c in carriers],
        )


def rate_cards() -> list[dict]:
    return rows("SELECT * FROM rate_cards ORDER BY carrier, service, zone, effective_from")


def rate_set_version(carrier: str) -> int:
    got = one("SELECT rate_set_version FROM rate_sets WHERE carrier = ?", (carrier,))
    return int(got["rate_set_version"]) if got else 0


def max_rate_set_version() -> int:
    got = one("SELECT MAX(rate_set_version) AS v FROM rate_sets")
    return int(got["v"]) if got and got["v"] is not None else 0


# --- 运单 / 隔离区 / 计费行 ------------------------------------------------

def replace_waybills(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM waybills WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO waybills (run_id, line_no, waybill_no, carrier, service, zone, "
            "pickup_utc, weight_kg, length_cm, width_cm, height_cm, declared_value, pieces, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["line_no"], it["waybill_no"], it["carrier"], it["service"],
                    it["zone"], it["pickup_utc"], it["weight_kg"], it["length_cm"],
                    it["width_cm"], it["height_cm"], it["declared_value"], it["pieces"],
                    it["codes"],
                )
                for it in items
            ],
        )


def waybills(run_id: str) -> list[dict]:
    return rows("SELECT * FROM waybills WHERE run_id = ? ORDER BY line_no", (run_id,))


def replace_quarantine(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM quarantine WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO quarantine (run_id, line_no, waybill_no, code, detail, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, it["line_no"], it["waybill_no"], it["code"], it["detail"], it["raw"])
                for it in items
            ],
        )


def quarantine(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM quarantine WHERE run_id = ? ORDER BY line_no, code", (run_id,)
    )


def replace_weight_lines(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM weight_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO weight_lines (run_id, waybill_no, version_id, actual_kg, volumetric_kg, "
            "source, billable_kg, steps, codes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["version_id"], it["actual_kg"],
                    it["volumetric_kg"], it["source"], it["billable_kg"], it["steps"],
                    it.get("codes", ""),
                )
                for it in items
            ],
        )


def weight_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM weight_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def replace_charge_lines(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM charge_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO charge_lines (run_id, waybill_no, version_id, base_amount, step_amount, "
            "remote_amount, fuel_amount, insurance_amount, total_amount, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["version_id"], it["base_amount"],
                    it["step_amount"], it["remote_amount"], it["fuel_amount"],
                    it["insurance_amount"], it["total_amount"], it["codes"],
                )
                for it in items
            ],
        )


def charge_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM charge_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def latest_run_id() -> str:
    got = one("SELECT run_id FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1")
    return got["run_id"] if got else ""


# --- 服务变更 / 实际成本 ---------------------------------------------------

CHANGE_COLUMNS = (
    "change_id", "waybill_no", "carrier", "from_service", "to_service", "zone",
    "change_type", "source", "seq", "status", "detail", "digest", "received_at",
)

COST_COLUMNS = (
    "change_id", "billed_service", "actual_service", "version_id", "billable_kg",
    "base_amount", "step_amount", "remote_amount", "fuel_amount", "insurance_amount",
    "total_amount", "receivable_amount", "downgrade_loss", "codes",
)


def get_change(run_id: str, change_id: str) -> dict | None:
    return one(
        "SELECT * FROM service_changes WHERE run_id = ? AND change_id = ?",
        (run_id, change_id),
    )


def get_change_by_digest(run_id: str, digest: str) -> dict | None:
    """无上游变更号时的兜底幂等键：同规范化载荷只认一条。"""
    return one(
        "SELECT * FROM service_changes WHERE run_id = ? AND digest = ?",
        (run_id, digest),
    )


def insert_change(run_id: str, item: dict) -> None:
    with session() as conn:
        conn.execute(
            f"INSERT INTO service_changes (run_id, {', '.join(CHANGE_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' for _ in CHANGE_COLUMNS)})",
            (run_id,) + tuple(item[c] for c in CHANGE_COLUMNS),
        )


def service_changes(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM service_changes WHERE run_id = ? "
        "ORDER BY waybill_no, seq, received_at",
        (run_id,),
    )


def active_changes(run_id: str) -> list[dict]:
    """每票取 seq 最大的一条有效变更；同票旧变更不再参与重算。"""
    rows = service_changes(run_id)
    latest: dict[str, dict] = {}
    for row in rows:
        if row["status"] != "received":
            continue
        current = latest.get(row["waybill_no"])
        if current is None or (row["seq"], row["received_at"], row["change_id"]) > (
            current["seq"], current["received_at"], current["change_id"]
        ):
            latest[row["waybill_no"]] = row
    return [latest[key] for key in sorted(latest)]


def change_count(run_id: str) -> int:
    got = one(
        "SELECT COUNT(*) AS n FROM service_changes WHERE run_id = ?", (run_id,)
    )
    return int(got["n"]) if got else 0


def replace_cost_lines(run_id: str, items: list[dict]) -> None:
    """成本结果整体替换：旧结果先失效，再写新结果，重算不留残余。"""
    with session() as conn:
        conn.execute("DELETE FROM cost_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO cost_lines (run_id, waybill_no, change_id, billed_service, "
            "actual_service, version_id, billable_kg, base_amount, step_amount, "
            "remote_amount, fuel_amount, insurance_amount, total_amount, "
            "receivable_amount, downgrade_loss, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["change_id"], it["billed_service"],
                    it["actual_service"], it["version_id"], it["billable_kg"],
                    it["base_amount"], it["step_amount"], it["remote_amount"],
                    it["fuel_amount"], it["insurance_amount"], it["total_amount"],
                    it["receivable_amount"], it["downgrade_loss"], it["codes"],
                )
                for it in items
            ],
        )


def cost_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM cost_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))
