"""Build user-facing energy reports from APsystems API responses."""

from __future__ import annotations

import json
import math
import statistics
import ast
from pathlib import Path
from typing import Callable

from .config import get_today

ApiRequest = Callable[[str, dict[str, str] | None], dict]


def analyze_inverter_data(
    report: list[dict] | dict,
    below_average_percent: float = 80,
) -> dict:
    """Compare in-memory inverter results against the average for each channel.

    Accepts the list returned by ``APsystemsClient.get_inverter_energy()`` or
    the report dictionary returned when ``raw_response=True``.
    """
    if not 0 <= below_average_percent <= 100:
        raise ValueError("below_average_percent must be between 0 and 100")
    if isinstance(report, list):
        inverters = report
        report_date = (
            report[0].get("date")
            if report and isinstance(report[0], dict)
            else None
        )
    elif isinstance(report, dict) and isinstance(report.get("inverters"), list):
        inverters = report["inverters"]
        report_date = report.get("date")
    else:
        raise ValueError("Expected inverter results or a report containing inverters")

    channel_values: dict[str, list[tuple[dict, float]]] = {}
    skipped_channels = 0
    for inverter in inverters:
        if not isinstance(inverter, dict):
            continue
        values = inverter.get("channel_energy_kwh")
        if not isinstance(values, dict):
            continue
        for channel, raw_value in values.items():
            try:
                value = float(raw_value)
                if not math.isfinite(value) or value < 0:
                    raise ValueError("not a finite non-negative value")
            except (TypeError, ValueError):
                skipped_channels += 1
                continue
            channel_values.setdefault(channel, []).append((inverter, value))

    if not channel_values:
        raise ValueError("No valid channel-energy readings found in inverter results")

    total_system_output_kwh = sum(
        value
        for readings in channel_values.values()
        for _, value in readings
    )
    cutoff_ratio = 1 - below_average_percent / 100
    channel_stats = {}
    low_readings = []
    for channel, readings in sorted(channel_values.items()):
        average = statistics.mean(value for _, value in readings)
        cutoff = average * cutoff_ratio
        flagged_count = 0
        for inverter, value in readings:
            if value < cutoff:
                flagged_count += 1
                low_readings.append(
                    {
                        "ecu_id": inverter.get("ecu_id"),
                        "inverter_id": inverter.get("inverter_id"),
                        "channel": channel,
                        "energy_kwh": value,
                        "channel_average_kwh": average,
                        "percent_of_average": (
                            value / average * 100 if average else None
                        ),
                    }
                )
        channel_stats[channel] = {
            "average_kwh": average,
            "readings_compared": len(readings),
            "below_cutoff_count": flagged_count,
        }

    return {
        "date": report_date,
        "total_system_output_kwh": total_system_output_kwh,
        "comparison": (
            f"Flag channel readings more than {below_average_percent:g}% "
            "below the average for that same channel"
        ),
        "channels": channel_stats,
        "low_channels": low_readings,
        "inverter_error_count": len(low_readings),
        "inverter_errors": [
            (
                f"Inverter {reading['inverter_id']}, channel "
                f"{reading['channel']}: "
                f"{reading['percent_of_average']:.1f}% of average"
            )
            for reading in sorted(
                low_readings, key=lambda reading: int(reading["inverter_id"])
            )
        ],
        "skipped_invalid_channel_readings": skipped_channels,
        "note": (
            "A low daily reading is a screening signal, not proof of a faulty "
            "panel. Shading, orientation, weather, and incomplete-day data can "
            "affect comparisons. Channel IDs may not map one-to-one to physical "
            "panels in every installation."
        ),
    }


def analyze_inverter_file(path: Path, below_average_percent: float = 80) -> dict:
    """Load a saved inverter report and analyze it using ``analyze_inverter_data``."""
    contents = path.read_text(encoding="utf-8")
    try:
        report = json.loads(contents)
    except json.JSONDecodeError:
        # Older scripts saved ``str(results)``; accept that literal format safely.
        try:
            report = ast.literal_eval(contents)
        except (SyntaxError, ValueError) as error:
            raise ValueError(
                f"{path} is neither JSON nor a Python literal inverter report"
            ) from error
    try:
        return analyze_inverter_data(report, below_average_percent)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def get_system_energy(
    system_id: str,
    request: ApiRequest,
    raw_json: bool = False,
    *,
    today: str | None = None,
) -> dict:
    """Fetch and summarize hourly system production for today."""
    today = today or get_today()
    result = request(
        f"/user/api/v2/systems/energy/{system_id}",
        {"energy_level": "hourly", "date_range": today},
    )
    if raw_json:
        return result

    values = result.get("data")
    if not isinstance(values, list) or len(values) != 24:
        count = len(values) if isinstance(values, list) else "not an array"
        raise RuntimeError(f"Expected 24 hourly values; got {count}")
    try:
        total = sum(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Hourly energy values must be numeric") from error
    return {"date": today, "energy_kwh": total}


def _read_ecu_energy(
    system_id: str, ecu_id: str, today: str, request: ApiRequest
) -> tuple[dict | None, list[str], dict[str, dict[str, float]], dict[str, list[str]]]:
    """Fetch one ECU batch and group valid energy records by inverter/channel."""
    try:
        response = request(
            f"/user/api/v2/systems/{system_id}/devices/inverter/"
            f"batch/energy/{ecu_id}",
            {"energy_level": "energy", "date_range": today},
        )
    except RuntimeError as error:
        return None, [str(error)], {}, {}

    data = response.get("data")
    records = data.get("energy") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return (
            response,
            ["batch energy response does not contain an energy list"],
            {},
            {},
        )

    grouped: dict[str, dict[str, float]] = {}
    errors: dict[str, list[str]] = {}
    unassigned_errors = []
    for record in records:
        if not isinstance(record, str):
            unassigned_errors.append(f"Invalid batch energy record: {record!r}")
            continue
        uid, separator, remainder = record.partition("-")
        channel_text, separator2, value_text = remainder.partition("-")
        if not uid or not separator or not separator2:
            unassigned_errors.append(f"Invalid batch energy record: {record!r}")
            continue
        try:
            channel_number = int(channel_text)
            if not 1 <= channel_number <= 4:
                raise ValueError("channel must be between 1 and 4")
            value = float(value_text)
            if not math.isfinite(value):
                raise ValueError("energy must be a finite number")
        except ValueError as error:
            errors.setdefault(uid, []).append(
                f"Invalid energy record {record!r}: {error}"
            )
            continue
        grouped.setdefault(uid, {})[f"e{channel_number}"] = value
    return response, unassigned_errors, grouped, errors


def get_inverter_energy(
    system_id: str,
    request: ApiRequest,
    ecu_id_filter: str | None = None,
    raw_json: bool = False,
    *,
    today: str | None = None,
) -> list | dict:
    """List inverter production today, using one batch-energy call per ECU."""
    devices_result = request(f"/user/api/v2/systems/inverters/{system_id}", None)
    devices = devices_result.get("data")
    if not isinstance(devices, list):
        raise RuntimeError("APsystems inverter response data is not a list")
    if any(
        not isinstance(device, dict)
        or not isinstance(device.get("inverter"), list)
        for device in devices
    ):
        raise RuntimeError("APsystems returned an invalid ECU/inverter list")
    if ecu_id_filter:
        devices = [device for device in devices if device.get("eid") == ecu_id_filter]
        if not devices:
            raise RuntimeError(
                f"No ECU matching {ecu_id_filter} was found in the system"
            )

    today = today or get_today()
    results = []
    raw_ecu_responses = []
    for device in devices:
        ecu_id = device.get("eid")
        if not isinstance(ecu_id, str) or not ecu_id:
            raise RuntimeError("APsystems returned an ECU without a valid ID")

        response, unassigned_errors, grouped, record_errors = _read_ecu_energy(
            system_id, ecu_id, today, request
        )
        batch_error = (
            unassigned_errors[0]
            if unassigned_errors and response is None
            else None
        )
        if raw_json:
            raw_ecu_responses.append(
                {
                    "ecu_id": ecu_id,
                    "response": response,
                    **({"error": batch_error} if batch_error else {}),
                }
            )

        for inverter in device["inverter"]:
            uid = inverter.get("uid")
            if not isinstance(uid, str) or not uid:
                raise RuntimeError(
                    f"APsystems returned an invalid inverter entry for ECU {ecu_id}"
                )
            channels = grouped.get(uid, {})
            errors = list(record_errors.get(uid, []))
            if response is None:
                errors.extend(unassigned_errors)
            elif unassigned_errors and not batch_error:
                errors.extend(unassigned_errors)
            if not channels and not errors:
                errors.append("No energy records were returned for inverter")
            item = {
                "ecu_id": ecu_id,
                "inverter_id": uid,
                "type": inverter.get("type"),
                "date": today,
                "energy_kwh": sum(channels.values()) if channels and not errors else None,
                "channel_energy_kwh": channels,
            }
            if errors:
                item["errors"] = errors
                item["error"] = "; ".join(errors)
            results.append(item)

    if raw_json:
        return {
            "date": today,
            "api_calls": 1 + len(devices),
            "ecus": raw_ecu_responses,
            "inverters": results,
        }
    return results


def check_system(
    system_id: str, request: ApiRequest, raw_json: bool = False
) -> dict | str:
    """Confirm the API can retrieve details for the configured system ID."""
    result = request(f"/user/api/v2/systems/details/{system_id}", None)
    if raw_json:
        return result
    data = result.get("data")
    if not isinstance(data, dict) or data.get("sid") != system_id:
        raise RuntimeError(
            "System-details response did not match the configured system ID"
        )
    return "System-details request succeeded for the configured system ID."
