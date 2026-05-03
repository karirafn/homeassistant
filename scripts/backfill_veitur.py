#!/usr/bin/env python3
"""
Correct sensor.veitur_hot_water monthly statistics using Veitur API data.

Readings arrive after the month boundary, causing HA to attribute
consumption to the wrong month. This script compares API usage values
against HA statistics and applies sum adjustments to shift consumption
to the correct calendar month.

Reads credentials from /config/secrets.yaml (inside HA container)
or from environment variables VEITUR_API_KEY and HA_TOKEN.
"""

import json
import os
import sys
import requests
import websocket
from datetime import datetime, timezone, timedelta
from pathlib import Path

SECRETS_PATH = Path("/config/secrets.yaml")
HA_URL = os.getenv("HA_URL", "http://localhost:8123")
HA_WS_URL = HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"

PERMANENT_NUMBER = "196313"
STATISTIC_ID = "sensor.veitur_hot_water"
BASELINE_DATE = "2025-01-01"


def read_secret(key: str) -> str:
    if not SECRETS_PATH.exists():
        return ""
    for line in SECRETS_PATH.read_text().splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def get_config() -> tuple[str, str]:
    api_key = os.getenv("VEITUR_API_KEY") or read_secret("veitur_api_key")
    ha_token = os.getenv("HA_TOKEN") or read_secret("ha_token")
    return api_key, ha_token


def fetch_readings(api_key: str) -> list[dict]:
    resp = requests.get(
        "https://api.veitur.is/api/meter/reading-history",
        params={
            "PermanentNumber": PERMANENT_NUMBER,
            "DateFrom": f"{BASELINE_DATE}T00:00:00",
            "DateTo": datetime.now().strftime("%Y-%m-%dT23:59:59"),
        },
        headers={"Authorization": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["meterReading"]


def build_monthly_usage(readings: list[dict]) -> dict[str, float]:
    monthly: dict[str, float] = {}
    for r in readings:
        usage = float(r["usage"])
        if usage == 0.0:
            continue
        reading_date = datetime.fromisoformat(r["readingDate"])
        target = reading_date - timedelta(days=1)
        key = target.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + usage
    return monthly


def ws_connect(ha_token: str) -> websocket.WebSocket:
    ws = websocket.create_connection(HA_WS_URL, timeout=30)
    msg = json.loads(ws.recv())
    if msg.get("type") != "auth_required":
        ws.close()
        sys.exit(f"Unexpected WS message: {msg}")
    ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
    msg = json.loads(ws.recv())
    if msg.get("type") != "auth_ok":
        ws.close()
        sys.exit(f"Auth failed: {msg}")
    return ws


def get_ha_statistics(ws: websocket.WebSocket, msg_id: int) -> dict[str, float]:
    ws.send(json.dumps({
        "id": msg_id,
        "type": "recorder/statistics_during_period",
        "start_time": f"{BASELINE_DATE}T00:00:00+00:00",
        "end_time": (datetime.now(timezone.utc) + timedelta(days=31)).strftime("%Y-%m-%dT00:00:00+00:00"),
        "statistic_ids": [STATISTIC_ID],
        "period": "month",
        "types": ["change"],
    }))
    msg = json.loads(ws.recv())
    if not msg.get("success"):
        sys.exit(f"Failed to get statistics: {msg}")

    ha_monthly: dict[str, float] = {}
    for entry in msg["result"].get(STATISTIC_ID, []):
        start = datetime.fromtimestamp(entry["start"] / 1000, tz=timezone.utc)
        ha_monthly[start.strftime("%Y-%m")] = round(entry["change"], 3)
    return ha_monthly


def adjust_statistics(ws: websocket.WebSocket, adjustments: list[tuple[str, float]]) -> None:
    """Apply sum adjustments. Each adjustment shifts the sum at that month
    and all subsequent months, so we apply in chronological order and
    pair each with a reverse adjustment on the next month to localize
    the effect."""
    msg_id = 10
    for ym, adj in adjustments:
        year, month = int(ym[:4]), int(ym[5:7])
        start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
        ws.send(json.dumps({
            "id": msg_id,
            "type": "recorder/adjust_sum_statistics",
            "statistic_id": STATISTIC_ID,
            "start_time": start.isoformat(),
            "adjustment": adj,
            "adjustment_unit_of_measurement": "m³",
        }))
        msg = json.loads(ws.recv())
        if not msg.get("success"):
            print(f"  WARNING: adjustment at {ym} failed: {msg}")
        msg_id += 1


def main() -> None:
    api_key, ha_token = get_config()
    if not api_key:
        sys.exit("Veitur API key not found.")
    if not ha_token:
        sys.exit("HA token not found.")

    print("Fetching Veitur readings...")
    readings = fetch_readings(api_key)
    api_monthly = build_monthly_usage(readings)

    ws = ws_connect(ha_token)
    print("Fetching HA statistics...")
    ha_monthly = get_ha_statistics(ws, msg_id=1)

    print(f"\n{"Month":<10} {"API":>8} {"HA":>8} {"Diff":>8}")
    print("-" * 36)

    adjustments: list[tuple[str, float]] = []
    sorted_months = sorted(set(list(api_monthly.keys()) + list(ha_monthly.keys())))

    for ym in sorted_months:
        api_val = api_monthly.get(ym, 0.0)
        ha_val = ha_monthly.get(ym, 0.0)
        diff = round(api_val - ha_val, 3)
        marker = " ←" if abs(diff) > 1.0 else ""
        print(f"  {ym:<8} {api_val:>8.2f} {ha_val:>8.2f} {diff:>+8.2f}{marker}")

        if abs(diff) > 0.5:
            adjustments.append((ym, diff))

    if not adjustments:
        print("\nNo adjustments needed.")
        ws.close()
        return

    # adjust_sum_statistics cascades to all future sums, but since
    # change = sum[M] - sum[M-1], the cascade cancels out for all
    # months except the target. Each adjustment independently fixes
    # only its target month's change value.
    print(f"\nApplying {len(adjustments)} adjustments...")
    for ym, adj in adjustments:
        print(f"  {ym}: {adj:+.3f}")
    adjust_statistics(ws, adjustments)

    ws.close()

    # Verify with fresh connection
    print("\nVerifying...")
    ws2 = ws_connect(ha_token)
    ha_monthly_after = get_ha_statistics(ws2, msg_id=1)
    ws2.close()
    for ym in sorted_months:
        api_val = api_monthly.get(ym, 0.0)
        ha_val = ha_monthly_after.get(ym, 0.0)
        diff = round(api_val - ha_val, 3)
        status = "✓" if abs(diff) < 0.5 else "✗"
        print(f"  {ym}: API={api_val:.2f}  HA={ha_val:.2f}  diff={diff:+.2f}  {status}")
    print("\nDone.")


if __name__ == "__main__":
    main()
