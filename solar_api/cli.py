"""Command-line entry point for APsystems data queries."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .client import APsystemsClient
from .config import load_env_file
from .reports import analyze_inverter_file


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line options and validate their combinations."""
    parser = argparse.ArgumentParser(
        description="Fetch APsystems energy, check a system, or report today's inverter energy."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check-system",
        action="store_true",
        help="check the configured system ID using the system-details endpoint",
    )
    actions.add_argument(
        "--inverters",
        action="store_true",
        help="show today's energy for each inverter in the configured system",
    )
    actions.add_argument(
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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Load settings, run the selected report, and print JSON output."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.ecu_id and not args.inverters:
        parser.error("--ecu-id requires --inverters")
    if args.analyze_file and not 0 <= args.below_average_percent <= 100:
        parser.error("--below-average-percent must be between 0 and 100")

    script_dir = Path(__file__).resolve().parent.parent
    env_file = Path(
        os.environ.get("APPSYSTEMS_ENV_FILE", str(script_dir / ".env"))
    ).expanduser()
    try:
        load_env_file(env_file)
        if args.analyze_file:
            output = analyze_inverter_file(
                args.analyze_file, args.below_average_percent
            )
        else:
            for name in (
                "APPSYSTEMS_APP_ID",
                "APPSYSTEMS_APP_SECRET",
                "APPSYSTEMS_SYSTEM_ID",
            ):
                if not os.environ.get(name):
                    raise ValueError(f"Set {name} in {env_file} or the environment")

            system_id = os.environ["APPSYSTEMS_SYSTEM_ID"]
            client = APsystemsClient(
                app_id=os.environ["APPSYSTEMS_APP_ID"],
                app_secret=os.environ["APPSYSTEMS_APP_SECRET"],
                system_id=system_id,
                base_url=os.environ.get(
                    "APPSYSTEMS_BASE_URL",
                    "https://api.apsystemsema.com:9282",
                ),
                timezone=os.environ.get("APPSYSTEMS_TIMEZONE"),
            )
            if args.check_system:
                details = client.check_system()
                output = (
                    details
                    if args.raw_json
                    else "System-details request succeeded for the configured system ID."
                )
            elif args.inverters:
                output = client.get_inverter_energy(
                    ecu_id=args.ecu_id,
                    raw_response=args.raw_json,
                )
                inverters = (
                    output.get("inverters", [])
                    if isinstance(output, dict)
                    else output
                )
                if any("error" in item for item in inverters):
                    print(json.dumps(output, indent=2))
                    return 1
            else:
                output = client.get_today_energy(raw_response=args.raw_json)

        if isinstance(output, str):
            print(output)
        else:
            print(json.dumps(output, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
