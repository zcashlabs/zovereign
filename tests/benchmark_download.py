#!/usr/bin/env python3
"""Compare downloader scheduling against a controlled local HTTP publisher."""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import statistics
import tempfile
import time

from test_stack import publisher


SCENARIOS = {
    "uniform": lambda index: 0.12,
    "first_straggler": lambda index: 0.8 if index == 0 else 0.12,
    "repeated_stragglers": lambda index: 0.6 if index % 5 == 0 else 0.08,
    "variable": lambda index: 0.04 + (index * 17 % 13) * 0.035,
}


def load(path):
    spec = importlib.util.spec_from_file_location("subject", path)
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    return subject


def measure(module, delay, workers):
    subject = load(module)
    with publisher() as server, tempfile.TemporaryDirectory() as folder:
        chunk = 65536
        data = bytes(range(256)) * (chunk * 18 // 256)
        server["files"]["/data"] = data

        def pause(byte_range):
            index = int(byte_range.split("=")[1].split("-")[0]) // chunk
            time.sleep(delay(index))

        server["before_response"] = pause
        item = {"url": server["url"] + "/data", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
        output = Path(folder) / "sample.part"
        start = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            subject.download(item, output, chunk, 0, workers=workers)
        seconds = time.monotonic() - start
        assert output.read_bytes() == data and not subject.CHILDREN
        return seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Original stack.py")
    parser.add_argument("--candidate", default=str(Path(__file__).resolve().parents[1] / "docker/stack.py"))
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--workers", default="3,6")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    args = parser.parse_args()
    try:
        workers_list = [int(value) for value in args.workers.split(",")]
    except ValueError:
        parser.error("workers must be comma-separated integers")
    if args.trials < 1 or any(worker < 1 or worker > 8 for worker in workers_list):
        parser.error("positive trials and workers 1–8 required")
    try:
        scenarios = {name: SCENARIOS[name] for name in args.scenarios.split(",")}
    except KeyError as error:
        parser.error(f"unknown scenario: {error}")

    rows = []
    for workers in workers_list:
        for name, delay in scenarios.items():
            for trial in range(args.trials):
                order = [("old", args.baseline), ("new", args.candidate)]
                if trial % 2:
                    order.reverse()
                for label, module in order:
                    seconds = measure(module, delay, workers)
                    row = {"scenario": name, "workers": workers, "trial": trial + 1,
                           "version": label, "seconds": seconds}
                    rows.append(row)
                    print(json.dumps(row), flush=True)

    for workers in workers_list:
        for name in scenarios:
            summary = {}
            for label in ("old", "new"):
                values = [row["seconds"] for row in rows if row["workers"] == workers
                          and row["scenario"] == name and row["version"] == label]
                summary[label] = {"median": statistics.median(values), "min": min(values),
                                  "max": max(values),
                                  "stdev": statistics.stdev(values) if len(values) > 1 else 0}
            old, new = summary["old"]["median"], summary["new"]["median"]
            print(json.dumps({"summary": name, "workers": workers, **summary,
                              "saved_seconds": old - new,
                              "reduction_percent": 100 * (1 - new / old)}), flush=True)


if __name__ == "__main__":
    main()
