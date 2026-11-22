"""Example custom plugin: writes the latest values to a CSV file.

Many historians and older SCADA packages can import a CSV/text file on a
schedule, so this is also a template for your own integrations (MQTT, SQL
Server, a REST API...). Enable it in config.yaml:

integrations:
  - type: custom
    enabled: true
    name: csv-export
    class: "example_csv_export:CsvExport"
    path: "data/live-values.csv"
    update_seconds: 30
"""
import csv
import os
from datetime import datetime
from pathlib import Path

from ems.integrations import QUANTITIES, Integration


class CsvExport(Integration):
    type_name = "csv_export"

    def start(self):
        p = Path(self.options.get("path", "data/live-values.csv"))
        self.path = p if p.is_absolute() else self.cfg.base_dir / p
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.running = True

    def describe(self):
        return str(self.path) if self.running else ""

    def publish(self, snapshot):
        stamp = datetime.fromtimestamp(snapshot["ts"], self.cfg.tz).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "meter_id", "name", "online"] + QUANTITIES)
            for m in snapshot["meters"]:
                w.writerow([stamp, m["id"], m["name"], int(m["online"])] + [m.get(q) for q in QUANTITIES])
        os.replace(tmp, self.path)  # readers never see a half-written file
