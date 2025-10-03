import os

from prometheus_client import Gauge, start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY
import json
import http.client
import ssl
import time

def get_macaroon_hex():
    if os.environ.get("ADMIN_MACAROON_HEX"):
        return os.environ.get("ADMIN_MACAROON_HEX")
    try:
        with open("/macaroon.hex", "r") as f:
            return f.read().strip()
    except Exception:
        print("Unable to read /macaroon.hex - abort")
        exit(1)

# hard-coded deterministic lnd credentials
ADMIN_MACAROON_HEX = get_macaroon_hex()
# Don't worry about lnd's self-signed certificates
INSECURE_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
INSECURE_CONTEXT.check_hostname = False
INSECURE_CONTEXT.verify_mode = ssl.CERT_NONE
LND_HOST = os.environ.get("LND_HOST", "localhost")
LND_REST_PORT = os.environ.get("LND_REST_PORT", "8080")

class LND:
    def __init__(self):
        self.conn = http.client.HTTPSConnection(
            host=LND_HOST, port=LND_REST_PORT, timeout=5, context=INSECURE_CONTEXT
        )

    def get(self, uri):
        while True:
            try:
                self.conn.request(
                    method="GET",
                    url=uri,
                    headers={"Grpc-Metadata-macaroon": ADMIN_MACAROON_HEX, "Connection": "close"},
                )
                return self.conn.getresponse().read().decode("utf8")
            except Exception:
                time.sleep(1)

    # decode response looking for result, result can be key.path
    def parse(self, endpoint, result):
        response = self.get(endpoint)
        data = json.loads(response)
        for key in result.split("."):
            data = data[key]
        return data

# Port where prometheus server will scrape metrics data
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9332"))

# LND REST data to scrape. Expressed as labeled REST separated by spaces
# [Lightning Labs Endpoint reference](https://lightning.engineering/api-docs/api/lnd/rest-endpoints/)
# <name of metric> = parse("<REST endpoint>","<key path of REST response>")
METRICS = os.environ.get(
    "METRICS",
'''
lnd_balance_channels=parse("/v1/balance/channels","balance")
lnd_local_balance_channels=parse("/v1/balance/channels","local_balance.sat")
lnd_remote_balance_channels=parse("/v1/balance/channels","remote_balance.sat")
lnd_peers=parse("/v1/getinfo","num_peers")
'''
)

lnd = LND()

class HTLCCollector:
    def collect(self):
        resp = lnd.get("/v1/channels")
        data = json.loads(resp)

        g = GaugeMetricFamily("pending_htlcs", "pending HTLCs", labels=["scid"])
        for channel in data["channels"]:
            chan_id = int(channel["chan_id"])
            short_id = f"{(chan_id>>40)&0xFFFFFF}x{(chan_id>>16)&0xFFFFFF}x{chan_id&0xFFFF}"
            g.add_metric([short_id], len(channel["pending_htlcs"]))
        yield g

class FailedPaymentsCollector:
    def collect(self):
        resp = lnd.get("/v1/payments?include_incomplete=true")
        data = json.loads(resp)

        failed_count = sum(1 for payment in data["payments"] if payment.get("status") == "FAILED")
        g = GaugeMetricFamily("failed_payments", "Number of payments with status FAILED")
        g.add_metric([], failed_count)
        yield g

# Create closure outside the loop
def make_metric_function(cmd):
    try:
        return lambda: eval(f"lnd.{cmd}")
    except Exception:
        return None

# Parse RPC queries into metrics
commands = METRICS.split(" ")
for labeled_cmd in commands:
    if "=" not in labeled_cmd:
        continue
    label, cmd = labeled_cmd.strip().split("=")
    if cmd == "PENDING_HTLCS":
        REGISTRY.register(HTLCCollector())
    elif cmd == "FAILED_PAYMENTS":
        REGISTRY.register(FailedPaymentsCollector())
    else:
        metric = Gauge(label, cmd)
        metric.set_function(make_metric_function(cmd))
    print(f"Metric created: {labeled_cmd}")


# Start the server
server, thread = start_http_server(METRICS_PORT)

print(f"Server: {server}")
print(f"Thread: {thread}")

# Keep alive by waiting for endless loop to end
thread.join()
server.shutdown()
