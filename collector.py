#!/usr/bin/env python3
"""iperf3 speedtest collector.

Periodically runs an idle latency probe plus iperf3 download (-R) and upload tests
against a public server (default: Init7), and serves results over HTTP:

  GET /api/speedtest/latest   -> the most recent reading (flat object, Mbps + Gbps + ping)
  GET /api/speedtest/history  -> a rolling list of past readings (oldest -> newest)
  GET /                       -> a self-contained dashboard (index.html)

The /latest path mirrors speedtest-tracker so an existing fetch URL works unchanged; the
dashboard is told it's an "iperf" source so it reads the flat shape.

Stdlib only — no third-party dependencies.
"""

import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

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
# Scheduling: run once per day at a random time inside a local-time window, to stay
# light on the public test server. TZ sets the local timezone (needs OS tzdata).
TZ = os.environ.get("TZ", "Europe/Zurich")
WINDOW_START_HOUR = int(os.environ.get("DAILY_WINDOW_START_HOUR", "2"))
WINDOW_END_HOUR = int(os.environ.get("DAILY_WINDOW_END_HOUR", "5"))
# Off by default: a fresh deploy waits for the next daily window. Set RUN_ON_START=true
# to also run one test immediately on startup (handy for a first reading / testing).
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() in ("1", "true", "yes")
# Latency probe: time to open a TCP connection to the iperf host (SYN -> SYN/ACK). Pure
# stdlib and unprivileged, so it behaves the same in Docker and an LXC; we keep the best
# of a few samples on the idle line, before the throughput test loads it.
PING_SAMPLES = int(os.environ.get("PING_SAMPLES", "5"))
PING_TIMEOUT = float(os.environ.get("PING_TIMEOUT", "2"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
RESULT_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
# How many past readings to keep (~one daily test each). 400 ≈ 13 months, enough to
# back the dashboard's week/month/year views.
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "400"))
# The dashboard (index.html) ships alongside this script, so it works the same
# whether run from a repo checkout or copied into the Docker image.
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")
NCPU = os.cpu_count() or 1

if ZoneInfo is not None:
    try:
        LOCAL_TZ = ZoneInfo(TZ)
        TZ_OK = True
    except Exception:
        LOCAL_TZ, TZ_OK = timezone.utc, False
else:  # pragma: no cover
    LOCAL_TZ, TZ_OK = timezone.utc, False


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


def run_ping():
    """Measure idle latency to the iperf host as the TCP-connect (SYN -> SYN/ACK) time.

    Returns the best (minimum) of PING_SAMPLES samples in milliseconds, or None if every
    attempt failed. We use a plain TCP connect rather than ICMP so it needs no special
    privileges and works identically in a container and an unprivileged systemd service.
    """
    try:
        port = int(IPERF_PORT)
    except ValueError:
        return None
    rtts = []
    for _ in range(max(1, PING_SAMPLES)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(PING_TIMEOUT)
        try:
            start = time.perf_counter()
            s.connect((IPERF_HOST, port))
            rtts.append((time.perf_counter() - start) * 1000)
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.2)
    return round(min(rtts), 1) if rtts else None


def write_result(result):
    """Atomically write the latest result so the HTTP server never sees a partial file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, RESULT_PATH)


def append_history(result):
    """Append one completed reading to the rolling history (oldest -> newest), keeping at
    most HISTORY_SIZE entries. Written atomically; a corrupt/missing file starts fresh."""
    try:
        with open(HISTORY_PATH) as f:
            hist = json.load(f)
        if not isinstance(hist, list):
            hist = []
    except (FileNotFoundError, ValueError):
        hist = []
    hist.append(result)
    hist = hist[-HISTORY_SIZE:]
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(hist, f)
    os.replace(tmp, HISTORY_PATH)


def run_cycle():
    log("starting test cycle")
    ping = run_ping()  # idle latency first, before the throughput test loads the line
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
        # Convenience fields pre-rounded to 1 decimal in Gbps (Homepage can't cap decimals).
        "download_gbps": round(download / 1000, 1),
        "upload_gbps": round(upload / 1000, 1),
        "ping_ms": ping,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    write_result(result)
    append_history(result)

    # iperf3's host_total is summed across threads, so it can exceed 100% — divide
    # by 100 to read it as "cores used", and compare against the CPUs we have.
    def cores(v):
        return f"{v / 100:.1f}/{NCPU} cores" if isinstance(v, (int, float)) else "cpu n/a"
    ping_str = f"{ping} ms" if ping is not None else "n/a"
    log(f"cycle ok: down {download:.0f} Mbps ({cores(down_cpu)}), up {upload:.0f} Mbps "
        f"({cores(up_cpu)}), ping {ping_str}")

    peak = max((c for c in (down_cpu, up_cpu) if isinstance(c, (int, float))), default=0)
    if peak >= 85 * NCPU:
        log("⚠ CPU-bound: nearly all vCPUs are saturated — this machine, not the line, "
            "is the limit. Add vCPUs (keep IPERF_PARALLEL high).")
    elif peak and peak < 60 * NCPU:
        log(f"note: only ~{peak / 100:.1f} of {NCPU} cores used, so CPU isn't the limit — "
            "the cap is the network path: the NIC/queue (VM: enable multiqueue), the host "
            "uplink, or the upstream test server itself.")


def next_run():
    """Return (seconds_to_wait, target_local_datetime) for the next daily run:
    a random time within [WINDOW_START_HOUR, WINDOW_END_HOUR) local time, on the
    next calendar occurrence of that window."""
    now = datetime.now(LOCAL_TZ)
    window_seconds = max(1, (WINDOW_END_HOUR - WINDOW_START_HOUR) * 3600)
    offset = random.randint(0, window_seconds - 1)
    target = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0) \
        + timedelta(seconds=offset)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds(), target


def safe_cycle():
    try:
        run_cycle()
    except Exception as exc:  # never let the loop die
        log(f"unexpected error in cycle: {exc}")


def collector_loop():
    if RUN_ON_START:
        safe_cycle()
    while True:
        delay, target = next_run()
        log(f"next test at {target:%Y-%m-%d %H:%M} {TZ} (in {delay / 3600:.1f}h)")
        time.sleep(delay)
        safe_cycle()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/speedtest/latest":
            self._serve_latest()
        elif path == "/api/speedtest/history":
            self._serve_history()
        elif path in ("", "/index.html"):
            self._serve_dashboard()
        else:
            self.send_error(404)

    def _serve_latest(self):
        try:
            with open(RESULT_PATH) as f:
                body = f.read().encode()
            status = 200
        except FileNotFoundError:
            body = b"{}"
            status = 503  # no result yet
        self._respond(status, "application/json", body)

    def _serve_history(self):
        try:
            with open(HISTORY_PATH) as f:
                body = f.read().encode()
        except FileNotFoundError:
            body = b"[]"  # no readings yet — an empty timeline, not an error
        self._respond(200, "application/json", body)

    def _serve_dashboard(self):
        try:
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
            self._respond(200, "text/html; charset=utf-8", body)
        except FileNotFoundError:
            self.send_error(404)

    def _respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
    log(f"target {IPERF_HOST}:{IPERF_PORT}, -P {IPERF_PARALLEL} -t {IPERF_DURATION}s -O {IPERF_OMIT}s")
    log(f"schedule: once daily between {WINDOW_START_HOUR:02d}:00 and {WINDOW_END_HOUR:02d}:00 {TZ}"
        + ("" if TZ_OK else " (⚠ timezone unavailable — using UTC; add tzdata)")
        + (", plus one run on startup" if RUN_ON_START
           else " (set RUN_ON_START=true to also test on startup)"))
    log(f"keeping up to {HISTORY_SIZE} readings of history")
    log(f"serving dashboard / and GET /api/speedtest/{{latest,history}} on :{HTTP_PORT}")

    threading.Thread(target=collector_loop, daemon=True).start()
    ThreadingHTTPServer(("", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
