# Alpine ships iperf3 >= 3.17, whose multi-threaded receiver makes -P 16 accurate
# on fast (multi-Gbit) fiber. Older iperf3 (e.g. Debian Bookworm 3.12) caps
# multi-stream throughput well below line rate.
FROM alpine:3.20

# tzdata lets Python's zoneinfo resolve TZ (e.g. Europe/Zurich) for the daily schedule.
RUN apk add --no-cache iperf3 python3 tzdata

COPY collector.py /collector.py
COPY index.html /index.html

VOLUME ["/data"]
EXPOSE 8080

CMD ["python3", "/collector.py"]
