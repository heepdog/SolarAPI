#!/usr/bin/env python3
"""Compatibility launcher; prefer ``python -m solar_api``."""

import json
import os
import sys
from pathlib import Path

import emailalert

import requests

from dotenv import load_dotenv

from solar_api import APsystemsClient
from solar_api.reports import analyze_inverter_file

IFTTT_URL = os.getenv("IFTTT_URL")

if __name__ == "__main__":
    load_dotenv()
    
    client = APsystemsClient(
        app_id=os.getenv("HEATMOR_APSYSTEMS_APP_ID"),
        app_secret=os.getenv("HEATMOR_APSYSTEMS_APP_SECRET"),
        system_id=os.getenv("HEATMOR_APSYSTEMS_SYSTEM_ID"),
    )
    # print(client.check_system())
    
    # results = client.get_inverter_energy()
    report_path = Path("inverterdata2.txt")
    # with report_path.open("w", encoding="utf-8") as file:
        # json.dump(results, file, indent=2)
    analysis = analyze_inverter_file(report_path)
    json.dump(analysis, sys.stdout, indent=2)
    
    # requests.post(IFTTT_URL, json=analysis)
    
    if analysis["inverter_errors"]:
        print(f'{analysis["inverter_errors"]} \n {analysis["total_system_output_kwh"]}')
        email_content = "\n".join(
            [
                f"Total system output today: "
                f"{analysis['total_system_output_kwh']:.2f} kWh",
                "Inverters with low output today:",
                *analysis["inverter_errors"],
            ]
        )
        email_message = 'Heatmor/KMS Solar Details'
        emailalert.sendalert(subject=email_message, content=email_content, message="")
