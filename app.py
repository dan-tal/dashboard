import ipaddress
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import docker
from flask import Flask, jsonify, request

HOST_IP = os.environ.get("HOST_IP", "192.168.1.100")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "history.db")
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")
ROOTFS = os.environ.get("ROOTFS", "/rootfs")
HOSTSYS_ROOT = os.environ.get("HOSTSYS_ROOT", "/hostsys")
THERMAL_ROOT = os.path.join(HOSTSYS_ROOT, "class/thermal")
NET_ROOT = os.path.join(HOSTSYS_ROOT, "class/net")
# PID 1's netns is the host's; /proc/net inside the container only shows the container's own.
CONNTRACK_PATH = os.path.join(ROOTFS, "proc/1/net/nf_conntrack")
LAN_NET = ipaddress.ip_network(os.environ.get("LAN_SUBNET") or "192.168.1.0/24", strict=False)
# class/thermal and class/net are symlink farms into devices/... , so the
# container needs the whole host /sys tree mounted, not just these subdirs.

SAMPLE_INTERVAL = 15  # seconds
RETENTION_HOURS = 24 * 35  # ~5 weeks, comfortably covers the 30-day range
ALERT_SUSTAIN_SAMPLES = 4  # ~60s at SAMPLE_INTERVAL=15s, avoids flagging brief spikes

ALERT_THRESHOLDS = {"cpu": 90, "mem": 90, "disk": 90, "temp": 80}

HOST_RE = re.compile(r"Host\(`([^`]+)`\)")
VIRTUAL_IFACE_RE = re.compile(r"^(lo|docker\d*|br-|veth|tun|tap|virbr|vnet|wg)")

SAMPLE_COLUMNS = [
    "ts", "cpu", "mem", "disk", "temp", "load1", "load5", "load15",
    "rx_bps", "tx_bps", "dio_read_bps", "dio_write_bps", "lan_bps", "net_bps",
]

db_lock = threading.Lock()
docker_client = docker.DockerClient(base_url="unix://var/run/docker.sock")
recent_samples = deque(maxlen=ALERT_SUSTAIN_SAMPLES)
samples_lock = threading.Lock()

# ---------------------------------------------------------------- storage --
# One persistent connection, reused for the app's lifetime: sqlite3.connect()
# plus the schema/migration check ran on *every* query before (a few ms each,
# but multiplied across every poll from every open tab). WAL + synchronous=
# NORMAL trade a small durability margin (a lost write on an OS crash, not on
# a process crash) for readers never blocking on the writer thread.

def _init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, disk REAL, temp REAL, "
        "load1 REAL, load5 REAL, load15 REAL)"
    )
    for col in ("rx_bps", "tx_bps", "dio_read_bps", "dio_write_bps", "lan_bps", "net_bps"):
        try:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # column already exists from a previous run
    conn.commit()
    return conn


_db = _init_db()


def save_sample(sample):
    with db_lock, _db as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO samples ({','.join(SAMPLE_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(SAMPLE_COLUMNS))})",
            tuple(sample[k] for k in SAMPLE_COLUMNS),
        )
        cutoff = sample["ts"] - RETENTION_HOURS * 3600
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))


def read_history(start_ts, end_ts, target_points=300):
    span = max(1, end_ts - start_ts)
    bucket = max(SAMPLE_INTERVAL, span // target_points)
    metrics = SAMPLE_COLUMNS[1:]  # everything but ts
    select_cols = ", ".join(f"AVG({c})" for c in metrics)
    with db_lock, _db as conn:
        rows = conn.execute(
            f"SELECT MIN(ts), {select_cols} FROM samples "
            f"WHERE ts >= ? AND ts <= ? GROUP BY (ts / ?) ORDER BY 1",
            (start_ts, end_ts, bucket),
        ).fetchall()
        earliest = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    points = [dict(zip(SAMPLE_COLUMNS, row)) for row in rows]
    return points, earliest


def _earliest_sample_ts():
    with db_lock, _db as conn:
        return conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]


def disk_forecast():
    """Least-squares slope of disk% over all retained history, extrapolated
    to a 'days until full' estimate. Computed as SQL aggregates (not a Python
    loop over rows) so it stays cheap even at full 35-day retention."""
    with db_lock, _db as conn:
        row = conn.execute(
            # ts is ~1.79e9; SUM(ts*ts) as plain INTEGER overflows sqlite's int64
            # past a handful of rows (1.79e9^2 * a few rows already exceeds it) -
            # the REAL cast forces float arithmetic, which has ample range for this.
            "SELECT COUNT(*), SUM(ts), SUM(disk), SUM(ts*disk), SUM(CAST(ts AS REAL)*ts), MIN(ts), MAX(ts) "
            "FROM samples WHERE disk IS NOT NULL"
        ).fetchone()
    n, sum_x, sum_y, sum_xy, sum_xx, min_ts, max_ts = row
    if not n or n < 10 or not min_ts or (max_ts - min_ts) < 86400:
        # under a day of history, noise (and any mid-session formula changes,
        # like the disk% fix earlier today) swamps any real trend
        return {"status": "insufficient_data"}

    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return {"status": "insufficient_data"}
    slope_per_day = (n * sum_xy - sum_x * sum_y) / denom * 86400  # %/day

    with latest_lock:
        sample = latest["sample"]
    current_pct = sample["disk"] if sample else None
    if current_pct is None:
        return {"status": "insufficient_data"}

    if slope_per_day <= 0.005 or (100 - current_pct) / slope_per_day > 3650:
        return {"status": "stable", "slope_pct_per_day": round(slope_per_day, 4)}

    days_left = (100 - current_pct) / slope_per_day
    eta = datetime.now(timezone.utc) + timedelta(days=days_left)
    return {
        "status": "growing",
        "days_left": round(days_left, 1),
        "eta_date": eta.strftime("%Y-%m-%d"),
        "slope_pct_per_day": round(slope_per_day, 4),
    }


def read_summary(start_ts, end_ts):
    """Avg/min/max for gauge metrics, and totals (avg rate * duration) for
    the rate metrics - computed from raw samples, not the downsampled buckets,
    so short spikes aren't smoothed away."""
    gauge_cols = ["cpu", "mem", "disk", "temp"]
    rate_cols = ["rx_bps", "tx_bps", "dio_read_bps", "dio_write_bps", "lan_bps", "net_bps"]
    select = ", ".join(f"AVG({c}), MIN({c}), MAX({c})" for c in gauge_cols)
    select += ", " + ", ".join(f"AVG({c}), MAX({c})" for c in rate_cols)
    with db_lock, _db as conn:
        row = conn.execute(
            f"SELECT COUNT(*), {select} FROM samples WHERE ts >= ? AND ts <= ?",
            (start_ts, end_ts),
        ).fetchone()
    count = row[0]
    if not count:
        return None
    duration = max(0, end_ts - start_ts)
    gauges = {}
    for i, col in enumerate(gauge_cols):
        avg, lo, hi = row[1 + i * 3: 4 + i * 3]
        gauges[col] = {"avg": avg, "min": lo, "max": hi} if avg is not None else None
    rate_offset = 1 + len(gauge_cols) * 3
    rates = {}
    for i, col in enumerate(rate_cols):
        rates[col] = row[rate_offset + i * 2: rate_offset + i * 2 + 2]  # (avg, max)

    def totals(names):
        return {f"{n}_total": (rates[c][0] or 0) * duration for n, c in names} | \
               {f"{n}_max": rates[c][1] or 0 for n, c in names}

    return {
        "cpu": gauges["cpu"], "mem": gauges["mem"], "disk": gauges["disk"], "temp": gauges["temp"],
        "net": totals([("rx", "rx_bps"), ("tx", "tx_bps")]) if rates["rx_bps"][0] is not None else None,
        "dio": totals([("read", "dio_read_bps"), ("write", "dio_write_bps")])
        if rates["dio_read_bps"][0] is not None else None,
        "scope": totals([("lan", "lan_bps"), ("net", "net_bps")])
        if rates["lan_bps"][0] is not None else None,
    }


# ------------------------------------------------------------- host stats --

_prev_cpu = None


def read_cpu_pct():
    global _prev_cpu
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(v) for v in parts[1:]]
    idle = vals[3] + vals[4]  # idle + iowait
    total = sum(vals)
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return 0.0
    prev_idle, prev_total = _prev_cpu
    _prev_cpu = (idle, total)
    d_idle = idle - prev_idle
    d_total = total - prev_total
    if d_total <= 0:
        return 0.0
    return round((1 - d_idle / d_total) * 100, 1)


def read_mem_pct():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            info[key] = int(rest.strip().split()[0])
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    if total == 0:
        return 0.0, 0, 0
    used = total - avail
    return round(used / total * 100, 1), used // 1024, total // 1024  # MiB


def read_disk_pct():
    st = os.statvfs(ROOTFS)
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bfree      # all free blocks, including root's ~5% ext4 reserve
    avail = st.f_frsize * st.f_bavail    # free blocks available to non-root - what `df` calls Avail
    used = total - free
    denom = used + avail
    if denom == 0:
        return 0.0, 0, 0
    # `df`'s Use% is used/(used+avail), not used/total - the reserve is
    # excluded from the denominator, which is why df reads a bit higher
    # than a naive used/total calc would.
    return round(used / denom * 100, 1), used, total


def read_temp_c():
    best = None
    try:
        zones = sorted(os.listdir(THERMAL_ROOT))
    except OSError:
        return None
    for zone in zones:
        if not zone.startswith("thermal_zone"):
            continue
        type_path = os.path.join(THERMAL_ROOT, zone, "type")
        temp_path = os.path.join(THERMAL_ROOT, zone, "temp")
        try:
            with open(type_path) as f:
                ztype = f.read().strip()
            with open(temp_path) as f:
                milli = int(f.read().strip())
        except (OSError, ValueError):
            continue
        celsius = milli / 1000
        if "pkg" in ztype.lower():
            return round(celsius, 1)
        if best is None:
            best = celsius
    return round(best, 1) if best is not None else None


def read_net_bytes():
    """Cumulative rx/tx bytes for real (non-virtual) host interfaces.

    The container's own /proc/net/dev is per-netns and only shows its veth peer,
    so this reads counters straight from the bind-mounted host sysfs instead
    (/hostsys is the whole host /sys tree, unnamespaced by the bind mount).
    """
    rx = tx = 0
    try:
        ifaces = os.listdir(NET_ROOT)
    except OSError:
        return None
    for iface in ifaces:
        if VIRTUAL_IFACE_RE.match(iface):
            continue
        try:
            with open(os.path.join(NET_ROOT, iface, "statistics", "rx_bytes")) as f:
                rx += int(f.read().strip())
            with open(os.path.join(NET_ROOT, iface, "statistics", "tx_bytes")) as f:
                tx += int(f.read().strip())
        except (OSError, ValueError):
            continue
    return rx, tx


DISK_DEV_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)$")


def read_disk_io_bytes():
    """Cumulative sectors read/written for whole-disk devices (not partitions)."""
    read_sectors = write_sectors = 0
    try:
        with open("/proc/diskstats") as f:
            lines = f.readlines()
    except OSError:
        return None
    found = False
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        name = parts[2]
        if not DISK_DEV_RE.match(name):
            continue
        found = True
        read_sectors += int(parts[5])
        write_sectors += int(parts[9])
    if not found:
        return None
    return read_sectors * 512, write_sectors * 512


_prev_net = None
_prev_dio = None
_prev_net_ts = None
_prev_dio_ts = None


def read_net_bps():
    global _prev_net, _prev_net_ts
    now = time.time()
    cur = read_net_bytes()
    if cur is None:
        return None, None
    if _prev_net is None:
        _prev_net, _prev_net_ts = cur, now
        return 0.0, 0.0
    dt = now - _prev_net_ts
    rx_bps = (cur[0] - _prev_net[0]) / dt if dt > 0 else 0.0
    tx_bps = (cur[1] - _prev_net[1]) / dt if dt > 0 else 0.0
    _prev_net, _prev_net_ts = cur, now
    return max(0.0, round(rx_bps, 1)), max(0.0, round(tx_bps, 1))


def read_dio_bps():
    global _prev_dio, _prev_dio_ts
    now = time.time()
    cur = read_disk_io_bytes()
    if cur is None:
        return None, None
    if _prev_dio is None:
        _prev_dio, _prev_dio_ts = cur, now
        return 0.0, 0.0
    dt = now - _prev_dio_ts
    r_bps = (cur[0] - _prev_dio[0]) / dt if dt > 0 else 0.0
    w_bps = (cur[1] - _prev_dio[1]) / dt if dt > 0 else 0.0
    _prev_dio, _prev_dio_ts = cur, now
    return max(0.0, round(r_bps, 1)), max(0.0, round(w_bps, 1))


CT_TUPLE_RE = re.compile(r"src=(\S+) dst=(\S+)(?: sport=(\d+) dport=(\d+))?")
CT_BYTES_RE = re.compile(r"\bbytes=(\d+)")
_prev_flows = None
_prev_flows_ts = None


def flow_scope(src, dst):
    """'net' if either end is a public address; 'lan' if both ends are on the
    LAN subnet; None otherwise (container<->container, loopback, or a
    container talking to the host's own LAN IP - none of that touches the NIC)."""
    try:
        a, b = ipaddress.ip_address(src), ipaddress.ip_address(dst)
    except ValueError:
        return None
    if any(ip.is_global and not ip.is_multicast for ip in (a, b)):
        return "net"
    if a in LAN_NET and b in LAN_NET:
        return "lan"
    return None


def read_scope_bps():
    """(lan_bps, net_bps) from per-flow byte deltas in the host conntrack table.
    Needs net.netfilter.nf_conntrack_acct=1 on the host - without it conntrack
    keeps no byte counters, and this returns (None, None)."""
    global _prev_flows, _prev_flows_ts
    now = time.time()
    try:
        with open(CONNTRACK_PATH) as f:
            lines = f.readlines()
    except OSError:
        return None, None
    cur = {}
    saw_bytes = False
    for line in lines:
        counted = CT_BYTES_RE.findall(line)
        if not counted:
            continue
        saw_bytes = True
        m = CT_TUPLE_RE.search(line)
        scope = flow_scope(m.group(1), m.group(2)) if m else None
        if scope:
            cur[(line.split(None, 3)[2],) + m.groups()] = (scope, sum(int(b) for b in counted))
    if lines and not saw_bytes:
        _prev_flows = None
        return None, None
    prev, prev_ts = _prev_flows, _prev_flows_ts
    _prev_flows, _prev_flows_ts = cur, now
    if prev is None or now <= prev_ts:
        return None, None
    delta = {"lan": 0, "net": 0}
    for key, (scope, total) in cur.items():
        before = prev.get(key)
        delta[scope] += total - before[1] if before and total >= before[1] else total
    dt = now - prev_ts
    return round(delta["lan"] / dt, 1), round(delta["net"] / dt, 1)


def sampler_loop():
    while True:
        try:
            cpu = read_cpu_pct()
            mem_pct, mem_used, mem_total = read_mem_pct()
            disk_pct, disk_used, disk_total = read_disk_pct()
            temp = read_temp_c()
            load1, load5, load15 = os.getloadavg()
            rx_bps, tx_bps = read_net_bps()
            dio_r_bps, dio_w_bps = read_dio_bps()
            lan_bps, net_bps = read_scope_bps()
            sample = {
                "ts": int(time.time()),
                "cpu": cpu, "mem": mem_pct, "disk": disk_pct, "temp": temp,
                "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
                "rx_bps": rx_bps, "tx_bps": tx_bps,
                "dio_read_bps": dio_r_bps, "dio_write_bps": dio_w_bps,
                "lan_bps": lan_bps, "net_bps": net_bps,
            }
            sample["mem_used_mib"] = mem_used
            sample["mem_total_mib"] = mem_total
            sample["disk_used_bytes"] = disk_used
            sample["disk_total_bytes"] = disk_total
            with latest_lock:
                latest["sample"] = sample
            with samples_lock:
                recent_samples.append(sample)
            save_sample({k: sample[k] for k in SAMPLE_COLUMNS})
        except Exception as exc:  # pragma: no cover - defensive, host I/O boundary
            print(f"sampler error: {exc}")
        time.sleep(SAMPLE_INTERVAL)


latest_lock = threading.Lock()
latest = {"sample": None}


def host_alerts():
    """Threshold breaches sustained across every sample in the recent window,
    so a brief spike doesn't trigger a banner."""
    with samples_lock:
        snapshot = list(recent_samples)  # copy out - sampler_loop mutates the deque from another thread
    if len(snapshot) < ALERT_SUSTAIN_SAMPLES:
        return []
    alerts = []
    labels_ro = {"cpu": "CPU", "mem": "RAM", "disk": "SSD", "temp": "Temperatură"}
    for key, threshold in ALERT_THRESHOLDS.items():
        vals = [s[key] for s in snapshot if s.get(key) is not None]
        if len(vals) == len(snapshot) and all(v >= threshold for v in vals):
            unit = "°C" if key == "temp" else "%"
            alerts.append({
                "level": "critical",
                "text": f"{labels_ro[key]} peste {threshold}{unit} de {ALERT_SUSTAIN_SAMPLES * SAMPLE_INTERVAL}s (acum {vals[-1]:.0f}{unit})",
            })
    return alerts


# --------------------------------------------------------- container stats --

CONTAINER_STATS_INTERVAL = SAMPLE_INTERVAL
container_stats_lock = threading.Lock()
container_stats = {}  # name -> {"cpu_pct": float, "mem_mib": float}
_prev_container_cpu = {}  # name -> (total_usage, system_usage)


def sample_container_stats():
    try:
        containers = docker_client.containers.list()  # running only
    except Exception as exc:
        print(f"container_stats: list failed: {exc}")
        return
    seen = set()
    for c in containers:
        seen.add(c.name)
        try:
            raw = c.stats(stream=False, one_shot=True)
            cpu_usage = raw["cpu_stats"]["cpu_usage"]["total_usage"]
            sys_usage = raw["cpu_stats"].get("system_cpu_usage")
            online_cpus = raw["cpu_stats"].get("online_cpus") or len(
                raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1]
            )
            cpu_pct = None
            prev = _prev_container_cpu.get(c.name)
            if prev is not None and sys_usage is not None:
                d_cpu = cpu_usage - prev[0]
                d_sys = sys_usage - prev[1]
                if d_sys > 0:
                    cpu_pct = round(max(0.0, d_cpu / d_sys * online_cpus * 100), 1)
            if sys_usage is not None:
                _prev_container_cpu[c.name] = (cpu_usage, sys_usage)

            mem_stats = raw.get("memory_stats", {})
            mem_usage = mem_stats.get("usage")
            mem_mib = None
            if mem_usage is not None:
                cache = mem_stats.get("stats", {}).get("inactive_file", 0)
                mem_mib = round(max(0, mem_usage - cache) / 1024 / 1024, 1)

            with container_stats_lock:
                container_stats[c.name] = {"cpu_pct": cpu_pct, "mem_mib": mem_mib}
        except Exception as exc:
            print(f"container_stats: {c.name}: {exc}")
    with container_stats_lock:
        for name in list(container_stats):
            if name not in seen:
                container_stats.pop(name, None)
                _prev_container_cpu.pop(name, None)


def container_stats_loop():
    while True:
        sample_container_stats()
        time.sleep(CONTAINER_STATS_INTERVAL)


# ------------------------------------------------------------- containers --

def load_overrides():
    try:
        with open(OVERRIDES_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def container_urls(container, labels, overrides):
    """Returns (primary_url, extra_urls) - extra_urls holds any additional
    Host() rule on the same router (typically a public example.com alias
    alongside the .home one), so the UI can flag it separately."""
    if container.name in overrides:
        override = overrides[container.name]
        return (override, []) if override else (None, [])

    rule = labels.get(f"traefik.http.routers.{container.name}.rule", "")
    hosts = HOST_RE.findall(rule)
    if not hosts:
        # some setups name the router differently from the container; scan all rule labels
        for key, val in labels.items():
            if key.startswith("traefik.http.routers.") and key.endswith(".rule"):
                hosts = HOST_RE.findall(val)
                if hosts:
                    break
    if hosts:
        ordered = sorted(hosts, key=lambda h: 0 if h.endswith(".home") else 1)
        urls = [f"http://{h}" for h in ordered]
        return urls[0], urls[1:]

    ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
    for _, bindings in ports.items():
        if not bindings:
            continue
        for b in bindings:
            host_port = b.get("HostPort")
            if host_port:
                return f"http://{HOST_IP}:{host_port}", []
    return None, []


def list_containers():
    overrides = load_overrides()
    out = []
    for c in docker_client.containers.list(all=True):
        labels = c.labels or {}
        state = c.attrs.get("State", {})
        health = state.get("Health", {}).get("Status")
        started_at = state.get("StartedAt", "")
        started_ago = None
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", started_at)
        if m and c.status == "running":
            try:
                started = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                started_ago = int((datetime.now(timezone.utc) - started).total_seconds())
            except ValueError:
                pass

        # c.image.tags looks equivalent but is a *live* Docker API call per
        # container (~70ms each locally) - Config.Image is the same string,
        # already sitting in the .attrs this container came with for free.
        image = c.attrs.get("Config", {}).get("Image", "?")

        url, extra_urls = container_urls(c, labels, overrides)
        with container_stats_lock:
            cstats = container_stats.get(c.name, {})
        out.append({
            "name": c.name,
            "image": image,
            "status": c.status,
            "health": health,
            "url": url,
            "extra_urls": extra_urls,
            "uptime_s": started_ago,
            "cpu_pct": cstats.get("cpu_pct"),
            "mem_mib": cstats.get("mem_mib"),
        })
    out.sort(key=lambda x: x["name"])
    return out


# ------------------------------------------------------------------ flask --

app = Flask(__name__)


@app.get("/api/stats")
def api_stats():
    with latest_lock:
        sample = latest["sample"]
    return jsonify(sample or {})


@app.get("/api/history")
def api_history():
    now = int(time.time())
    from_ts = request.args.get("from", type=int)
    to_ts = request.args.get("to", type=int)
    if from_ts is not None and to_ts is not None:
        floor = now - RETENTION_HOURS * 3600
        start = max(from_ts, floor)
        end = min(to_ts, now)
    else:
        minutes = request.args.get("minutes", default=60, type=int)
        minutes = max(1, min(minutes, RETENTION_HOURS * 60))
        start, end = now - minutes * 60, now
    points, earliest = read_history(start, end) if end > start else ([], _earliest_sample_ts())
    summary = read_summary(start, end) if end > start else None
    return jsonify({
        "points": points, "earliest_ts": earliest,
        "range_start": start, "range_end": end, "summary": summary,
    })


@app.get("/api/containers")
def api_containers():
    try:
        return jsonify(list_containers())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/alerts")
def api_alerts():
    alerts = host_alerts()
    try:
        for c in list_containers():
            if c["status"] == "running" and c["health"] == "unhealthy":
                alerts.append({"level": "critical", "text": f"{c['name']}: nesănătos"})
            elif c["status"] in ("exited", "dead"):
                alerts.append({"level": "warning", "text": f"{c['name']}: {c['status']}"})
    except Exception as exc:
        print(f"api_alerts: {exc}")
    return jsonify(alerts)


@app.get("/api/disk-forecast")
def api_disk_forecast():
    try:
        return jsonify(disk_forecast())
    except Exception as exc:
        print(f"api_disk_forecast: {exc}")
        return jsonify({"status": "insufficient_data"})


@app.get("/manifest.json")
def manifest():
    return jsonify({
        "name": "Server Acasă", "short_name": "Server", "start_url": "/",
        "display": "standalone", "background_color": "#0d0d0d", "theme_color": "#2a78d6",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
    })


@app.get("/icon.svg")
def icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="20" fill="#2a78d6"/>'
        '<text x="50" y="66" font-size="56" text-anchor="middle" '
        'font-family="system-ui,sans-serif">\U0001F3E0</text></svg>'
    )
    return app.response_class(svg, mimetype="image/svg+xml")


@app.get("/")
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="ro">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Server Acasă</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<meta name="theme-color" content="#2a78d6">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --text: #0b0b0b;
  --text-2: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --border: rgba(11,11,11,0.10);
  --good: #0ca30c;
  --warning: #fab219;
  --serious: #ec835a;
  --critical: #d03b3b;
  --line: #2a78d6;
  --line-fill: rgba(42,120,214,0.12);
  --line2: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --text: #ffffff;
    --text-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --border: rgba(255,255,255,0.10);
    --line: #3987e5;
    --line-fill: rgba(57,135,229,0.16);
    --line2: #d95926;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 20px 16px 60px;
}
.wrap { max-width: 1080px; margin: 0 auto; }
header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 18px; flex-wrap: wrap; gap: 6px; }
header h1 { font-size: 1.15rem; margin: 0; font-weight: 600; }
header .clock { color: var(--text-2); font-variant-numeric: tabular-nums; font-size: 0.85rem; }
h2.section { font-size: 0.78rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 28px 0 10px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }

.alerts { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.alert-item {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 8px;
  font-size: 0.8rem; border: 1px solid; animation: alert-in .2s ease;
}
.alert-item.critical { background: rgba(208,59,59,0.12); border-color: var(--critical); color: var(--critical); }
.alert-item.warning { background: rgba(250,178,25,0.12); border-color: var(--warning); color: #a06800; }
@media (prefers-color-scheme: dark) { .alert-item.warning { color: var(--warning); } }
@keyframes alert-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
@media (max-width: 640px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
.tile { padding: 14px 16px; }
.tile .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.tile .value { font-size: 1.7rem; font-variant-numeric: tabular-nums; margin-top: 2px; }
.tile .value small { font-size: 0.9rem; color: var(--text-2); font-weight: 400; }
.bar { margin-top: 10px; height: 6px; border-radius: 3px; background: var(--grid); overflow: hidden; }
.bar > i { display: block; height: 100%; border-radius: 3px; transition: width .4s ease; }
.tile .sub { margin-top: 8px; font-size: 0.72rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.tile .forecast { margin-top: 4px; font-size: 0.72rem; font-variant-numeric: tabular-nums; }
.tile .forecast.soon { color: var(--warning); }
.tile .forecast.critical { color: var(--critical); }
.tile .forecast.calm { color: var(--muted); }

.range-toggle { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.range-toggle button {
  border: 1px solid var(--border); background: transparent; color: var(--text-2);
  padding: 4px 10px; border-radius: 999px; font-size: 0.75rem; cursor: pointer;
}
.range-toggle button.active { background: var(--line); border-color: var(--line); color: #fff; }
.range-toggle button.live.active { background: var(--critical); border-color: var(--critical); }
.range-toggle button.live.active::before {
  content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  background: #fff; margin-right: 5px; vertical-align: middle; animation: live-pulse 1.2s ease-in-out infinite;
}
@keyframes live-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }

.datepicker {
  display: none; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: 8px 0 4px; padding: 10px; font-size: 0.75rem; color: var(--text-2);
}
.datepicker.open { display: flex; }
.datepicker input {
  border: 1px solid var(--border); background: var(--page); color: var(--text);
  border-radius: 6px; padding: 4px 6px; font-size: 0.75rem; font-family: inherit;
}
.datepicker button.apply {
  border: none; background: var(--line); color: #fff; border-radius: 999px;
  padding: 4px 12px; font-size: 0.75rem; cursor: pointer;
}

.charts { display: grid; grid-template-columns: 1fr; gap: 10px; }
.chart-card { padding: 12px 14px 8px; }
.chart-card .chead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; gap: 8px; }
.chart-card .ctitle { font-size: 0.78rem; color: var(--text-2); font-weight: 600; }
.chart-card .cval { font-size: 0.78rem; color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.csummary { font-size: 0.71rem; color: var(--muted); font-variant-numeric: tabular-nums; margin-bottom: 6px; }
.legend { display: flex; gap: 12px; margin-bottom: 4px; }
.legend span { display: inline-flex; align-items: center; gap: 5px; font-size: 0.7rem; color: var(--text-2); }
.legend i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
svg.chart { width: 100%; height: 90px; display: block; overflow: visible; }
.chart .gridline { stroke: var(--grid); stroke-width: 1; }
.chart .area { fill: var(--line-fill); stroke: none; }
.chart .hoverline { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 2 2; opacity: 0; }
.tooltip {
  position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 8px; font-size: 0.72rem; color: var(--text); box-shadow: 0 2px 8px rgba(0,0,0,0.15);
  opacity: 0; transform: translate(-50%, -110%); white-space: nowrap;
}
.chart-wrap { position: relative; }
.chart .peakline { stroke: var(--text-2); stroke-width: 1; stroke-dasharray: 5 4; opacity: 0.55; vector-effect: non-scaling-stroke; }
.peak-label {
  position: absolute; right: 4px; pointer-events: none; font-size: 0.66rem; color: var(--text-2);
  font-variant-numeric: tabular-nums; background: var(--surface); padding: 0 4px; border-radius: 4px; opacity: 0.9;
}
.empty-state {
  height: 90px; display: none; align-items: center; justify-content: center; text-align: center;
  color: var(--muted); font-size: 0.75rem; line-height: 1.4; padding: 0 20px;
}
.empty-state.show { display: flex; }

.grid-containers { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
.ccard { padding: 12px 14px; }
.ccard .top { display: flex; align-items: center; gap: 8px; }
.dot-status { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.ccard .name { font-weight: 600; font-size: 0.92rem; }
.ccard .meta { margin-top: 4px; font-size: 0.72rem; color: var(--muted); }
.ccard .image { margin-top: 6px; font-size: 0.7rem; color: var(--text-2); font-family: ui-monospace, monospace; word-break: break-all; }
.ccard .usage { margin-top: 6px; font-size: 0.7rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.ccard .row2 { margin-top: 10px; display: flex; align-items: center; justify-content: space-between; }
.ccard a.open {
  font-size: 0.75rem; text-decoration: none; color: #fff;
  padding: 4px 10px; border-radius: 999px;
}
.ccard a.open.home { background: var(--good); }
.ccard a.open.public { background: var(--critical); }
.ccard .internal { font-size: 0.72rem; color: var(--muted); }
.ccard .extra-urls { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.ccard a.open-extra {
  font-size: 0.7rem; text-decoration: none;
  padding: 3px 8px; border-radius: 999px; border: 1px solid;
}
.ccard a.open-extra.home { color: var(--good); border-color: var(--good); }
.ccard a.open-extra.public { color: var(--critical); border-color: var(--critical); }
footer { text-align: center; color: var(--muted); font-size: 0.7rem; margin-top: 30px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🏠 Server Acasă</h1>
    <span class="clock" id="clock">--:--:--</span>
  </header>

  <div class="alerts" id="alerts"></div>

  <div class="tiles">
    <div class="card tile">
      <div class="label">CPU</div>
      <div class="value" id="t-cpu">--<small>%</small></div>
      <div class="bar"><i id="b-cpu" style="width:0%"></i></div>
      <div class="sub" id="s-cpu">load --</div>
    </div>
    <div class="card tile">
      <div class="label">RAM</div>
      <div class="value" id="t-mem">--<small>%</small></div>
      <div class="bar"><i id="b-mem" style="width:0%"></i></div>
      <div class="sub" id="s-mem">-- / --</div>
    </div>
    <div class="card tile">
      <div class="label">SSD</div>
      <div class="value" id="t-disk">--<small>%</small></div>
      <div class="bar"><i id="b-disk" style="width:0%"></i></div>
      <div class="sub" id="s-disk">-- / --</div>
      <div class="forecast" id="f-disk"></div>
    </div>
    <div class="card tile">
      <div class="label">Temperatură</div>
      <div class="value" id="t-temp">--<small>°C</small></div>
      <div class="bar"><i id="b-temp" style="width:0%"></i></div>
      <div class="sub" id="s-temp">CPU package</div>
    </div>
  </div>


  <h2 class="section" style="display:flex; justify-content:space-between; align-items:center;">
    <span>Istoric</span>
    <span class="range-toggle" id="range-toggle">
      <button data-min="2" data-live="1" class="live active">LIVE</button>
      <button data-min="5">5m</button>
      <button data-min="10">10m</button>
      <button data-min="30">30m</button>
      <button data-min="60">1h</button>
      <button data-min="360">6h</button>
      <button data-min="1440">24h</button>
      <button data-min="10080">7z</button>
      <button data-min="43200">30z</button>
      <button data-custom="1">📅 interval</button>
    </span>
  </h2>
  <div class="card datepicker" id="datepicker">
    <label>de la <input type="datetime-local" id="dp-from"></label>
    <label>până la <input type="datetime-local" id="dp-to"></label>
    <button class="apply" id="dp-apply">Aplică</button>
  </div>
  <div class="charts">
    <div class="card chart-card">
      <div class="chead"><span class="ctitle">CPU %</span><span class="cval" id="cv-cpu"></span></div>
      <div class="csummary" id="sum-cpu"></div>
      <div class="chart-wrap">
        <svg class="chart" id="chart-cpu" viewBox="0 0 700 90" preserveAspectRatio="none"></svg>
        <div class="tooltip" id="tip-cpu"></div>
        <div class="empty-state" id="empty-cpu"></div>
      </div>
    </div>
    <div class="card chart-card">
      <div class="chead"><span class="ctitle">RAM %</span><span class="cval" id="cv-mem"></span></div>
      <div class="csummary" id="sum-mem"></div>
      <div class="chart-wrap">
        <svg class="chart" id="chart-mem" viewBox="0 0 700 90" preserveAspectRatio="none"></svg>
        <div class="tooltip" id="tip-mem"></div>
        <div class="empty-state" id="empty-mem"></div>
      </div>
    </div>
    <div class="card chart-card">
      <div class="chead"><span class="ctitle">Temperatură °C</span><span class="cval" id="cv-temp"></span></div>
      <div class="csummary" id="sum-temp"></div>
      <div class="chart-wrap">
        <svg class="chart" id="chart-temp" viewBox="0 0 700 90" preserveAspectRatio="none"></svg>
        <div class="tooltip" id="tip-temp"></div>
        <div class="empty-state" id="empty-temp"></div>
      </div>
    </div>
    <div class="card chart-card">
      <div class="chead"><span class="ctitle">Rețea</span><span class="cval" id="cv-net"></span></div>
      <div class="csummary" id="sum-net"></div>
      <div class="legend" id="legend-net"></div>
      <div class="chart-wrap">
        <svg class="chart" id="chart-net" viewBox="0 0 700 90" preserveAspectRatio="none"></svg>
        <div class="tooltip" id="tip-net"></div>
        <div class="empty-state" id="empty-net"></div>
      </div>
    </div>
    <div class="card chart-card">
      <div class="chead"><span class="ctitle">Rețea · local vs internet</span><span class="cval" id="cv-scope"></span></div>
      <div class="csummary" id="sum-scope"></div>
      <div class="legend" id="legend-scope"></div>
      <div class="chart-wrap">
        <svg class="chart" id="chart-scope" viewBox="0 0 700 90" preserveAspectRatio="none"></svg>
        <div class="tooltip" id="tip-scope"></div>
        <div class="empty-state" id="empty-scope"></div>
      </div>
    </div>
    <div class="card chart-card">
      <div class="chead"><span class="ctitle">Disc I/O</span><span class="cval" id="cv-dio"></span></div>
      <div class="csummary" id="sum-dio"></div>
      <div class="legend" id="legend-dio"></div>
      <div class="chart-wrap">
        <svg class="chart" id="chart-dio" viewBox="0 0 700 90" preserveAspectRatio="none"></svg>
        <div class="tooltip" id="tip-dio"></div>
        <div class="empty-state" id="empty-dio"></div>
      </div>
    </div>
  </div>

  <h2 class="section">Containere</h2>
  <div class="grid-containers" id="containers"></div>

  <footer>actualizat automat · <span id="updated">--</span></footer>
</div>

<script>
const fmtBytes = (b) => {
  if (!b && b !== 0) return "--";
  const units = ["B","KB","MB","GB","TB"];
  let i = 0, v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + " " + units[i];
};

const fmtMiB = (m) => fmtBytes(m * 1024 * 1024);

function statusColor(pct) {
  if (pct >= 90) return "var(--critical)";
  if (pct >= 75) return "var(--serious)";
  if (pct >= 55) return "var(--warning)";
  return "var(--good)";
}
function tempColor(t) {
  if (t >= 80) return "var(--critical)";
  if (t >= 70) return "var(--serious)";
  if (t >= 60) return "var(--warning)";
  return "var(--good)";
}

function tick() {
  document.getElementById("clock").textContent = new Date().toLocaleTimeString("ro-RO");
}
setInterval(tick, 1000); tick();

async function refreshStats() {
  try {
    const r = await fetch("/api/stats");
    const s = await r.json();
    if (!s || s.cpu === undefined) return;

    document.getElementById("t-cpu").innerHTML = s.cpu.toFixed(0) + "<small>%</small>";
    document.getElementById("b-cpu").style.width = s.cpu + "%";
    document.getElementById("b-cpu").style.background = statusColor(s.cpu);
    document.getElementById("s-cpu").textContent =
      `load ${s.load1.toFixed(2)} · ${s.load5.toFixed(2)} · ${s.load15.toFixed(2)}`;

    document.getElementById("t-mem").innerHTML = s.mem.toFixed(0) + "<small>%</small>";
    document.getElementById("b-mem").style.width = s.mem + "%";
    document.getElementById("b-mem").style.background = statusColor(s.mem);
    document.getElementById("s-mem").textContent = `${fmtMiB(s.mem_used_mib)} / ${fmtMiB(s.mem_total_mib)}`;

    document.getElementById("t-disk").innerHTML = s.disk.toFixed(0) + "<small>%</small>";
    document.getElementById("b-disk").style.width = s.disk + "%";
    document.getElementById("b-disk").style.background = statusColor(s.disk);
    document.getElementById("s-disk").textContent = `${fmtBytes(s.disk_used_bytes)} / ${fmtBytes(s.disk_total_bytes)}`;

    if (s.temp !== null && s.temp !== undefined) {
      document.getElementById("t-temp").innerHTML = s.temp.toFixed(0) + "<small>°C</small>";
      document.getElementById("b-temp").style.width = Math.min(100, s.temp) + "%";
      document.getElementById("b-temp").style.background = tempColor(s.temp);
    }

    document.getElementById("updated").textContent = new Date().toLocaleTimeString("ro-RO");
  } catch (e) { /* transient network hiccup, retry on next tick */ }
}

function urlKind(url) {
  // green = stays on the LAN (.home name, or a bare LAN IP); red = leaves it (a public domain)
  let host;
  try { host = new URL(url).hostname; } catch (e) { return "public"; }
  if (host.endsWith(".home")) return "home";
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return "home";
  return "public";
}

const STATUS_LABEL = {running: "activ", exited: "oprit", restarting: "repornește", paused: "pauzat", dead: "mort", created: "creat"};

function fmtUptime(sec) {
  if (sec === null || sec === undefined) return "";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}z ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

async function refreshContainers() {
  try {
    const r = await fetch("/api/containers");
    const list = await r.json();
    if (!Array.isArray(list)) return;
    const el = document.getElementById("containers");
    el.innerHTML = list.map(c => {
      let color = "var(--muted)", label = STATUS_LABEL[c.status] || c.status;
      if (c.status === "running") {
        if (c.health === "unhealthy") { color = "var(--critical)"; label = "nesănătos"; }
        else if (c.health === "starting") { color = "var(--warning)"; label = "pornește"; }
        else { color = "var(--good)"; label = c.health === "healthy" ? "sănătos" : "activ"; }
      } else if (c.status === "restarting") { color = "var(--warning)"; }
      else if (c.status === "exited" || c.status === "dead") { color = "var(--critical)"; }

      const uptime = c.uptime_s ? ` · ${fmtUptime(c.uptime_s)}` : "";
      const linkOrBadge = c.url
        ? `<a class="open ${urlKind(c.url)}" href="${c.url}" target="_blank" rel="noopener">Deschide ↗</a>`
        : `<span class="internal">doar intern</span>`;
      const extras = (c.extra_urls || []).map(u =>
        `<a class="open-extra ${urlKind(u)}" href="${u}" target="_blank" rel="noopener">${u.replace(/^https?:\/\//, "")} ↗</a>`
      ).join("");
      const usage = (c.cpu_pct !== null && c.cpu_pct !== undefined)
        ? `<div class="usage">CPU ${c.cpu_pct.toFixed(1)}% · RAM ${fmtMiB(c.mem_mib || 0)}</div>` : "";

      return `<div class="card ccard">
        <div class="top"><span class="dot-status" style="background:${color}"></span>
          <span class="name">${c.name}</span></div>
        <div class="meta">${label}${uptime}</div>
        <div class="image">${c.image}</div>
        ${usage}
        <div class="row2">${linkOrBadge}</div>
        ${extras ? `<div class="extra-urls">${extras}</div>` : ""}
      </div>`;
    }).join("");
  } catch (e) { /* transient */ }
}

// ---- charts ----
const fmtRate = (bps) => bps === null || bps === undefined ? "--" : fmtBytes(bps) + "/s";

const CHARTS = {
  cpu:  { title: "CPU %", unit: "%", domain: [0, 100], fmt: (v) => v.toFixed(1) + "%",
          series: [{ key: "cpu", color: "var(--line)" }] },
  mem:  { title: "RAM %", unit: "%", domain: [0, 100], fmt: (v) => v.toFixed(1) + "%",
          series: [{ key: "mem", color: "var(--line)" }] },
  temp: { title: "Temperatură °C", unit: "°C", domain: null, fmt: (v) => v.toFixed(1) + "°C",
          series: [{ key: "temp", color: "var(--line)" }] },
  net:  { title: "Rețea", unit: "B/s", domain: null, fmt: fmtRate,
          series: [
            { key: "rx_bps", color: "var(--line)", label: "Descărcare ↓" },
            { key: "tx_bps", color: "var(--line2)", label: "Încărcare ↑" },
          ] },
  scope: { title: "Rețea · local vs internet", unit: "B/s", domain: null, fmt: fmtRate,
          series: [
            { key: "lan_bps", color: "var(--line)", label: "Local" },
            { key: "net_bps", color: "var(--line2)", label: "Internet" },
          ] },
  dio:  { title: "Disc I/O", unit: "B/s", domain: null, fmt: fmtRate,
          series: [
            { key: "dio_read_bps", color: "var(--line)", label: "Citire" },
            { key: "dio_write_bps", color: "var(--line2)", label: "Scriere" },
          ] },
};

// build legends once
for (const [name, cfg] of Object.entries(CHARTS)) {
  if (cfg.series.length < 2) continue;
  const el = document.getElementById(`legend-${name}`);
  if (el) el.innerHTML = cfg.series.map(s => `<span><i style="background:${s.color}"></i>${s.label}</span>`).join("");
}

let currentMode = { type: "minutes", minutes: 2, live: true };
let historyCache = { points: [], earliest_ts: null, range_start: 0, range_end: 0 };

function buildPath(points, w, h, minV, maxV, t0, span) {
  const range = (maxV - minV) || 1;
  const coords = points.map(p => {
    const x = ((p.t - t0) / span) * w;
    const y = h - ((p.v - minV) / range) * h;
    return [x, y];
  });
  let line = `M ${coords[0][0].toFixed(1)} ${coords[0][1].toFixed(1)}`;
  for (let i = 1; i < coords.length; i++) line += ` L ${coords[i][0].toFixed(1)} ${coords[i][1].toFixed(1)}`;
  const area = `${line} L ${coords[coords.length-1][0].toFixed(1)} ${h} L ${coords[0][0].toFixed(1)} ${h} Z`;
  return { line, area, coords };
}

function emptyMessage(hist, name) {
  if (hist.earliest_ts === null) return "Se colectează date… revino peste câteva minute.";
  if (name === "scope" && hist.points.length && hist.points.every(p => p.lan_bps == null)) {
    return "Indisponibil: activează net.netfilter.nf_conntrack_acct=1 pe host (vezi README).";
  }
  if (hist.earliest_ts > hist.range_start) {
    const d = new Date(hist.earliest_ts * 1000).toLocaleString("ro-RO", { dateStyle: "medium", timeStyle: "short" });
    return `Istoricul disponibil începe la ${d}.`;
  }
  return "Nu sunt date pentru acest interval.";
}

function renderSummary(name, cfg, hist) {
  const el = document.getElementById(`sum-${name}`);
  const s = hist.summary && hist.summary[name];
  if (!s) { el.textContent = ""; return; }
  if ("avg" in s) {
    el.textContent = `medie ${cfg.fmt(s.avg)} · min ${cfg.fmt(s.min)} · max ${cfg.fmt(s.max)} pe interval`;
  } else if (name === "net") {
    el.textContent = `total pe interval: ↓ ${fmtBytes(s.rx_total)} · ↑ ${fmtBytes(s.tx_total)}` +
      ` · vârf ↓ ${fmtRate(s.rx_max)} · ↑ ${fmtRate(s.tx_max)}`;
  } else if (name === "dio") {
    el.textContent = `total pe interval: citire ${fmtBytes(s.read_total)} · scriere ${fmtBytes(s.write_total)}` +
      ` · vârf citire ${fmtRate(s.read_max)} · scriere ${fmtRate(s.write_max)}`;
  } else if (name === "scope") {
    el.textContent = `total pe interval: local ${fmtBytes(s.lan_total)} · internet ${fmtBytes(s.net_total)}` +
      ` · vârf local ${fmtRate(s.lan_max)} · internet ${fmtRate(s.net_max)}`;
  }
}

function chartPeak(s) {
  if (!s) return null;
  if ("max" in s) return s.max;
  const maxes = Object.entries(s).filter(([k]) => k.endsWith("_max")).map(([, v]) => v);
  return maxes.length ? Math.max(...maxes) : null;
}

function renderChart(name, cfg, hist) {
  const svg = document.getElementById(`chart-${name}`);
  const tip = document.getElementById(`tip-${name}`);
  const emptyEl = document.getElementById(`empty-${name}`);
  const w = 700, h = 90;
  const wrap = svg.parentElement;
  let peakLabel = wrap.querySelector(".peak-label");
  if (!peakLabel) {
    peakLabel = document.createElement("div");
    peakLabel.className = "peak-label";
    wrap.appendChild(peakLabel);
  }
  peakLabel.style.display = "none";

  renderSummary(name, cfg, hist);

  const seriesPoints = cfg.series.map(s => ({
    ...s,
    points: hist.points
      .filter(p => p[s.key] !== null && p[s.key] !== undefined)
      .map(p => ({ t: p.ts, v: p[s.key] })),
  }));
  const anyUsable = seriesPoints.some(s => s.points.length >= 2);

  svg.innerHTML = "";
  if (!anyUsable) {
    svg.style.display = "none";
    emptyEl.textContent = emptyMessage(hist, name);
    emptyEl.classList.add("show");
    document.getElementById(`cv-${name}`).textContent = "--";
    tip.style.opacity = 0;
    return;
  }
  svg.style.display = "block";
  emptyEl.classList.remove("show");

  const allVals = seriesPoints.flatMap(s => s.points.map(p => p.v));
  const peak = chartPeak(hist.summary && hist.summary[name]);
  let [minV, maxV] = cfg.domain || [Math.min(...allVals), Math.max(...allVals)];
  if (!cfg.domain) {
    if (peak !== null) maxV = Math.max(maxV, peak);
    const pad = (maxV - minV) * 0.15 || 1; minV -= pad; maxV += pad;
  }
  const t0 = hist.range_start, span = (hist.range_end - hist.range_start) || 1;

  const latestParts = seriesPoints.map(s => s.points.length
    ? (cfg.series.length > 1 ? `${s.label.replace(/[↓↑]/, "").trim()} ${cfg.fmt(s.points[s.points.length-1].v)}` : cfg.fmt(s.points[s.points.length-1].v))
    : null).filter(Boolean);
  document.getElementById(`cv-${name}`).textContent = latestParts.join(" · ") || "--";

  const ns = "http://www.w3.org/2000/svg";
  for (const frac of [0, 0.5, 1]) {
    const gy = h - frac * h;
    const g = document.createElementNS(ns, "line");
    g.setAttribute("class", "gridline");
    g.setAttribute("x1", 0); g.setAttribute("x2", w); g.setAttribute("y1", gy); g.setAttribute("y2", gy);
    svg.appendChild(g);
  }

  if (peak !== null && peak > 0) {
    const peakY = h - ((peak - minV) / ((maxV - minV) || 1)) * h;
    const pl = document.createElementNS(ns, "line");
    pl.setAttribute("class", "peakline");
    pl.setAttribute("x1", 0); pl.setAttribute("x2", w); pl.setAttribute("y1", peakY); pl.setAttribute("y2", peakY);
    svg.appendChild(pl);
    peakLabel.textContent = `max ${cfg.fmt(peak)}`;
    peakLabel.style.top = (peakY / h * 100) + "%";
    peakLabel.style.transform = peakY / h < 0.2 ? "translateY(3px)" : "translateY(-100%)";
    peakLabel.style.display = "";
  }

  const drawnSeries = [];
  for (const s of seriesPoints) {
    if (s.points.length < 2) continue;
    const { line, area, coords } = buildPath(s.points, w, h, minV, maxV, t0, span);
    if (cfg.series.length === 1) {
      const areaEl = document.createElementNS(ns, "path");
      areaEl.setAttribute("d", area); areaEl.setAttribute("fill", "var(--line-fill)"); areaEl.setAttribute("stroke", "none");
      svg.appendChild(areaEl);
    }
    const lineEl = document.createElementNS(ns, "path");
    lineEl.setAttribute("d", line); lineEl.setAttribute("fill", "none");
    lineEl.setAttribute("stroke", s.color); lineEl.setAttribute("stroke-width", "2");
    svg.appendChild(lineEl);
    const last = coords[coords.length - 1];
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", last[0]); dot.setAttribute("cy", last[1]); dot.setAttribute("r", 3); dot.setAttribute("fill", s.color);
    svg.appendChild(dot);
    drawnSeries.push({ ...s, coords });
  }

  const hoverLine = document.createElementNS(ns, "line");
  hoverLine.setAttribute("class", "hoverline"); hoverLine.setAttribute("y1", 0); hoverLine.setAttribute("y2", h);
  svg.appendChild(hoverLine);
  const hoverDots = drawnSeries.map(s => {
    const d = document.createElementNS(ns, "circle");
    d.setAttribute("r", 4); d.setAttribute("fill", "var(--surface)"); d.setAttribute("stroke", s.color);
    d.setAttribute("stroke-width", "2"); d.style.opacity = 0;
    svg.appendChild(d);
    return d;
  });

  const rect = document.createElementNS(ns, "rect");
  rect.setAttribute("x", 0); rect.setAttribute("y", 0); rect.setAttribute("width", w); rect.setAttribute("height", h);
  rect.setAttribute("fill", "transparent");
  svg.appendChild(rect);

  function showTip(evt) {
    const bbox = svg.getBoundingClientRect();
    const px = (evt.clientX - bbox.left) / bbox.width * w;
    let idx = 0, best = Infinity;
    (drawnSeries[0] || { coords: [] }).coords.forEach(([x], i) => { const d = Math.abs(x - px); if (d < best) { best = d; idx = i; } });
    let cx = null, time = "";
    const parts = [];
    drawnSeries.forEach((s, si) => {
      const p = s.points[idx];
      if (!p) return;
      const [x, y] = s.coords[idx];
      cx = x;
      time = new Date(p.t * 1000).toLocaleString("ro-RO", { dateStyle: currentMode.type === "minutes" && currentMode.minutes <= 1440 ? undefined : "short", timeStyle: "short" });
      hoverDots[si].setAttribute("cx", x); hoverDots[si].setAttribute("cy", y); hoverDots[si].style.opacity = 1;
      const label = cfg.series.length > 1 ? `${s.label.replace(/[↓↑]/, "").trim()} ` : "";
      parts.push(`${label}${cfg.fmt(p.v)}`);
    });
    if (cx === null) return;
    hoverLine.setAttribute("x1", cx); hoverLine.setAttribute("x2", cx); hoverLine.style.opacity = 1;
    tip.textContent = `${time} · ${parts.join(" · ")}`;
    tip.style.left = (cx / w * 100) + "%";
    tip.style.top = "10%";
    tip.style.opacity = 1;
  }
  function hideTip() { hoverLine.style.opacity = 0; hoverDots.forEach(d => d.style.opacity = 0); tip.style.opacity = 0; }
  rect.addEventListener("mousemove", showTip);
  rect.addEventListener("mouseleave", hideTip);
  rect.addEventListener("touchmove", (e) => { showTip(e.touches[0]); e.preventDefault(); }, {passive:false});
  rect.addEventListener("touchend", hideTip);
}

function renderAllCharts() {
  for (const [name, cfg] of Object.entries(CHARTS)) renderChart(name, cfg, historyCache);
}

async function refreshHistory() {
  try {
    const url = currentMode.type === "custom"
      ? `/api/history?from=${currentMode.from}&to=${currentMode.to}`
      : `/api/history?minutes=${currentMode.minutes}`;
    const r = await fetch(url);
    historyCache = await r.json();
    renderAllCharts();
  } catch (e) { /* transient */ }
}

document.getElementById("range-toggle").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  if (btn.dataset.custom) {
    document.getElementById("datepicker").classList.toggle("open");
    return;
  }
  document.getElementById("datepicker").classList.remove("open");
  document.querySelectorAll("#range-toggle button").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  currentMode = { type: "minutes", minutes: parseInt(btn.dataset.min, 10), live: !!btn.dataset.live };
  refreshHistory();
  scheduleHistoryRefresh();
});

function toLocalInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
const dpFrom = document.getElementById("dp-from"), dpTo = document.getElementById("dp-to");
dpTo.value = toLocalInputValue(new Date());
dpFrom.value = toLocalInputValue(new Date(Date.now() - 24 * 3600 * 1000));

document.getElementById("dp-apply").addEventListener("click", () => {
  const from = Math.floor(new Date(dpFrom.value).getTime() / 1000);
  const to = Math.floor(new Date(dpTo.value).getTime() / 1000);
  if (!from || !to || to <= from) return;
  document.querySelectorAll("#range-toggle button").forEach(b => b.classList.remove("active"));
  currentMode = { type: "custom", from, to };
  refreshHistory();
  scheduleHistoryRefresh();
});

let historyTimer = null;
function scheduleHistoryRefresh() {
  if (historyTimer) clearInterval(historyTimer);
  const fast = currentMode.type === "minutes" && (currentMode.live || currentMode.minutes <= 30);
  historyTimer = setInterval(refreshHistory, fast ? 15000 : 60000);
}

const ALERT_ICON = { critical: "⛔", warning: "⚠️" };
async function refreshAlerts() {
  try {
    const r = await fetch("/api/alerts");
    const list = await r.json();
    const el = document.getElementById("alerts");
    if (!Array.isArray(list) || list.length === 0) { el.innerHTML = ""; return; }
    el.innerHTML = list.map(a =>
      `<div class="alert-item ${a.level}">${ALERT_ICON[a.level] || "•"} ${a.text}</div>`
    ).join("");
  } catch (e) { /* transient */ }
}

async function refreshDiskForecast() {
  try {
    const r = await fetch("/api/disk-forecast");
    const d = await r.json();
    const el = document.getElementById("f-disk");
    if (!d || d.status === "insufficient_data") { el.textContent = ""; return; }
    if (d.status === "stable") {
      el.className = "forecast calm";
      el.textContent = "spațiu stabil";
      return;
    }
    const days = Math.round(d.days_left);
    el.className = "forecast" + (days <= 7 ? " critical" : days <= 30 ? " soon" : "");
    el.textContent = `~${days} ${days === 1 ? "zi" : "zile"} rămase (plin pe ${d.eta_date})`;
  } catch (e) { /* transient */ }
}

refreshStats(); refreshContainers(); refreshHistory(); refreshAlerts(); refreshDiskForecast();
setInterval(refreshStats, 5000);
setInterval(refreshContainers, 5000);
setInterval(refreshAlerts, 10000);
setInterval(refreshDiskForecast, 300000);
scheduleHistoryRefresh();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    threading.Thread(target=sampler_loop, daemon=True).start()
    threading.Thread(target=container_stats_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
