"""SQLite persistence for inverter report runs."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_runs (
    run_id INTEGER PRIMARY KEY,
    system_name TEXT NOT NULL,
    system_id TEXT NOT NULL,
    report_date TEXT NOT NULL,
    run_at TEXT NOT NULL,
    inventory_complete INTEGER NOT NULL CHECK (inventory_complete IN (0, 1))
);

CREATE INDEX IF NOT EXISTS report_runs_by_system_and_time
    ON report_runs (system_id, run_at, run_id);

CREATE TABLE IF NOT EXISTS inverter_observations (
    run_id INTEGER NOT NULL REFERENCES report_runs (run_id),
    ecu_id TEXT NOT NULL,
    inverter_id TEXT NOT NULL,
    inverter_type TEXT,
    PRIMARY KEY (run_id, ecu_id, inverter_id)
);

CREATE TABLE IF NOT EXISTS channel_energy_readings (
    run_id INTEGER NOT NULL,
    ecu_id TEXT NOT NULL,
    inverter_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    energy_kwh REAL NOT NULL,
    PRIMARY KEY (run_id, ecu_id, inverter_id, channel),
    FOREIGN KEY (run_id, ecu_id, inverter_id)
        REFERENCES inverter_observations (run_id, ecu_id, inverter_id)
);

CREATE TABLE IF NOT EXISTS inverter_errors (
    run_id INTEGER NOT NULL,
    ecu_id TEXT NOT NULL,
    inverter_id TEXT NOT NULL,
    error_number INTEGER NOT NULL,
    error_message TEXT NOT NULL,
    PRIMARY KEY (run_id, ecu_id, inverter_id, error_number),
    FOREIGN KEY (run_id, ecu_id, inverter_id)
        REFERENCES inverter_observations (run_id, ecu_id, inverter_id)
);
"""


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: Path) -> None:
    """Create the database directory and tables when they do not exist."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(database_path)) as connection, connection:
        connection.executescript(_SCHEMA)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _inverter_errors(inverter: dict[str, Any]) -> list[str]:
    errors = inverter.get("errors")
    if errors is None:
        error = inverter.get("error")
        if error is None:
            return []
        if not isinstance(error, str) or not error:
            raise ValueError("inverter error must be a non-empty string")
        return [error]
    if not isinstance(errors, list) or any(
        not isinstance(error, str) or not error for error in errors
    ):
        raise ValueError("inverter errors must be a list of non-empty strings")
    return errors


def save_inverter_run(
    database_path: Path,
    *,
    system_name: str,
    system_id: str,
    report_date: str,
    inverters: list[dict[str, Any]],
    run_at: str | None = None,
    inventory_complete: bool = True,
) -> int:
    """Persist one immutable inventory snapshot and its channel readings."""
    system_name = _required_text(system_name, "system_name")
    system_id = _required_text(system_id, "system_id")
    report_date = _required_text(report_date, "report_date")
    if not isinstance(inverters, list):
        raise ValueError("inverters must be a list")
    if not isinstance(inventory_complete, bool):
        raise ValueError("inventory_complete must be a boolean")
    if run_at is None:
        run_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    else:
        run_at = _required_text(run_at, "run_at")

    initialize_database(database_path)
    with closing(_connect(Path(database_path))) as connection, connection:
        cursor = connection.execute(
            """
            INSERT INTO report_runs
                (system_name, system_id, report_date, run_at, inventory_complete)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                system_name,
                system_id,
                report_date,
                run_at,
                int(inventory_complete),
            ),
        )
        run_id = cursor.lastrowid
        if run_id is None:
            raise RuntimeError("SQLite did not return an ID for the report run")

        for inverter in inverters:
            if not isinstance(inverter, dict):
                raise ValueError("each inverter observation must be an object")
            ecu_id = _required_text(inverter.get("ecu_id"), "ecu_id")
            inverter_id = _required_text(inverter.get("inverter_id"), "inverter_id")
            inverter_type = inverter.get("type")
            if inverter_type is not None and not isinstance(inverter_type, str):
                raise ValueError("inverter type must be a string or None")

            connection.execute(
                """
                INSERT INTO inverter_observations
                    (run_id, ecu_id, inverter_id, inverter_type)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, ecu_id, inverter_id, inverter_type),
            )

            channels = inverter.get("channel_energy_kwh", {})
            if not isinstance(channels, dict):
                raise ValueError("channel_energy_kwh must be an object")
            for channel, energy_kwh in channels.items():
                if not isinstance(channel, str) or not channel:
                    raise ValueError("channel names must be non-empty strings")
                if (
                    isinstance(energy_kwh, bool)
                    or not isinstance(energy_kwh, (int, float))
                    or not math.isfinite(energy_kwh)
                ):
                    raise ValueError(
                        f"energy for channel {channel} must be a finite number"
                    )
                connection.execute(
                    """
                    INSERT INTO channel_energy_readings
                        (run_id, ecu_id, inverter_id, channel, energy_kwh)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, ecu_id, inverter_id, channel, float(energy_kwh)),
                )

            for error_number, error_message in enumerate(
                _inverter_errors(inverter)
            ):
                connection.execute(
                    """
                    INSERT INTO inverter_errors
                        (run_id, ecu_id, inverter_id, error_number, error_message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, ecu_id, inverter_id, error_number, error_message),
                )

    return run_id


def get_inventory_changes(database_path: Path, run_id: int) -> dict[str, Any]:
    """Compare a run's inventory with the preceding complete run for its system."""
    initialize_database(database_path)
    with closing(_connect(Path(database_path))) as connection, connection:
        current_run = connection.execute(
            """
            SELECT run_id, system_id, run_at, inventory_complete
            FROM report_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if current_run is None:
            raise ValueError(f"Unknown report run ID: {run_id}")

        result: dict[str, Any] = {
            "comparable": False,
            "baseline_run_id": None,
            "new_inverters": [],
            "missing_inverters": [],
        }
        if not current_run["inventory_complete"]:
            result["reason"] = "current inventory scan is incomplete"
            return result

        baseline = connection.execute(
            """
            SELECT run_id
            FROM report_runs
            WHERE system_id = ?
              AND inventory_complete = 1
              AND (
                    run_at < ?
                    OR (run_at = ? AND run_id < ?)
              )
            ORDER BY run_at DESC, run_id DESC
            LIMIT 1
            """,
            (
                current_run["system_id"],
                current_run["run_at"],
                current_run["run_at"],
                run_id,
            ),
        ).fetchone()
        if baseline is None:
            result["reason"] = "no previous complete inventory snapshot"
            return result

        baseline_run_id = baseline["run_id"]
        result["baseline_run_id"] = baseline_run_id
        result["comparable"] = True
        current_rows = connection.execute(
            """
            SELECT ecu_id, inverter_id, inverter_type
            FROM inverter_observations
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
        baseline_rows = connection.execute(
            """
            SELECT ecu_id, inverter_id, inverter_type
            FROM inverter_observations
            WHERE run_id = ?
            """,
            (baseline_run_id,),
        ).fetchall()

        current = {
            (row["ecu_id"], row["inverter_id"]): row for row in current_rows
        }
        previous = {
            (row["ecu_id"], row["inverter_id"]): row for row in baseline_rows
        }
        result["new_inverters"] = [
            {
                "ecu_id": ecu_id,
                "inverter_id": inverter_id,
                "type": current[(ecu_id, inverter_id)]["inverter_type"],
            }
            for ecu_id, inverter_id in sorted(current.keys() - previous.keys())
        ]
        result["missing_inverters"] = [
            {
                "ecu_id": ecu_id,
                "inverter_id": inverter_id,
                "type": previous[(ecu_id, inverter_id)]["inverter_type"],
            }
            for ecu_id, inverter_id in sorted(previous.keys() - current.keys())
        ]
        return result


def get_nonproducing_channel_changes(
    database_path: Path, run_id: int
) -> dict[str, Any]:
    """Compare zero-or-negative channel readings with the preceding complete run."""
    initialize_database(database_path)
    with closing(_connect(Path(database_path))) as connection, connection:
        current_run = connection.execute(
            """
            SELECT run_id, system_id, run_at, inventory_complete
            FROM report_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if current_run is None:
            raise ValueError(f"Unknown report run ID: {run_id}")

        result: dict[str, Any] = {
            "comparable": False,
            "baseline_run_id": None,
            "previous_count": None,
            "current_count": None,
            "changed": False,
        }
        if not current_run["inventory_complete"]:
            result["reason"] = "current inventory scan is incomplete"
            return result

        current_has_errors = connection.execute(
            "SELECT 1 FROM inverter_errors WHERE run_id = ? LIMIT 1",
            (run_id,),
        ).fetchone()
        if current_has_errors:
            result["reason"] = "current run has incomplete inverter readings"
            return result

        baseline = connection.execute(
            """
            SELECT runs.run_id
            FROM report_runs AS runs
            WHERE runs.system_id = ?
              AND runs.inventory_complete = 1
              AND NOT EXISTS (
                    SELECT 1
                    FROM inverter_errors AS errors
                    WHERE errors.run_id = runs.run_id
              )
              AND (
                    runs.run_at < ?
                    OR (runs.run_at = ? AND runs.run_id < ?)
              )
            ORDER BY runs.run_at DESC, runs.run_id DESC
            LIMIT 1
            """,
            (
                current_run["system_id"],
                current_run["run_at"],
                current_run["run_at"],
                run_id,
            ),
        ).fetchone()
        if baseline is None:
            result["reason"] = "no previous complete reading snapshot"
            return result

        baseline_run_id = baseline["run_id"]
        counts = connection.execute(
            """
            SELECT run_id, COUNT(*) AS nonproducing_count
            FROM channel_energy_readings
            WHERE run_id IN (?, ?)
              AND energy_kwh <= 0
            GROUP BY run_id
            """,
            (baseline_run_id, run_id),
        ).fetchall()
        counts_by_run = {row["run_id"]: row["nonproducing_count"] for row in counts}
        previous_count = counts_by_run.get(baseline_run_id, 0)
        current_count = counts_by_run.get(run_id, 0)
        result.update(
            {
                "comparable": True,
                "baseline_run_id": baseline_run_id,
                "previous_count": previous_count,
                "current_count": current_count,
                "changed": previous_count != current_count,
                "count_delta": current_count - previous_count,
            }
        )
        return result
