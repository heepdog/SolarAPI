from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from solar_api.db import (
    get_inventory_changes,
    get_nonproducing_channel_changes,
    save_inverter_run,
)


class InverterDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "reports.sqlite3"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def save_run(
        self,
        run_at: str,
        inverter_ids: list[str],
        *,
        inventory_complete: bool = True,
    ) -> int:
        return save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at=run_at,
            inventory_complete=inventory_complete,
            inverters=[
                {
                    "ecu_id": "ecu-1",
                    "inverter_id": inverter_id,
                    "type": "microinverter",
                    "channel_energy_kwh": {"e1": 1.25},
                }
                for inverter_id in inverter_ids
            ],
        )

    def test_same_day_runs_are_preserved_with_normalized_readings(self) -> None:
        first_run = self.save_run("2026-11-16T12:00:00+00:00", ["inv-1"])
        second_run = self.save_run("2026-11-16T12:01:00+00:00", ["inv-1"])

        with sqlite3.connect(self.database_path) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM report_runs"
            ).fetchone()[0]
            reading_count = connection.execute(
                "SELECT COUNT(*) FROM channel_energy_readings"
            ).fetchone()[0]
            observation_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(inverter_observations)"
                )
            }

        self.assertNotEqual(first_run, second_run)
        self.assertEqual(run_count, 2)
        self.assertEqual(reading_count, 2)
        self.assertNotIn("energy_kwh", observation_columns)

    def test_inventory_changes_compare_with_previous_complete_run(self) -> None:
        first_run = self.save_run("2026-11-16T12:00:00+00:00", ["inv-1", "inv-2"])
        changes = get_inventory_changes(self.database_path, first_run)
        self.assertFalse(changes["comparable"])

        second_run = self.save_run("2026-11-16T12:01:00+00:00", ["inv-1", "inv-3"])
        changes = get_inventory_changes(self.database_path, second_run)

        self.assertTrue(changes["comparable"])
        self.assertEqual(changes["baseline_run_id"], first_run)
        self.assertEqual(
            [inverter["inverter_id"] for inverter in changes["new_inverters"]],
            ["inv-3"],
        )
        self.assertEqual(
            [inverter["inverter_id"] for inverter in changes["missing_inverters"]],
            ["inv-2"],
        )

    def test_incomplete_run_is_not_compared_or_used_as_baseline(self) -> None:
        first_run = self.save_run("2026-11-16T12:00:00+00:00", ["inv-1", "inv-2"])
        incomplete_run = self.save_run(
            "2026-11-16T12:01:00+00:00",
            ["inv-1"],
            inventory_complete=False,
        )

        incomplete_changes = get_inventory_changes(
            self.database_path, incomplete_run
        )
        self.assertFalse(incomplete_changes["comparable"])

        next_run = self.save_run("2026-11-16T12:02:00+00:00", ["inv-1", "inv-3"])
        changes = get_inventory_changes(self.database_path, next_run)
        self.assertEqual(changes["baseline_run_id"], first_run)
        self.assertEqual(
            [inverter["inverter_id"] for inverter in changes["missing_inverters"]],
            ["inv-2"],
        )

    def test_nonproducing_channel_count_compares_with_previous_run(self) -> None:
        first_run = save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at="2026-11-16T12:00:00+00:00",
            inverters=[
                {
                    "ecu_id": "ecu-1",
                    "inverter_id": "inv-1",
                    "channel_energy_kwh": {"e1": 0, "e2": 1.0},
                }
            ],
        )
        second_run = save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at="2026-11-16T12:01:00+00:00",
            inverters=[
                {
                    "ecu_id": "ecu-1",
                    "inverter_id": "inv-1",
                    "channel_energy_kwh": {"e1": 0, "e2": 0},
                }
            ],
        )

        changes = get_nonproducing_channel_changes(
            self.database_path, second_run
        )

        self.assertTrue(changes["comparable"])
        self.assertEqual(changes["baseline_run_id"], first_run)
        self.assertEqual(changes["previous_count"], 1)
        self.assertEqual(changes["current_count"], 2)
        self.assertTrue(changes["changed"])
        self.assertEqual(changes["count_delta"], 1)

    def test_unchanged_nonproducing_channel_count_is_not_a_change(self) -> None:
        first_run = save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at="2026-11-16T12:00:00+00:00",
            inverters=[
                {
                    "ecu_id": "ecu-1",
                    "inverter_id": "inv-1",
                    "channel_energy_kwh": {"e1": 0, "e2": 1.0},
                }
            ],
        )
        second_run = save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at="2026-11-16T12:01:00+00:00",
            inverters=[
                {
                    "ecu_id": "ecu-1",
                    "inverter_id": "inv-1",
                    "channel_energy_kwh": {"e1": 1.0, "e2": 0},
                }
            ],
        )

        changes = get_nonproducing_channel_changes(
            self.database_path, second_run
        )

        self.assertEqual(changes["previous_count"], changes["current_count"])
        self.assertFalse(changes["changed"])

    def test_invalid_reading_rolls_back_the_entire_run(self) -> None:
        with self.assertRaises(ValueError):
            save_inverter_run(
                self.database_path,
                system_name="Test system",
                system_id="system-1",
                report_date="2026-11-16",
                run_at="2026-11-16T12:00:00+00:00",
                inverters=[
                    {
                        "ecu_id": "ecu-1",
                        "inverter_id": "inv-1",
                        "channel_energy_kwh": {"e1": float("nan")},
                    }
                ],
            )

        with sqlite3.connect(self.database_path) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM report_runs"
            ).fetchone()[0]
            observation_count = connection.execute(
                "SELECT COUNT(*) FROM inverter_observations"
            ).fetchone()[0]

        self.assertEqual(run_count, 0)
        self.assertEqual(observation_count, 0)

    def test_inverter_errors_are_stored_one_message_per_row(self) -> None:
        run_id = save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at="2026-11-16T12:00:00+00:00",
            inverters=[
                {
                    "ecu_id": "ecu-1",
                    "inverter_id": "inv-1",
                    "channel_energy_kwh": {},
                    "errors": ["first issue", "second issue"],
                    "error": "first issue; second issue",
                }
            ],
        )

        with sqlite3.connect(self.database_path) as connection:
            errors = connection.execute(
                """
                SELECT error_message
                FROM inverter_errors
                WHERE run_id = ?
                ORDER BY error_number
                """,
                (run_id,),
            ).fetchall()

        self.assertEqual(
            [error[0] for error in errors],
            ["first issue", "second issue"],
        )


if __name__ == "__main__":
    unittest.main()
