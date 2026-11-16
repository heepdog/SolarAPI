#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python="${project_dir}/.venv/bin/python"
job_script="${project_dir}/run_daily_report.py"
log_file="${project_dir}/solar_daily.log"
start_marker="# BEGIN APsystems daily report"
end_marker="# END APsystems daily report"
legacy_marker="# solar-api-daily-report"

if ! command -v crontab >/dev/null 2>&1; then
  printf 'crontab command not found; install cron before running this script.\n' >&2
  exit 1
fi

if [[ ! -x "$python" ]]; then
  printf 'Virtual environment Python not found or not executable: %s\n' "$python" >&2
  exit 1
fi

if [[ ! -f "$job_script" ]]; then
  printf 'Daily report script not found: %s\n' "$job_script" >&2
  exit 1
fi

if [[ "$(cat /etc/timezone 2>/dev/null || true)" != "America/Chicago" ]]; then
  printf 'This schedule uses the machine timezone; expected America/Chicago for 10 PM Central.\n' >&2
  exit 1
fi

if existing_crontab=$(crontab -l 2>/dev/null); then
  :
else
  existing_crontab=""
fi

temporary_file=$(mktemp)
trap 'rm -f "$temporary_file"' EXIT

if [[ -n "$existing_crontab" ]]; then
  printf '%s\n' "$existing_crontab" |
    awk -v start="$start_marker" -v end="$end_marker" -v legacy="$legacy_marker" '
      $0 == start { in_block = 1; next }
      $0 == end { in_block = 0; next }
      in_block { next }
      index($0, legacy) { next }
      { print }
    ' >"$temporary_file"
fi

{
  if [[ -s "$temporary_file" ]]; then
    printf '\n'
  fi
  printf '%s\n' "$start_marker"
  printf '%s\n' "0 22 * * * \"$python\" \"$job_script\" >> \"$log_file\" 2>&1"
  printf '%s\n' "$end_marker"
} >>"$temporary_file"

crontab "$temporary_file"
printf 'Installed daily APsystems report at 10:00 PM America/Chicago.\n'
