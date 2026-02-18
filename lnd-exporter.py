import os

from prometheus_client import Gauge, make_wsgi_app
from prometheus_client.core import GaugeMetricFamily, REGISTRY
import json
import http.client
import ssl
import time
import threading
from wsgiref.simple_server import make_server


def get_macaroon_hex():
    if os.environ.get("ADMIN_MACAROON_HEX"):
        return os.environ.get("ADMIN_MACAROON_HEX")
    try:
        with open("/macaroon.hex", "r") as f:
            return f.read().strip()
    except Exception:
        print("Unable to read /macaroon.hex - abort")
        raise SystemExit(1)


# hard-coded deterministic lnd credentials
ADMIN_MACAROON_HEX = get_macaroon_hex()

# Don't worry about lnd's self-signed certificates
INSECURE_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
INSECURE_CONTEXT.check_hostname = False
INSECURE_CONTEXT.verify_mode = ssl.CERT_NONE

LND_HOST = os.environ.get("LND_HOST", "localhost")
LND_REST_PORT = int(os.environ.get("LND_REST_PORT", "8080"))

# Port where prometheus server will expose metrics
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9332"))

# LND REST data to scrape. Expressed as labeled REST separated by spaces/newlines
METRICS = os.environ.get(
    "METRICS",
    """
lnd_balance_channels=parse("/v1/balance/channels","balance")
lnd_local_balance_channels=parse("/v1/balance/channels","local_balance.sat")
lnd_remote_balance_channels=parse("/v1/balance/channels","remote_balance.sat")
lnd_peers=parse("/v1/getinfo","num_peers")
""",
)

# Heartbeat metric to support liveness/freshness checks
LAST_SUCCESS = Gauge(
    "lnd_exporter_last_success_timestamp",
    "Unix timestamp of last successful LND data fetch by the exporter",
)


class LND:
    """
    Minimal REST client for LND.
      - No infinite retry loops (bounded retries)
      - Recreate HTTPSConnection on each request (avoids stale sockets)
      - Hard timeout on the request path (via HTTPSConnection timeout)
    """

    def __init__(self):
        self.timeout = float(os.environ.get("LND_REST_TIMEOUT", "60"))
        self.retries = int(os.environ.get("LND_REST_RETRIES", "1"))
        self.retry_sleep = float(os.environ.get("LND_REST_RETRY_SLEEP", "10"))

    def _new_conn(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(
            host=LND_HOST,
            port=LND_REST_PORT,
            timeout=self.timeout,
            context=INSECURE_CONTEXT,
        )

    def get(self, uri: str) -> str:
        last_exc: Exception | None = None
        for _ in range(self.retries):
            conn = self._new_conn()
            try:
                conn.request(
                    method="GET",
                    url=uri,
                    headers={
                        "Grpc-Metadata-macaroon": ADMIN_MACAROON_HEX,
                        "Connection": "close",
                    },
                )
                resp = conn.getresponse()
                body = resp.read().decode("utf8")
                if resp.status >= 400:
                    raise RuntimeError(f"LND HTTP {resp.status} for {uri}: {body[:200]}")
                # Mark liveness only on successful HTTP fetch
                LAST_SUCCESS.set(time.time())
                return body
            except Exception as e:
                last_exc = e
                time.sleep(self.retry_sleep)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        raise RuntimeError(f"LND request failed after retries: {uri}") from last_exc

    def parse(self, endpoint: str, result: str):
        response = self.get(endpoint)
        data = json.loads(response)
        for key in result.split("."):
            data = data[key]
        return data


lnd = LND()


class HTLCCollector:
    def collect(self):
        resp = lnd.get("/v1/channels")
        data = json.loads(resp)

        g = GaugeMetricFamily("pending_htlcs", "pending HTLCs", labels=["scid"])
        for channel in data.get("channels", []):
            chan_id = int(channel["chan_id"])
            short_id = f"{(chan_id >> 40) & 0xFFFFFF}x{(chan_id >> 16) & 0xFFFFFF}x{chan_id & 0xFFFF}"
            g.add_metric([short_id], len(channel.get("pending_htlcs", [])))
        yield g


class FailedPaymentsCollector:
    def collect(self):
        resp = lnd.get("/v1/payments?include_incomplete=true")
        data = json.loads(resp)

        failed_count = sum(1 for payment in data.get("payments", []) if payment.get("status") == "FAILED")
        g = GaugeMetricFamily("failed_payments", "Number of payments with status FAILED")
        g.add_metric([], failed_count)
        yield g


# Create closure outside the loop
def make_metric_function(cmd):
    try:
        return lambda: eval(f"lnd.{cmd}")
    except Exception:
        return None


def parse_metrics_spec(spec: str) -> list[tuple[str, str]]:
    lines = []
    for raw in spec.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        # allow spaces too
        for token in s.split():
            if token:
                lines.append(token)
    out = []
    for labeled_cmd in lines:
        if "=" not in labeled_cmd:
            continue
        label, cmd = labeled_cmd.strip().split("=", 1)
        out.append((label.strip(), cmd.strip()))
    return out


# Register metrics
for label, cmd in parse_metrics_spec(METRICS):
    if cmd == "PENDING_HTLCS":
        REGISTRY.register(HTLCCollector())
    elif cmd == "FAILED_PAYMENTS":
        REGISTRY.register(FailedPaymentsCollector())
    else:
        metric = Gauge(label, cmd)
        fn = make_metric_function(cmd)
        if fn is None:
            # Fail fast rather than run half-broken and wedge later
            raise RuntimeError(f"Unsupported metric command: {label}={cmd}")
        metric.set_function(fn)
    print(f"Metric created: {label}={cmd}")


def serve_metrics():
    """
    Run metrics server in its own daemon thread using a known-simple WSGI server.
    This avoids reliance on start_http_server return values and reduces deadlock risk.
    """
    app = make_wsgi_app()
    httpd = make_server("", METRICS_PORT, app)
    httpd.serve_forever()


threading.Thread(target=serve_metrics, daemon=True).start()

# Keep the main thread alive forever.
# Do not join the server thread; joining can mask deadlocks and prevents watchdog behavior.
while True:
    time.sleep(60)
