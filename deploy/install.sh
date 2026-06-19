#!/bin/sh
# Install the iperf3 collector as a systemd service (for a bare host / LXC, no Docker).
# Run as root from a checkout of this repo:  sudo deploy/install.sh
set -e

DIR=$(cd "$(dirname "$0")/.." && pwd)   # repo root

echo "Installing dependencies (iperf3, python3)..."
apt-get update
apt-get install -y --no-install-recommends iperf3 python3

echo "Installing systemd unit (collector at $DIR)..."
sed "s#/opt/iperf-speedtest#$DIR#g" "$DIR/deploy/iperf-speedtest.service" \
    > /etc/systemd/system/iperf-speedtest.service

systemctl daemon-reload
systemctl enable --now iperf-speedtest

echo
echo "Done. iperf3 version:"; iperf3 --version | head -1
echo "Follow logs with:  journalctl -u iperf-speedtest -f"
