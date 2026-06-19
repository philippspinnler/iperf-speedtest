# iperf-speedtest

A tiny self-hosted speedtest collector that runs **iperf3** against a public server
(default: [Init7](https://www.init7.net/)) and serves the latest download/upload result
over HTTP. Built as a more accurate alternative to Ookla-based
[speedtest-tracker](https://github.com/linuxserver/docker-speedtest-tracker) on fast
fiber connections.

One Python process (stdlib only) runs the test loop and an HTTP server. Results are
persisted atomically to a volume so the last-known-good value survives restarts and
transient test failures.

## Endpoint

```
GET /api/speedtest/latest
→ { "download": 9450.21, "upload": 9380.14, "timestamp": "2026-06-19T10:30:00+00:00" }
```

Speeds are in **Mbps**. Returns `503 {}` until the first test completes. The path mirrors
speedtest-tracker so it drops into existing tooling unchanged.

## Run

```sh
docker compose up -d --build
curl http://localhost:8080/api/speedtest/latest
```

## ⚠️ Bandwidth warning

Init7 is up to **25 Gbit/s symmetric**. With `-P 16`, each test direction can move several
GB. A `-t 5` download + upload at the default 60-minute interval still transfers a large
amount of data per day. **Only run this on an unmetered connection.** Tune `IPERF_DURATION`
and `INTERVAL_SECONDS` to control usage.

## Configuration (environment variables)

| Variable | Default | Description |
| --- | --- | --- |
| `IPERF_HOST` | `speedtest.init7.net` | iperf3 server hostname |
| `IPERF_PORT` | `5202` | iperf3 server port |
| `IPERF_PARALLEL` | `16` | parallel streams (`-P`). Over the internet each TCP flow is rate-limited, so you need many streams to fill a 10G line (Init7 recommends 16). iperf3 ≥3.16 gives each stream its own thread, so a high count can saturate the CPU — if a run logs ~100% CPU, add vCPUs rather than lowering this. |
| `IPERF_DURATION` | `10` | test duration in seconds (`-t`) per direction |
| `IPERF_OMIT` | `2` | seconds to omit at the start (`-O`) so TCP slow-start isn't averaged in |
| `INTERVAL_SECONDS` | `3600` | seconds between test cycles |
| `HTTP_PORT` | `8080` | HTTP listen port |

The download value uses `iperf3 -R` (reverse / server→client); upload uses a forward test.
Each cycle logs throughput plus CPU usage as **cores used / available** (iperf3's CPU%
is summed across threads, so e.g. `1.6/6 cores`). That tells you where the bottleneck is.

## Tuning for 10G in a Proxmox VM

Filling a 10G line over the internet needs many parallel streams (`-P 16`), which makes
the test heavy. If you're not hitting line rate, read the cores figure in the logs:

- **Near all cores used** (e.g. `5.5/6 cores`) → CPU-bound. Give the VM more/faster vCPUs;
  set CPU type to `host`.
- **Only a fraction used** (e.g. `1.6/6 cores`) but still slow → the bottleneck is the
  network path, almost always the **single virtio NIC queue** serializing softirqs on one
  core. Fixes, in order:
  1. VM → Network Device → **Multiqueue = vCPU count** (then reboot, or
     `ethtool -L <iface> combined <N>` in the guest).
  2. Set **firewall = 0** on the NIC if you don't need it (per-packet host overhead).
  3. Prefer an **LXC container** over a full VM — it skips the virtio layer entirely and
     gets closest to bare-metal throughput.

A low-power host CPU (e.g. an i5-8500T at 2.1 GHz) may simply cap a software speedtest
below line rate; the line is fine, the measuring machine is the limit.

## Dashboard integration

Point the dashboard's speedtest config at this collector and select the iperf parser:

```
NUXT_SPEEDTEST_SOURCE=iperf
NUXT_SPEEDTESTS_JSON=[{"host":"<collector-host>","port":8080,"provider":"Init7"}]
```
