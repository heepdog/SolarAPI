#!/usr/bin/env python3
"""Fetch today's inverter readings, save them, and print their analysis."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from solar_api import APsystemsClient, analyze_inverter_data
from solar_api.config import get_today
from solar_api.db import (
    get_inventory_changes,
    get_nonproducing_channel_changes,
    save_inverter_run,
)

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

    timezone_name = get_setting("APPSYSTEMS_TIMEZONE", "APSYSTEMS_TIMEZONE")
    database_path = Path(
        get_setting("APPSYSTEMS_DB_PATH")
        or str(PROJECT_DIR / "solar_reports.sqlite3")
    ).expanduser()
    if not database_path.is_absolute():
        database_path = PROJECT_DIR / database_path

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

        run_details: dict[str, Any] = {}
        try:
            client = APsystemsClient(
                app_id=app_id,
                app_secret=app_secret,
                system_id=system_id,
                timezone=timezone_name,
            )
            results = client.get_inverter_energy()
            run_timestamp = datetime.now(timezone.utc)
            run_at = run_timestamp.isoformat(timespec="microseconds")
            report_path = (
                PROJECT_DIR
                / (
                    f"inverterdata_{name.lower()}_{get_today(timezone_name)}_"
                    f"{run_timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.json"
                )
            )
            with report_path.open("w", encoding="utf-8") as report_file:
                json.dump(results, report_file, indent=2)
                report_file.write("\n")
            run_details = {"report_file": str(report_path)}

            report_date = (
                results[0].get("date")
                if results
                else get_today(timezone_name)
            )
            run_id = save_inverter_run(
                database_path,
                system_name=name,
                system_id=system_id,
                report_date=report_date,
                inverters=results,
                run_at=run_at,
            )
            run_details.update(
                {
                    "database_path": str(database_path),
                    "database_run_id": run_id,
                }
            )
            run_details["inventory_changes"] = get_inventory_changes(
                database_path, run_id
            )
            channel_changes = get_nonproducing_channel_changes(
                database_path, run_id
            )
            run_details["nonproducing_channel_changes"] = channel_changes

            analysis = analyze_inverter_data(results)
            output["systems"][name] = {
                **run_details,
                "analysis": analysis,
                "inverter_errors": [
                    inverter for inverter in results if "error" in inverter
                ],
            }
            if analysis["inverter_errors"]:
                failed = True
            if channel_changes["comparable"] and channel_changes["changed"]:
                alert_sections.append(
                    "\n".join(
                        [
                            f"{name} Solar Details",
                            "Channels not delivering energy changed from "
                            f"{channel_changes['previous_count']} to "
                            f"{channel_changes['current_count']} "
                            f"({channel_changes['count_delta']:+d}).",
                        ]
                    )
                )
                alert_systems.append(name)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            output["systems"][name] = {**run_details, "error": str(error)}
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
