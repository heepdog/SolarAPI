#!/usr/bin/env python3
"""Fetch today's inverter readings, save them, and print their analysis."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from solar_api import APsystemsClient, analyze_inverter_file
from solar_api.config import get_today

import emailalert

PROJECT_DIR = Path(__file__).resolve().parent
SYSTEM_SETTING_SUFFIXES = (
    "_APSYSTEMS_APP_ID",
    "_APSYSTEMS_APP_SECRET",
    "_APSYSTEMS_SYSTEM_ID",
)


def get_setting(*names: str) -> str | None:
    """Return the first non-empty value among compatible environment names."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def get_system_settings() -> list[tuple[str, str | None, str | None, str | None]]:
    """Return credentials for each named system, or the legacy single system."""
    prefixes = sorted(
        {
            name.removesuffix(suffix)
            for name in os.environ
            for suffix in SYSTEM_SETTING_SUFFIXES
            if name.endswith(suffix)
        }
    )
    if prefixes:
        return [
            (
                prefix,
                get_setting(f"{prefix}_APSYSTEMS_APP_ID"),
                get_setting(f"{prefix}_APSYSTEMS_APP_SECRET"),
                get_setting(f"{prefix}_APSYSTEMS_SYSTEM_ID"),
            )
            for prefix in prefixes
        ]
    return [
        (
            "APsystems",
            get_setting("APPSYSTEMS_APP_ID", "APSYSTEMS_APP_ID"),
            get_setting("APPSYSTEMS_APP_SECRET", "APSYSTEMS_APP_SECRET"),
            get_setting("APPSYSTEMS_SYSTEM_ID", "APSYSTEMS_SYSTEM_ID"),
        )
    ]


def main() -> int:
    """Fetch, save, and analyze today's inverter report for each configured system."""
    load_dotenv(PROJECT_DIR / ".env", override=False)

    timezone = get_setting("APPSYSTEMS_TIMEZONE", "APSYSTEMS_TIMEZONE")
    output: dict[str, Any] = {"systems": {}}
    alert_sections = []
    alert_systems = []
    failed = False

    for name, app_id, app_secret, system_id in get_system_settings():
        missing_fields = [
            field
            for field, value in (
                ("APP_ID", app_id),
                ("APP_SECRET", app_secret),
                ("SYSTEM_ID", system_id),
            )
            if value is None
        ]
        if missing_fields:
            output["systems"][name] = {
                "error": (
                    f"Missing required settings for {name}: "
                    + ", ".join(f"{name}_APSYSTEMS_{field}" for field in missing_fields)
                )
            }
            failed = True
            continue

        try:
            client = APsystemsClient(
                app_id=app_id,
                app_secret=app_secret,
                system_id=system_id,
                timezone=timezone,
            )
            results = client.get_inverter_energy()
            report_path = (
                PROJECT_DIR
                / f"inverterdata_{name.lower()}_{get_today(timezone)}.json"
            )
            with report_path.open("w", encoding="utf-8") as report_file:
                json.dump(results, report_file, indent=2)
                report_file.write("\n")

            analysis = analyze_inverter_file(report_path)
            output["systems"][name] = {
                "report_file": str(report_path),
                "analysis": analysis,
                "inverter_errors": [
                    inverter for inverter in results if "error" in inverter
                ],
            }
            if analysis["inverter_errors"]:
                alert_sections.append(
                    "\n".join(
                        [
                            f"{name} Solar Details",
                            f"Total system output today: "
                            f"{analysis['total_system_output_kwh']:.2f} kWh",
                            f"Inverters with low output today ({analysis['inverter_error_count']}):",
                            *analysis["inverter_errors"],
                        ]
                    )
                )
                alert_systems.append(name)
                failed = True
        except (OSError, RuntimeError, ValueError) as error:
            output["systems"][name] = {"error": str(error)}
            failed = True

    if alert_sections:
        emailalert.sendalert(
            content="\n\n".join(alert_sections),
            message="",
            subject=f"Solar Details: {', '.join(alert_systems)}",
        )

    print(json.dumps(output, indent=2))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
