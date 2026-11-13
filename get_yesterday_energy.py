#!/usr/bin/env python3
"""Fetch APsystems energy, check a system, or report today's inverter energy."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BASE_URL = "https://api.apsystemsema.com:9282"
SIGNATURE_METHOD = "HmacSHA256"


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"Invalid entry in {path} on line {line_number}")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def get_today() -> str:
    timezone = os.environ.get("APPSYSTEMS_TIMEZONE")
    if timezone:
        try:
            today = datetime.now(ZoneInfo(timezone)).date()
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown APPSYSTEMS_TIMEZONE: {timezone}") from error
    else:
        today = datetime.now().astimezone().date()
    return today.isoformat()


def request_api(path: str, query: dict[str, str] | None = None) -> dict:
    app_id = os.environ["APPSYSTEMS_APP_ID"]
    app_secret = os.environ["APPSYSTEMS_APP_SECRET"]
    timestamp = str(int(datetime.now().timestamp() * 1000))
    nonce = uuid.uuid4().hex
    signature_path = path.rsplit("/", 1)[-1]
    string_to_sign = (
        f"{timestamp}/{nonce}/{app_id}/{signature_path}/GET/{SIGNATURE_METHOD}"
    )
    signature = base64.b64encode(
        hmac.new(
            app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")

    url = os.environ.get("APPSYSTEMS_BASE_URL", BASE_URL).rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={
            "X-CA-AppId": app_id,
            "X-CA-Timestamp": timestamp,
            "X-CA-Nonce": nonce,
            "X-CA-Signature-Method": SIGNATURE_METHOD,
            "X-CA-Signature": signature,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"APsystems HTTP error {error.code}: {details}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach APsystems API: {error.reason}") from error

    try:
        result = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("APsystems returned invalid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError("APsystems response is not a JSON object")
    if result.get("code") != 0:
        raise RuntimeError(f"APsystems API returned: {json.dumps(result)}")
    return result


def analyze_inverter_file(path: Path, below_average_percent: float) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} does not contain valid JSON") from error
    if not isinstance(report, dict) or not isinstance(report.get("inverters"), list):
        raise ValueError(f"{path} does not contain an inverter report")

    channel_values: dict[str, list[tuple[dict, float]]] = {}
    skipped_channels = 0
    for inverter in report["inverters"]:
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
        raise ValueError(f"No valid channel-energy readings found in {path}")

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
        "date": report.get("date"),
        "comparison": (
            f"Flag channel readings more than {below_average_percent:g}% "
            "below the average for that same channel"
        ),
        "channels": channel_stats,
        "low_channels": low_readings,
        "skipped_invalid_channel_readings": skipped_channels,
        "note": (
            "A low daily reading is a screening signal, not proof of a faulty "
            "panel. Shading, orientation, weather, and incomplete-day data can "
            "affect comparisons. Channel IDs may not map one-to-one to physical "
            "panels in every installation."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--check-system",
        action="store_true",
        help="check the configured system ID using the system-details endpoint",
    )
    action_group.add_argument(
        "--inverters",
        action="store_true",
        help="show today's energy for each inverter in the configured system",
    )
    action_group.add_argument(
        "--analyze-file",
        type=Path,
        metavar="JSON_FILE",
        help="analyze a saved --inverters --raw-json report without API calls",
    )
    parser.add_argument(
        "--below-average-percent",
        type=float,
        default=80,
        metavar="PERCENT",
        help="flag channels more than this percent below their channel average (default: 80)",
    )
    parser.add_argument(
        "--ecu-id",
        help="limit --inverters output to this ECU ID",
    )
    parser.add_argument(
        "--raw-json",
        action="store_true",
        help="print the full API response instead of the energy summary",
    )
    args = parser.parse_args()
    if args.ecu_id and not args.inverters:
        parser.error("--ecu-id requires --inverters")
    if args.analyze_file and not 0 <= args.below_average_percent <= 100:
        parser.error("--below-average-percent must be between 0 and 100")

    script_dir = Path(__file__).resolve().parent
    env_file = Path(
        os.environ.get("APPSYSTEMS_ENV_FILE", str(script_dir / ".env"))
    ).expanduser()
    try:
        load_env_file(env_file)
        if args.analyze_file:
            analysis = analyze_inverter_file(
                args.analyze_file, args.below_average_percent
            )
            print(json.dumps(analysis, indent=2))
            return 0

        for name in (
            "APPSYSTEMS_APP_ID",
            "APPSYSTEMS_APP_SECRET",
            "APPSYSTEMS_SYSTEM_ID",
        ):
            if not os.environ.get(name):
                raise ValueError(f"Set {name} in {env_file} or the environment")

        system_id = os.environ["APPSYSTEMS_SYSTEM_ID"]
        if args.check_system:
            result = request_api(f"/user/api/v2/systems/details/{system_id}")
            if args.raw_json:
                print(json.dumps(result, indent=2))
                return 0
            data = result.get("data")
            if not isinstance(data, dict) or data.get("sid") != system_id:
                raise RuntimeError(
                    "System-details response did not match the configured system ID"
                )
            print("System-details request succeeded for the configured system ID.")
            return 0

        if args.inverters:
            devices_result = request_api(
                f"/user/api/v2/systems/inverters/{system_id}"
            )
            devices = devices_result.get("data")
            if not isinstance(devices, list):
                raise RuntimeError("APsystems inverter response data is not a list")
            if any(
                not isinstance(device, dict)
                or not isinstance(device.get("inverter"), list)
                for device in devices
            ):
                raise RuntimeError("APsystems returned an invalid ECU/inverter list")
            if args.ecu_id:
                devices = [
                    device for device in devices if device.get("eid") == args.ecu_id
                ]
                if not devices:
                    raise RuntimeError(
                        f"No ECU matching {args.ecu_id} was found in the system"
                    )
            today = get_today()
            inverter_results = []
            raw_ecu_responses = []
            for device in devices:
                ecu_id = device.get("eid")
                if not isinstance(ecu_id, str) or not ecu_id:
                    raise RuntimeError("APsystems returned an ECU without a valid ID")
                ecu_inverters = device["inverter"]
                batch_response = None
                batch_error = None
                try:
                    batch_response = request_api(
                        f"/user/api/v2/systems/{system_id}/devices/inverter/"
                        f"batch/energy/{ecu_id}",
                        {"energy_level": "energy", "date_range": today},
                    )
                    batch_data = batch_response.get("data")
                    energy_records = (
                        batch_data.get("energy")
                        if isinstance(batch_data, dict)
                        else None
                    )
                    if not isinstance(energy_records, list):
                        raise RuntimeError(
                            "batch energy response does not contain an energy list"
                        )
                except RuntimeError as error:
                    batch_error = str(error)
                    energy_records = []

                if args.raw_json:
                    raw_ecu_responses.append(
                        {
                            "ecu_id": ecu_id,
                            "response": batch_response,
                            **({"error": batch_error} if batch_error else {}),
                        }
                    )

                records_by_inverter = {}
                record_errors = {}
                unassigned_errors = []
                if not batch_error:
                    for record in energy_records:
                        if not isinstance(record, str):
                            unassigned_errors.append(
                                f"Invalid batch energy record: {record!r}"
                            )
                            continue
                        uid, separator, remainder = record.partition("-")
                        channel_text, separator2, value_text = remainder.partition("-")
                        if not uid or not separator or not separator2:
                            unassigned_errors.append(
                                f"Invalid batch energy record: {record!r}"
                            )
                            continue
                        try:
                            channel_number = int(channel_text)
                            if not 1 <= channel_number <= 4:
                                raise ValueError("channel must be between 1 and 4")
                            value = float(value_text)
                            if not math.isfinite(value):
                                raise ValueError("energy must be a finite number")
                        except ValueError as error:
                            record_errors.setdefault(uid, []).append(
                                f"Invalid energy record {record!r}: {error}"
                            )
                            continue
                        records_by_inverter.setdefault(uid, {})[
                            f"e{channel_number}"
                        ] = value

                for inverter in ecu_inverters:
                    uid = inverter.get("uid")
                    if not isinstance(uid, str) or not uid:
                        raise RuntimeError(
                            f"APsystems returned an invalid inverter entry for ECU "
                            f"{ecu_id}"
                        )
                    channel_totals = records_by_inverter.get(uid, {})
                    item_errors = list(record_errors.get(uid, []))
                    if batch_error:
                        item_errors.append(batch_error)
                    item_errors.extend(unassigned_errors)
                    if not channel_totals and not item_errors:
                        item_errors.append("No energy records were returned for inverter")
                    item = {
                        "ecu_id": ecu_id,
                        "inverter_id": uid,
                        "type": inverter.get("type"),
                        "date": today,
                        "energy_kwh": (
                            sum(channel_totals.values())
                            if channel_totals and not item_errors
                            else None
                        ),
                        "channel_energy_kwh": channel_totals,
                    }
                    if item_errors:
                        item["error"] = "; ".join(item_errors)
                    inverter_results.append(item)

            if args.raw_json:
                output = {
                    "date": today,
                    "api_calls": 1 + len(devices),
                    "ecus": raw_ecu_responses,
                    "inverters": inverter_results,
                }
            else:
                output = inverter_results
            print(json.dumps(output, indent=2))
            return int(any("error" in item for item in inverter_results))

        today = get_today()
        result = request_api(
            f"/user/api/v2/systems/energy/{system_id}",
            {"energy_level": "hourly", "date_range": today},
        )
        if args.raw_json:
            print(json.dumps(result, indent=2))
            return 0
        values = result.get("data")
        if not isinstance(values, list) or len(values) != 24:
            count = len(values) if isinstance(values, list) else "not an array"
            raise RuntimeError(f"Expected 24 hourly values; got {count}")
        try:
            total = sum(float(value) for value in values)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Hourly energy values must be numeric") from error

        print(json.dumps({"date": today, "energy_kwh": total}, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
