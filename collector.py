#!/usr/bin/env python3
"""iperf3 speedtest collector.

Periodically runs iperf3 download (-R) and upload tests against a public server
(default: Init7) and serves the latest result over HTTP at
GET /api/speedtest/latest as {"download": <mbps>, "upload": <mbps>, "timestamp": <iso>}.

The path mirrors speedtest-tracker so the dashboard's existing fetch URL works
unchanged; the dashboard is told it's an "iperf" source so it reads the flat shape.

Stdlib only — no third-party dependencies.
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Config (env-overridable) ---
IPERF_HOST = os.environ.get("IPERF_HOST", "speedtest.init7.net")
IPERF_PORT = os.environ.get("IPERF_PORT", "5202")
# Over the internet, providers rate-limit each TCP flow, so a single stream can't
# fill a 10G line — you need many parallel streams to aggregate up (Init7 officially
# recommends 16). With iperf3 >=3.16 each stream gets its own thread, so a high
# stream count can saturate the CPU; if a run logs ~100% CPU, that machine (not the
# line) is the limit — add vCPUs rather than lowering the stream count.
IPERF_PARALLEL = os.environ.get("IPERF_PARALLEL", "16")
IPERF_DURATION = int(os.environ.get("IPERF_DURATION", "10"))
# Seconds to omit at the start (-O) so TCP slow-start isn't averaged into the result.
IPERF_OMIT = int(os.environ.get("IPERF_OMIT", "2"))
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "3600"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
RESULT_PATH = os.path.join(DATA_DIR, "latest.json")
NCPU = os.cpu_count() or 1


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def run_iperf(reverse):
    """Run one iperf3 test; return (Mbps, cpu_host_pct), or None on failure.

    reverse=True measures download (server -> client); False measures upload.
    cpu_host_pct is iperf3's own CPU utilisation for this machine — near 100
    means the test is CPU-bound (the host, not the line, is the limit).
    """
    cmd = [
        "iperf3",
        "-c", IPERF_HOST,
        "-p", str(IPERF_PORT),
        "-P", str(IPERF_PARALLEL),
        "-t", str(IPERF_DURATION),
        "-O", str(IPERF_OMIT),
        "--json",
    ]
    if reverse:
        cmd.append("-R")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=IPERF_DURATION + IPERF_OMIT + 30,
        )
    except subprocess.TimeoutExpired:
        log(f"iperf3 {'download' if reverse else 'upload'} timed out")
        return None

    if proc.returncode != 0:
        log(f"iperf3 {'download' if reverse else 'upload'} failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
        return None

    try:
        data = json.loads(proc.stdout)
        end = data["end"]
        # On reverse (download), the client receives → sum_received is the throughput.
        # On forward (upload), the client sends → sum_sent.
        key = "sum_received" if reverse else "sum_sent"
        mbps = end[key]["bits_per_second"] / 1_000_000.0
        cpu_host = end.get("cpu_utilization_percent", {}).get("host_total")
        return mbps, cpu_host
    except (ValueError, KeyError) as exc:
        log(f"could not parse iperf3 output: {exc}")
        return None


def write_result(result):
    """Atomically write the latest result so the HTTP server never sees a partial file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, RESULT_PATH)


def run_cycle():
    log("starting test cycle")
    down = run_iperf(reverse=True)
    up = run_iperf(reverse=False)
    if down is None or up is None:
        log("cycle incomplete — keeping previous result")
        return
    download, down_cpu = down
    upload, up_cpu = up
    result = {
        "download": round(download, 2),
        "upload": round(upload, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    write_result(result)

    # iperf3's host_total is summed across threads, so it can exceed 100% — divide
    # by 100 to read it as "cores used", and compare against the CPUs we have.
    def cores(v):
        return f"{v / 100:.1f}/{NCPU} cores" if isinstance(v, (int, float)) else "cpu n/a"
    log(f"cycle ok: down {download:.0f} Mbps ({cores(down_cpu)}), up {upload:.0f} Mbps ({cores(up_cpu)})")

    peak = max((c for c in (down_cpu, up_cpu) if isinstance(c, (int, float))), default=0)
    if peak >= 85 * NCPU:
        log("⚠ CPU-bound: nearly all vCPUs are saturated — this machine, not the line, "
            "is the limit. Add vCPUs (keep IPERF_PARALLEL high).")
    elif peak and peak < 60 * NCPU:
        log(f"note: only ~{peak / 100:.1f} of {NCPU} cores used, so CPU isn't the limit — "
            "the likely cap is the single virtio NIC queue (enable multiqueue = vCPUs).")


def collector_loop():
    while True:
        try:
            run_cycle()
        except Exception as exc:  # never let the loop die
            log(f"unexpected error in cycle: {exc}")
        time.sleep(INTERVAL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") != "/api/speedtest/latest":
            self.send_error(404)
            return
        try:
            with open(RESULT_PATH) as f:
                body = f.read().encode()
            status = 200
        except FileNotFoundError:
            body = b"{}"
            status = 503  # no result yet
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence per-request logging


def main():
    try:
        version = subprocess.run(["iperf3", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    except Exception:
        version = "unknown"
    log(f"iperf3: {version}")
    log(f"detected {os.cpu_count()} CPUs")
    log(f"target {IPERF_HOST}:{IPERF_PORT}, -P {IPERF_PARALLEL} -t {IPERF_DURATION}s "
        f"-O {IPERF_OMIT}s, every {INTERVAL_SECONDS}s")
    log(f"serving GET /api/speedtest/latest on :{HTTP_PORT}")

    threading.Thread(target=collector_loop, daemon=True).start()
    ThreadingHTTPServer(("", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
