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
| `IPERF_PARALLEL` | `16` | parallel streams (`-P`) |
| `IPERF_DURATION` | `5` | test duration in seconds (`-t`) per direction |
| `INTERVAL_SECONDS` | `3600` | seconds between test cycles |
| `HTTP_PORT` | `8080` | HTTP listen port |

The download value uses `iperf3 -R` (reverse / server→client); upload uses a forward test.

## Dashboard integration

Point the dashboard's speedtest config at this collector and select the iperf parser:

```
NUXT_SPEEDTEST_SOURCE=iperf
NUXT_SPEEDTESTS_JSON=[{"host":"<collector-host>","port":8080,"provider":"Init7"}]
```
