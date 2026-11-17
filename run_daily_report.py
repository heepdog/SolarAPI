#!/usr/bin/env python3
"""Fetch today's inverter readings, save them, and print their analysis."""

from __future__ import annotations

import argparse
from html import escape
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import sys
from typing import Any

from dotenv import load_dotenv

from solar_api import APsystemsClient, analyze_inverter_data, analyze_inverter_file
from solar_api.config import get_today
from solar_api.db import (
    get_inventory_changes,
    get_nonproducing_channel_changes,
    save_inverter_run,
)

import emailalert

PROJECT_DIR = Path(__file__).resolve().parent
CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE = 80
SYSTEM_OUTPUT_ALERT_THRESHOLD_KWH = 5
MAX_EMAIL_CHANNEL_DETAILS = 10
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


def format_alert_email(
    system_name: str,
    analysis: dict[str, Any],
    channel_changes: dict[str, Any],
    *,
    always_alert: bool = False,
) -> dict[str, str] | None:
    """Create concise plain-text and HTML alert bodies from report analysis."""
    low_channels = analysis["low_channels"]
    recovered = channel_changes.get("recovered", [])
    total_output = analysis.get("total_system_output_kwh", 0)
    low_system_output = total_output < SYSTEM_OUTPUT_ALERT_THRESHOLD_KWH
    if (
        not low_channels
        and not recovered
        and not low_system_output
        and not always_alert
    ):
        return None

    report_date = analysis.get("date") or "Unknown date"
    low_count = len(low_channels)
    if low_count:
        summary = (
            f"{low_count} channel{'s' if low_count != 1 else ''} below "
            f"{CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE}% of average"
        )
    else:
        summary = (
            f"No channels below "
            f"{CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE}% of average"
        )
    text_lines = [
        f"{system_name} solar report - {report_date}",
        f"{summary} | {total_output:.2f} kWh total",
    ]
    if low_system_output:
        text_lines.append(
            f"System output is below the {SYSTEM_OUTPUT_ALERT_THRESHOLD_KWH} kWh threshold."
        )
    html_rows = []
    ordered_low_channels = sorted(
        low_channels,
        key=lambda reading: (
            reading["percent_of_average"]
            if reading["percent_of_average"] is not None
            else float("inf")
        ),
    )
    for reading in ordered_low_channels:
        percent = reading["percent_of_average"]
        percent_text = f"{percent:.0f}%" if percent is not None else "n/a"
        text_lines.append(
            f"Inverter {reading['inverter_id']} / {reading['channel']}: "
            f"{percent_text} of average "
            f"({reading['energy_kwh']:.2f} / "
            f"{reading['channel_average_kwh']:.2f} kWh)"
        )
        row = (
            '<tr style="border-bottom:1px solid #e2e8f0">'
            + "".join(
                f'<td style="padding:7px;background-color:#ffffff;'
                f'color:#172033">{value}</td>'
                for value in (
                    escape(str(reading["inverter_id"])),
                    escape(str(reading["channel"])),
                    f"{reading['energy_kwh']:.2f} kWh",
                    f"{reading['channel_average_kwh']:.2f} kWh",
                    percent_text,
                )
            )
            + "</tr>"
        )
        html_rows.append(row)

    visible_rows = html_rows[:MAX_EMAIL_CHANNEL_DETAILS]
    hidden_rows = html_rows[MAX_EMAIL_CHANNEL_DETAILS:]

    if not low_channels:
        visible_rows.append(
            '<tr><td colspan="5">No channels below the alert threshold.</td></tr>'
        )

    table_header = (
        "<thead><tr>"
        + "".join(
            f'<th style="text-align:left;padding:7px;border-bottom:1px solid #cbd5e1;'
            f'background-color:#ffffff;color:#172033">{heading}</th>'
            for heading in ("Inverter", "Channel", "Energy", "Average", "Of avg.")
        )
        + "</tr></thead>"
    )
    channel_table = (
        '<table style="border-collapse:collapse;width:100%;'
        'background-color:#ffffff;color:#172033">'
        + table_header
        + "<tbody>"
        + "".join(visible_rows)
        + "</tbody></table>"
    )
    if hidden_rows:
        channel_table += (
            f'<details style="margin-top:8px"><summary style="cursor:pointer;'
            f'color:#1d4ed8">Show {len(hidden_rows)} more channels</summary>'
            '<table style="border-collapse:collapse;width:100%;margin-top:6px;'
            'background-color:#ffffff;color:#172033">'
            + table_header
            + "<tbody>"
            + "".join(hidden_rows)
            + "</tbody></table></details>"
        )

    def format_channel_names(readings: list[dict[str, Any]]) -> str:
        names = [
            f"{reading['inverter_id']}/{reading['channel']}"
            for reading in readings[:MAX_EMAIL_CHANNEL_DETAILS]
        ]
        if len(readings) > MAX_EMAIL_CHANNEL_DETAILS:
            names.append(f"+{len(readings) - MAX_EMAIL_CHANNEL_DETAILS} more")
        return ", ".join(names)

    change_lines = []
    if channel_changes.get("comparable"):
        previous_date = channel_changes["previous_report_date"]
        newly_low = channel_changes.get("newly_below_threshold", [])
        if newly_low:
            change_lines.append(
                f"New since {previous_date}: "
                + format_channel_names(newly_low)
            )
        if recovered:
            change_lines.append(
                f"Recovered since {previous_date}: "
                + format_channel_names(recovered)
            )
        if not newly_low and not recovered and low_channels:
            change_lines.append(f"No change since {previous_date}.")
    elif low_channels or always_alert:
        change_lines.append("Previous-day comparison unavailable.")

    text_lines.extend(change_lines)
    escaped_changes = "".join(
        f"<p class=\"muted\" style=\"margin:4px 0;color:#475569\">{escape(line)}</p>"
        for line in change_lines
    )
    system_output_notice = (
        f'<p class="system-output-notice" style="color:#b91c1c"><strong>System output is below '
        f"{SYSTEM_OUTPUT_ALERT_THRESHOLD_KWH} kWh.</strong></p>"
        if low_system_output
        else ""
    )
    html_section = (
        '<section class="solar-email" style="font-family:Arial,sans-serif;'
        'background-color:#ffffff;color:#172033;line-height:1.45;'
        'border:1px solid #e2e8f0;border-radius:8px;padding:20px;'
        'max-width:900px;margin:0 auto 24px">'
        f"<h2 style=\"margin-bottom:4px\">{escape(system_name)} solar report</h2>"
        f"<p class=\"muted\" style=\"margin-top:0;color:#475569\">{escape(report_date)}"
        f" &middot; {low_count} low-output channel"
        f"{'s' if low_count != 1 else ''}"
        f" &middot; {total_output:.2f} kWh total</p>"
        + system_output_notice
        + f"<p><strong>{CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE}% of channel "
        + "average threshold</strong></p>"
        + channel_table
        + escaped_changes
        + "</section>"
    )
    html_document = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        "<style>@media (prefers-color-scheme: dark) {"
        "body { background-color:#0f172a !important; color:#f8fafc !important; }"
        ".solar-email { background-color:#111827 !important; color:#f8fafc !important; "
        "border-color:#374151 !important; }"
        ".solar-email .muted { color:#cbd5e1 !important; }"
        ".solar-email table, .solar-email th, .solar-email td { "
        "background-color:#1f2937 !important; color:#f8fafc !important; "
        "border-color:#475569 !important; }"
        ".solar-email summary { color:#93c5fd !important; }"
        ".solar-email .system-output-notice { color:#fca5a5 !important; }"
        "}</style></head>"
        '<body style="margin:0;padding:16px;background-color:#f1f5f9;'
        'color:#172033;font-family:Arial,sans-serif">'
        + html_section
        + "</body></html>"
    )
    return {"text": "\n".join(text_lines), "html": html_document}


def main(argv: list[str] | None = None) -> int:
    """Fetch, save, analyze, and optionally always email today's report."""
    parser = argparse.ArgumentParser(
        description="Fetch and analyze today's APsystems inverter report."
    )
    parser.add_argument(
        "--always-alert",
        action="store_true",
        help=(
            "send an email for each successful run, even when no channels "
            "are below 80%% of their channel average"
        ),
    )
    parser.add_argument(
        "--preview-email",
        type=Path,
        metavar="JSON_FILE",
        help="render an email preview from a saved report without calling the API or sending email",
    )
    parser.add_argument(
        "--preview-system-name",
        default="Solar system",
        help="system name to show in --preview-email output",
    )
    args = parser.parse_args(argv)

    if args.preview_email:
        try:
            analysis = analyze_inverter_file(
                args.preview_email,
                below_average_percent=(
                    100 - CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE
                ),
            )
            preview = format_alert_email(
                args.preview_system_name,
                analysis,
                {"comparable": False, "recovered": []},
                always_alert=True,
            )
            if preview is None:
                raise RuntimeError("Could not build email preview")
            print(preview["html"])
            return 0
        except (OSError, RuntimeError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1

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
                database_path,
                run_id,
                minimum_percent_of_average=CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE,
            )
            run_details["nonproducing_channel_changes"] = channel_changes

            below_average_percent = (
                100 - CHANNEL_ALERT_THRESHOLD_PERCENT_OF_AVERAGE
            )
            analysis = analyze_inverter_data(
                results, below_average_percent=below_average_percent
            )

            output["systems"][name] = {
                **run_details,
                "analysis": analysis,
                "inverter_errors": [
                    inverter for inverter in results if "error" in inverter
                ],
            }
            if analysis["inverter_errors"]:
                failed = True
            alert = format_alert_email(
                name,
                analysis,
                channel_changes,
                always_alert=args.always_alert,
            )
            if alert:
                alert_sections.append(alert)
                alert_systems.append(name)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            output["systems"][name] = {**run_details, "error": str(error)}
            failed = True

    if alert_sections:
        plain_body = "\n\n".join(alert["text"] for alert in alert_sections)
        html_body = (
            "<!doctype html><html><body style=\"font-family:Arial,sans-serif\">"
            + "".join(alert["html"] for alert in alert_sections)
            + "</body></html>"
        )
        emailalert.sendalert(
            content=plain_body,
            html_content=html_body,
            message="",
            subject=f"Solar Details: {', '.join(alert_systems)} {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        )

    print(json.dumps(output, indent=2))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
