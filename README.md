# SolarAPI

A Python client and reporting tool for APsystems OpenAPI. It can check system
access, retrieve today's system or inverter energy, analyze saved inverter
reports, and run a daily report that stores readings and emails when the number
of non-producing channels changes.

## Requirements

- Python 3.10 or newer
- APsystems OpenAPI App ID, App Secret, and System ID
- Gmail sender account and an app password if you want email alerts
- `crontab` to install the optional daily schedule

## Install

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create a `.env` file in the project directory. For one system:

```dotenv
APPSYSTEMS_APP_ID=your-openapi-app-id
APPSYSTEMS_APP_SECRET=your-openapi-app-secret
APPSYSTEMS_SYSTEM_ID=your-system-id

# Optional. Defaults to the machine's local timezone.
APPSYSTEMS_TIMEZONE=America/Chicago

# Optional. Defaults to ./solar_reports.sqlite3.
APPSYSTEMS_DB_PATH=solar_reports.sqlite3

# Needed to send email through Gmail.
SENDER_EMAIL=you@example.com
EMAIL_APP_PASSWORD=your-gmail-app-password
ALERT_RECIPIENT=alerts@example.com
```

Use an app password rather than your regular Gmail password. Keep `.env`
private; it is listed in `.gitignore`.

The daily report also supports multiple named systems. Configure all three
values for each prefix; if any prefixed system settings are present, the
prefixed configuration is used instead of the single-system variables:

```dotenv
HEATMOR_APSYSTEMS_APP_ID=...
HEATMOR_APSYSTEMS_APP_SECRET=...
HEATMOR_APSYSTEMS_SYSTEM_ID=...

KMS_APSYSTEMS_APP_ID=...
KMS_APSYSTEMS_APP_SECRET=...
KMS_APSYSTEMS_SYSTEM_ID=...
```

## Daily report and email alerts

Run the report manually:

```bash
source .venv/bin/activate
python run_daily_report.py
```

For each configured system, the script fetches today's inverter readings,
writes a timestamped JSON report in the project directory, saves the run and
channel readings to SQLite, and prints a JSON summary. By default, the database
is `solar_reports.sqlite3`; `APPSYSTEMS_DB_PATH` can change its location.
Relative database paths are resolved from the project directory.

### When an email is sent

The alert compares the number of channels with energy readings less than or
equal to zero in the current run with the count in the immediately preceding
complete run for the same system. An email is sent only if those counts differ.
The email includes the previous count, current count, and change.

- The first run for a system has no baseline, so it does not send an alert.
- Runs with inverter-reading errors are not used for this comparison.
- The comparison is by count, not channel identity. For example, if one channel
  stops producing while another starts and the total count stays the same, no
  email is sent.
- Low-output analysis is still included in the report output, but a low-output
  finding alone does not send this email.

Email is sent through Gmail SMTP using `SENDER_EMAIL`,
`EMAIL_APP_PASSWORD`, and `ALERT_RECIPIENT`. Keep these real values in `.env`,
not in source files or committed examples.

The script exits with a nonzero status if a system fails or if the analysis
flags low-output readings. This status is separate from the email rule.

### Schedule the report

The installer creates or replaces the marked daily-report block in the current
user's crontab:

```bash
./install_daily_cron.sh
crontab -l
```

It schedules the report for 10:00 PM in the machine's local timezone and
requires the timezone to be `America/Chicago`, a working `.venv`, and an
installed `crontab` command. Standard output and errors are appended to
`solar_daily.log` in the project directory.

## Command-line client

The package CLI reads the single-system `APPSYSTEMS_*` settings from `.env` or
the environment. It does not send email or save SQLite snapshots.

```bash
# Check that the configured system can be reached.
python -m solar_api --check-system

# Show today's system energy summary.
python -m solar_api

# Show today's energy by inverter.
python -m solar_api --inverters

# Include the full inverter API response.
python -m solar_api --inverters --raw-json

# Analyze a saved inverter report without making API calls.
python -m solar_api --analyze-file inverterdata_<system>_<date>_<timestamp>.json
```

Use `--ecu-id ECU_ID` with `--inverters` to limit the result to one ECU. The
`--below-average-percent` option controls the analysis threshold and defaults to
80 (flagging readings more than 80% below the average for their channel).

## Tests

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```

## Data and generated files

The daily runner creates timestamped `inverterdata_*.json` reports, the SQLite
database, and `solar_daily.log`. The database stores each run, inverter
observations, channel readings, and any inverter errors so runs can be compared
over time. `.gitignore` excludes the database, `.env`, Python cache
directories, and compiled Python files.

`get_yesterday_energy.sh` is a separate legacy utility for retrieving the
previous day's hourly system total; it requires `curl`, `jq`, `openssl`, and
GNU `date`. It is not part of the scheduled inverter report workflow.
