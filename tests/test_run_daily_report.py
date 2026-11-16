from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_daily_report
from solar_api.db import save_inverter_run


class DailyReportAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_directory = Path(self.temporary_directory.name)
        self.database_path = self.project_directory / "reports.sqlite3"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_report(
        self,
        previous_readings: list[dict],
        current_readings: list[dict],
    ):
        save_inverter_run(
            self.database_path,
            system_name="Test system",
            system_id="system-1",
            report_date="2026-11-16",
            run_at=(
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat(timespec="microseconds"),
            inverters=previous_readings,
        )
        current_results = [
            {
                **reading,
                "date": "2026-11-16",
                "energy_kwh": sum(reading["channel_energy_kwh"].values()),
            }
            for reading in current_readings
        ]
        with (
            patch.object(run_daily_report, "PROJECT_DIR", self.project_directory),
            patch.object(
                run_daily_report,
                "get_system_settings",
                return_value=[("Test system", "app", "secret", "system-1")],
            ),
            patch.object(run_daily_report, "APsystemsClient") as client_class,
            patch.object(run_daily_report.emailalert, "sendalert") as sendalert,
            patch.dict(os.environ, {"APPSYSTEMS_DB_PATH": str(self.database_path)}),
            redirect_stdout(StringIO()),
        ):
            client_class.return_value.get_inverter_energy.return_value = current_results
            run_daily_report.main()

        return sendalert

    def test_email_is_sent_when_nonproducing_channel_count_changes(self) -> None:
        previous = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 0, "e2": 1.0},
            }
        ]
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 0, "e2": 0},
            }
        ]

        sendalert = self.run_report(previous, current)

        sendalert.assert_called_once()
        self.assertIn("changed from 1 to 2", sendalert.call_args.kwargs["content"])

    def test_low_output_alone_does_not_trigger_email(self) -> None:
        previous = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 0.01, "e2": 1.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 1.0, "e2": 1.0},
            },
        ]
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 0.01, "e2": 1.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 1.0, "e2": 1.0},
            },
        ]

        sendalert = self.run_report(previous, current)

        sendalert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
