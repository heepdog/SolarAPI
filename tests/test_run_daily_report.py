from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
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
        *,
        always_alert: bool = False,
    ):
        if previous_readings:
            save_inverter_run(
                self.database_path,
                system_name="Test system",
                system_id="system-1",
                report_date="2026-11-15",
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
            arguments = ["--always-alert"] if always_alert else []
            run_daily_report.main(arguments)

        return sendalert

    def test_email_is_sent_for_channels_below_80_percent_of_average(self) -> None:
        previous = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": inverter_id,
                "channel_energy_kwh": {"e1": 1.0},
            }
            for inverter_id in ("1", "2", "3")
        ]
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 1.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 1.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "3",
                "channel_energy_kwh": {"e1": 0.5},
            },
        ]

        sendalert = self.run_report(previous, current)

        sendalert.assert_called_once()
        self.assertIn("below 80%", sendalert.call_args.kwargs["content"])
        self.assertIn("New since 2026-11-15: 3/e1", sendalert.call_args.kwargs["content"])
        self.assertIn(
            "Inverter 3 / e1: 60% of average",
            sendalert.call_args.kwargs["content"],
        )
        self.assertIn("<table", sendalert.call_args.kwargs["html_content"])

    def test_email_reports_channels_recovered_since_previous_day(self) -> None:
        previous = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 1.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 1.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "3",
                "channel_energy_kwh": {"e1": 0.5},
            },
        ]
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": inverter_id,
                "channel_energy_kwh": {"e1": 1.0},
            }
            for inverter_id in ("1", "2", "3")
        ]

        sendalert = self.run_report(previous, current)

        sendalert.assert_called_once()
        self.assertIn(
            "Recovered since 2026-11-15: 3/e1",
            sendalert.call_args.kwargs["content"],
        )

    def test_channel_at_80_percent_of_average_does_not_trigger_email(self) -> None:
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 2.0},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 3.0},
            },
        ]

        sendalert = self.run_report([], current)

        sendalert.assert_not_called()

    def test_low_system_output_triggers_email(self) -> None:
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 2.49},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 2.49},
            },
        ]

        sendalert = self.run_report([], current)

        sendalert.assert_called_once()
        self.assertIn("4.98 kWh total", sendalert.call_args.kwargs["content"])
        self.assertIn(
            "System output is below the 5 kWh threshold",
            sendalert.call_args.kwargs["content"],
        )
        self.assertIn(
            "System output is below",
            sendalert.call_args.kwargs["html_content"],
        )

    def test_system_output_at_threshold_does_not_trigger_email(self) -> None:
        current = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 2.5},
            },
            {
                "ecu_id": "ecu-1",
                "inverter_id": "2",
                "channel_energy_kwh": {"e1": 2.5},
            },
        ]

        sendalert = self.run_report([], current)

        sendalert.assert_not_called()

    def test_always_alert_sends_email_when_no_channels_are_below_threshold(self) -> None:
        readings = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 0, "e2": 1.0},
            }
        ]

        sendalert = self.run_report(readings, readings, always_alert=True)

        sendalert.assert_called_once()
        self.assertIn(
            "No channels below 80%",
            sendalert.call_args.kwargs["content"],
        )

    def test_always_alert_does_not_require_a_previous_run(self) -> None:
        readings = [
            {
                "ecu_id": "ecu-1",
                "inverter_id": "1",
                "channel_energy_kwh": {"e1": 0, "e2": 1.0},
            }
        ]

        sendalert = self.run_report([], readings, always_alert=True)

        sendalert.assert_called_once()
        self.assertIn(
            "No channels below 80%",
            sendalert.call_args.kwargs["content"],
        )
        self.assertIn(
            "Previous-day comparison unavailable",
            sendalert.call_args.kwargs["content"],
        )

    def test_email_preview_uses_saved_report_without_api_or_sending_email(self) -> None:
        report_path = self.project_directory / "inverters.json"
        report_path.write_text(
            json.dumps(
                [
                    {
                        "ecu_id": "ecu-1",
                        "inverter_id": "1",
                        "date": "2026-11-16",
                        "channel_energy_kwh": {"e1": 1.0},
                    },
                    {
                        "ecu_id": "ecu-1",
                        "inverter_id": "2",
                        "date": "2026-11-16",
                        "channel_energy_kwh": {"e1": 1.0},
                    },
                    {
                        "ecu_id": "ecu-1",
                        "inverter_id": "3",
                        "date": "2026-11-16",
                        "channel_energy_kwh": {"e1": 0.5},
                    },
                ]
            ),
            encoding="utf-8",
        )
        output = StringIO()
        with (
            patch.object(run_daily_report, "APsystemsClient") as client_class,
            patch.object(run_daily_report.emailalert, "sendalert") as sendalert,
            redirect_stdout(output),
        ):
            result = run_daily_report.main(
                ["--preview-email", str(report_path), "--preview-system-name", "Test"]
            )

        self.assertEqual(result, 0)
        client_class.assert_not_called()
        sendalert.assert_not_called()
        self.assertIn("<table", output.getvalue())
        self.assertIn("60%</td>", output.getvalue())

    def test_email_collapses_extra_channel_rows_and_keeps_plain_text_complete(self) -> None:
        analysis = {
            "date": "2026-11-16",
            "total_system_output_kwh": 12.0,
            "low_channels": [
                {
                    "inverter_id": str(index),
                    "channel": "e1",
                    "energy_kwh": 0.1,
                    "channel_average_kwh": 1.0,
                    "percent_of_average": float(index),
                }
                for index in range(1, 13)
            ],
        }

        alert = run_daily_report.format_alert_email(
            "Test system",
            analysis,
            {"comparable": False, "recovered": []},
        )

        self.assertIsNotNone(alert)
        self.assertIn("12 channels below 80%", alert["text"])
        self.assertIn("Inverter 11 / e1", alert["text"])
        self.assertIn("Inverter 12 / e1", alert["text"])
        self.assertIn("<details", alert["html"])
        self.assertIn("<summary", alert["html"])
        self.assertIn("Show 2 more channels", alert["html"])
        self.assertIn('<meta name="color-scheme" content="light dark">', alert["html"])
        self.assertIn("@media (prefers-color-scheme: dark)", alert["html"])
        self.assertIn("background-color:#ffffff", alert["html"])
        self.assertIn("background-color:#111827", alert["html"])
        self.assertIn(">11</td>", alert["html"])
        self.assertIn(">12</td>", alert["html"])


if __name__ == "__main__":
    unittest.main()
