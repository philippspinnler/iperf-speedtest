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
→ {
    "download": 9450.21, "upload": 9380.14,      # Mbps
    "download_gbps": 9.5, "upload_gbps": 9.4,    # Gbps, rounded to 1 decimal
    "timestamp": "2026-06-19T10:30:00+00:00"
  }
```

`download`/`upload` are in **Mbps**; `download_gbps`/`upload_gbps` are the same values in
Gbps pre-rounded to one decimal (handy for dashboards that can't cap decimals, e.g.
Homepage). Returns `503 {}` until the first test completes. The path mirrors
speedtest-tracker so it drops into existing tooling unchanged.

## Run

```sh
docker compose up -d --build
curl http://localhost:8080/api/speedtest/latest
```

## ⚠️ Bandwidth warning

Init7 is up to **25 Gbit/s symmetric**. With `-P 16`, each test direction can move several
GB. By default the collector runs **once a day** (at a random time in the 02:00–05:00
window) to stay light on the shared test server, but a single run still transfers a fair
amount of data. **Only run this on an unmetered connection.** Tune `IPERF_DURATION` and the
schedule window to control usage.

## Configuration (environment variables)

| Variable | Default | Description |
| --- | --- | --- |
| `IPERF_HOST` | `speedtest.init7.net` | iperf3 server hostname |
| `IPERF_PORT` | `5202` | iperf3 server port |
| `IPERF_PARALLEL` | `16` | parallel streams (`-P`). Over the internet each TCP flow is rate-limited, so you need many streams to fill a 10G line (Init7 recommends 16). iperf3 ≥3.16 gives each stream its own thread, so a high count can saturate the CPU — if a run logs ~100% CPU, add vCPUs rather than lowering this. |
| `IPERF_DURATION` | `10` | test duration in seconds (`-t`) per direction |
| `IPERF_OMIT` | `2` | seconds to omit at the start (`-O`) so TCP slow-start isn't averaged in |
| `TZ` | `Europe/Zurich` | local timezone for the schedule window (needs OS tzdata) |
| `DAILY_WINDOW_START_HOUR` | `2` | earliest local hour for the daily run |
| `DAILY_WINDOW_END_HOUR` | `5` | latest local hour (exclusive) for the daily run |
| `RUN_ON_START` | `true` | also run one test on startup so the dashboard isn't empty after a (re)deploy |
| `HTTP_PORT` | `8080` | HTTP listen port |

The collector runs once per day at a random time within `[DAILY_WINDOW_START_HOUR,
DAILY_WINDOW_END_HOUR)` local time, plus once on startup (unless `RUN_ON_START=false`).

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

## Deploy natively in an LXC / bare host (alternative)

The Docker image above is the recommended deployment. If you'd rather run without Docker
(e.g. in a Proxmox LXC, which shares the host kernel and skips a VM's emulated virtio NIC),
you can run the collector directly. Note: in practice this usually does **not** beat the
Docker container — on a fast line the ceiling is typically the host uplink or the upstream
test server, not the local virtualization layer. Also mind the distro's iperf3 version
(Debian 12 ships the old single-threaded 3.12; the Docker image uses 3.17).

On the Proxmox host, create an unprivileged Debian LXC bound to your bridge, give it
several cores, and start it. Then inside the container:

```sh
apt-get update && apt-get install -y git
git clone https://github.com/philippspinnler/iperf-speedtest /opt/iperf-speedtest
/opt/iperf-speedtest/deploy/install.sh          # installs iperf3 + a systemd service
journalctl -u iperf-speedtest -f                # watch the first cycle
```

It serves on `:8089` (edit `HTTP_PORT` / the Init7 settings in
`/etc/systemd/system/iperf-speedtest.service`, then `systemctl restart iperf-speedtest`).
Update later with `git -C /opt/iperf-speedtest pull && systemctl restart iperf-speedtest`.
Point the dashboard's `NUXT_SPEEDTESTS_JSON` host at the LXC's IP, port `8089`.

## Dashboard integration

Point the dashboard's speedtest config at this collector and select the iperf parser:

```
NUXT_SPEEDTEST_SOURCE=iperf
NUXT_SPEEDTESTS_JSON=[{"host":"<collector-host>","port":8080,"provider":"Init7"}]
```
