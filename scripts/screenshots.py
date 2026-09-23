"""Regenerate docs/screenshots using synthetic demo data.

    pip install playwright pillow && playwright install chromium
    python scripts/screenshots.py http://localhost:5000 docs/screenshots

Every /api/* response is replaced inside the browser with generated data, so nothing
from the real host (containers, domains, addresses, metrics) ends up in the images.
"""
import glob
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000"
OUT = sys.argv[2] if len(sys.argv) > 2 else "docs/screenshots"

SAMPLE = 15
UTC_OFFSET = 3 * 3600
START = time.time()
TODAY = int((START + UTC_OFFSET) // 86400)
MEM_TOTAL_MIB = 16384
DISK_TOTAL = 1000204886016
COLS = ["ts", "cpu", "mem", "disk", "temp", "load1", "load5", "load15",
        "rx_bps", "tx_bps", "dio_read_bps", "dio_write_bps", "lan_bps", "net_bps"]


# ------------------------------------------------------------ demo signals --

def frac(x):
    return x - math.floor(x)


def jitter(t, seed):
    return frac(math.sin(int(t) / SAMPLE * 12.9898 + seed * 78.233) * 43758.5453)


def wave(t, seed, period):
    return 0.6 * math.sin(t / period + seed) + 0.4 * math.sin(t / (period * 0.37) + seed * 2.1)


def bump(h, a, b, ramp=0.3):
    if h < a - ramp or h > b + ramp:
        return 0.0
    if h < a:
        return (h - (a - ramp)) / ramp
    if h > b:
        return ((b + ramp) - h) / ramp
    return 1.0


def sample(t):
    """One fake host sample: quiet nights, a 03:00 backup, a daily download,
    evening media streaming (longer on weekends) and occasional CPU spikes.
    Per-day variation keeps the 7 and 30 day views from looking stamped."""
    local = t + UTC_OFFSET
    day, h = int(local // 86400), (local % 86400) / 3600
    dv, dv2 = frac(math.sin(day * 12.9898) * 43758.5453), frac(math.sin(day * 78.233 + 1) * 43758.5453)
    weekend = 1.0 if (day + 3) % 7 >= 5 else 0.0
    stream = bump(h, 18.0 - weekend, 23.0 + weekend, 0.6) * (0.7 + 0.6 * dv + 0.35 * weekend) \
        * (0.6 + 0.8 * max(0.0, wave(t, 3, 700)))
    morning = bump(h, 7.0, 8.5, 0.5)
    backup = bump(h, 2.75, 3.6, 0.1) * (0.8 + 0.4 * dv2)
    b0 = 9 + dv2 * 10
    burst = bump(h, b0, b0 + 0.45, 0.05)
    spike = bump(h, 19.6, 19.72, 0.02) if (dv > 0.7 or day >= TODAY - 1) else 0.0

    cpu = 6 + 3 * wave(t, 1, 900) + 2 * jitter(t, 1) + 30 * stream + 30 * backup + 12 * burst \
        + 8 * morning + 62 * spike * (0.9 + 0.1 * jitter(t, 4))
    cpu = min(97.0, max(1.0, cpu))
    mem = 47 + 4 * wave(t, 7, 9000) + 5 * stream + 2 * backup
    temp = 36 + 0.36 * cpu + 1.2 * wave(t, 5, 1200) + 0.8 * jitter(t, 2)
    disk = 46.4 + (t - START) / 86400 * 0.37

    lan = 25e3 * (0.5 + jitter(t, 5)) + 2.2e6 * stream + 0.4e6 * morning
    net_down = 60e3 * (0.5 + jitter(t, 6)) + 11e6 * burst * (0.85 + 0.3 * jitter(t, 7))
    net_up = 40e3 * (0.5 + jitter(t, 8)) + 2.6e6 * backup * (0.8 + 0.4 * jitter(t, 9)) + 0.15e6 * stream
    read = 300e3 * jitter(t, 10) + 95e6 * backup * (0.7 + 0.3 * jitter(t, 11)) + 3e6 * stream
    write = 400e3 * (0.5 + jitter(t, 12)) + 11e6 * burst + 4e6 * backup
    load = cpu / 100 * 4
    return {
        "ts": int(t), "cpu": cpu, "mem": mem, "disk": disk, "temp": temp,
        "load1": load * (0.9 + 0.2 * jitter(t, 13)), "load5": load * 0.9, "load15": load * 0.8,
        "rx_bps": 0.12 * lan + net_down, "tx_bps": 0.88 * lan + net_up,
        "dio_read_bps": read, "dio_write_bps": write, "lan_bps": lan, "net_bps": net_down + net_up,
    }


def history(start, end):
    step = SAMPLE if end - start <= 1.5 * 86400 else (end - start) // 2880 // SAMPLE * SAMPLE
    first = start - start % step + step
    raw = [sample(t) for t in range(first, end + 1, step)]
    bucket = max(SAMPLE, (end - start) // 300)
    groups = {}
    for s in raw:
        groups.setdefault(s["ts"] // bucket, []).append(s)
    points = []
    for _, rows in sorted(groups.items()):
        p = {c: round(sum(r[c] for r in rows) / len(rows), 1) for c in COLS[1:]}
        p["ts"] = min(r["ts"] for r in rows)
        points.append(p)

    def gauge(col):
        v = [s[col] for s in raw]
        return {"avg": sum(v) / len(v), "min": min(v), "max": max(v)}

    def rates(pairs):
        out = {}
        for name, col in pairs:
            v = [s[col] for s in raw]
            out[f"{name}_total"] = sum(v) / len(v) * (end - start)
            out[f"{name}_max"] = max(v)
        return out

    summary = {
        "cpu": gauge("cpu"), "mem": gauge("mem"), "disk": gauge("disk"), "temp": gauge("temp"),
        "net": rates([("rx", "rx_bps"), ("tx", "tx_bps")]),
        "dio": rates([("read", "dio_read_bps"), ("write", "dio_write_bps")]),
        "scope": rates([("lan", "lan_bps"), ("net", "net_bps")]),
    }
    return {"points": points, "earliest_ts": start - 3 * 86400,
            "range_start": start, "range_end": end, "summary": summary}


# -------------------------------------------------------------- demo routes --

CONTAINERS = [
    # name, image, status, health, url, extra_urls, uptime (s), cpu %, mem MiB
    ("adguardhome", "adguard/adguardhome:latest", "running", "healthy", "http://192.168.1.10:3000", [], 1123200, 0.8, 96),
    ("backup-runner", "restic/restic:latest", "exited", None, None, [], None, None, None),
    ("cloudflared", "cloudflare/cloudflared:latest", "running", None, None, [], 1123200, 0.3, 24),
    ("dashboard", "dashboard-dashboard:latest", "running", None, "http://dashboard.home", [], 86400, 1.1, 42),
    ("grafana", "grafana/grafana:latest", "running", "healthy", "http://grafana.home", [], 604800, 1.4, 182),
    ("home-assistant", "ghcr.io/home-assistant/home-assistant:stable", "running", "healthy",
     "http://ha.home", ["http://ha.example.com"], 950400, 2.6, 410),
    ("jellyfin", "jellyfin/jellyfin:latest", "running", "healthy",
     "http://jellyfin.home", ["http://jellyfin.example.com"], 259200, 18.4, 1230),
    ("metube", "ghcr.io/alexta69/metube:latest", "running", None, "http://metube.home", [], 432000, 0.4, 88),
    ("nextcloud", "nextcloud:29-apache", "running", "healthy", "http://nextcloud.home", [], 691200, 2.1, 310),
    ("paperless", "ghcr.io/paperless-ngx/paperless-ngx:latest", "running", "unhealthy", "http://paperless.home", [], 18000, 3.9, 640),
    ("traefik", "traefik:v3.1", "running", None, None, [], 1123200, 0.9, 71),
    ("vaultwarden", "vaultwarden/server:latest", "running", "healthy", "http://vault.home", [], 1123200, 0.1, 18),
    ("watchtower", "containrrr/watchtower:latest", "running", None, None, [], 1123200, 0.0, 12),
]


def api_containers(_):
    return [
        {"name": n, "image": img, "status": st, "health": hl, "url": url, "extra_urls": extra,
         "uptime_s": up, "cpu_pct": cpu, "mem_mib": mem}
        for n, img, st, hl, url, extra, up, cpu, mem in CONTAINERS
    ]


def api_stats(_):
    now = time.time()
    s = sample(now)
    s["load5"] = sum(sample(now - i * 30)["cpu"] for i in range(10)) / 10 / 100 * 4
    s["load15"] = sum(sample(now - i * 60)["cpu"] for i in range(15)) / 15 / 100 * 4
    s.update(mem_used_mib=round(s["mem"] / 100 * MEM_TOTAL_MIB), mem_total_mib=MEM_TOTAL_MIB,
             disk_used_bytes=round(s["disk"] / 100 * DISK_TOTAL), disk_total_bytes=DISK_TOTAL)
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}


def api_history(q):
    now = int(time.time())
    if "from" in q and "to" in q:
        return history(int(q["from"][0]), min(int(q["to"][0]), now))
    return history(now - int(q.get("minutes", ["60"])[0]) * 60, now)


def api_forecast(_):
    return {"status": "growing", "days_left": 96.0, "slope_pct_per_day": 0.37,
            "eta_date": (datetime.now() + timedelta(days=96)).strftime("%Y-%m-%d")}


API = {
    "/api/stats": api_stats,
    "/api/history": api_history,
    "/api/containers": api_containers,
    "/api/alerts": lambda _: [{"level": "critical", "text": "paperless: nesănătos"},
                              {"level": "warning", "text": "backup-runner: exited"}],
    "/api/disk-forecast": api_forecast,
}


def handle(route):
    url = urlparse(route.request.url)
    fn = API.get(url.path)
    if fn is None:
        route.abort()
        return
    route.fulfill(status=200, content_type="application/json", body=json.dumps(fn(parse_qs(url.query))))


# ---------------------------------------------------------------- capturing --

def open_page(browser, scheme, width, height, scale, mobile=False, range_min=1440):
    ctx = browser.new_context(
        viewport={"width": width, "height": height}, device_scale_factor=scale, color_scheme=scheme,
        locale="ro-RO", timezone_id="Europe/Chisinau", is_mobile=mobile, has_touch=mobile,
    )
    page = ctx.new_page()
    page.route(re.compile(r".*/api/.*"), handle)
    page.goto(BASE, wait_until="load")
    if range_min:
        page.click(f'#range-toggle button[data-min="{range_min}"]')
    page.wait_for_timeout(6000)
    return ctx, page


def make_gif(browser, path, width=900, height=600):
    """Tour: top of page, every history range, tooltips following the cursor on two
    charts, then the container cards. Frames are screenshots joined with Pillow."""
    ctx, page = open_page(browser, "dark", width, height, 1, range_min=None)
    frames, durations = [], []

    def shot(ms):
        frames.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
        durations.append(ms)

    def top_of(selector, offset):
        return page.evaluate(f"document.querySelector('{selector}').getBoundingClientRect().top + scrollY - {offset}")

    def scroll_to(y, steps):
        y0 = page.evaluate("scrollY")
        for i in range(1, steps + 1):
            k = i / steps
            page.evaluate(f"scrollTo(0, {y0 + (y - y0) * k * k * (3 - 2 * k)})")
            page.wait_for_timeout(30)
            shot(45)

    def sweep(selector, x0, x1, steps):
        box = page.locator(selector).bounding_box()
        for i in range(steps + 1):
            x = box["x"] + box["width"] * (x0 + (x1 - x0) * i / steps)
            page.mouse.move(x, box["y"] + box["height"] / 2)
            page.wait_for_timeout(40)
            shot(70)
        page.mouse.move(2, 2)

    shot(1800)
    scroll_to(top_of("#range-toggle", 70), 8)
    shot(600)
    for minutes in (60, 360, 1440, 10080, 43200, 1440):
        page.click(f'#range-toggle button[data-min="{minutes}"]')
        page.mouse.move(2, 2)
        page.wait_for_timeout(900)
        shot(1300)
    sweep("#chart-cpu", 0.04, 0.95, 18)
    shot(700)
    scroll_to(page.evaluate("document.querySelector('#chart-net').closest('.card').getBoundingClientRect().top + scrollY - 20"), 8)
    sweep("#chart-scope", 0.04, 0.83, 18)
    shot(700)
    scroll_to(top_of("#containers", 50), 8)
    shot(2200)
    ctx.close()

    step = max(1, len(frames) // 8)
    sheet = Image.new("RGB", (width, height * len(frames[::step])))
    for i, f in enumerate(frames[::step]):
        sheet.paste(f, (0, i * height))
    palette = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    paletted = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    paletted[0].save(path, save_all=True, append_images=paletted[1:], duration=durations, loop=0, optimize=True)
    print(f"{os.path.basename(path)}: {len(frames)} frames, {os.path.getsize(path) // 1024} KB")


def shrink(path):
    img = Image.open(path).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    img.save(path, optimize=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()

        ctx, page = open_page(browser, "dark", 1200, 900, 1.5)
        page.screenshot(path=f"{OUT}/hero-dark.png", full_page=True)
        page.locator(".tiles").screenshot(path=f"{OUT}/tiles.png")
        # A full_page capture resets the mouse and drops the hover tooltip, so use a
        # viewport tall enough to hold every chart and clip inside it instead.
        page.set_viewport_size({"width": 1200, "height": 1300})
        page.evaluate("document.querySelector('.charts').scrollIntoView({block: 'start'})")
        box = page.locator("#chart-cpu").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.93, box["y"] + box["height"] / 2)
        page.wait_for_timeout(600)
        page.screenshot(path=f"{OUT}/charts.png", clip=page.locator(".charts").bounding_box())
        page.locator("#containers").screenshot(path=f"{OUT}/containers.png")
        ctx.close()

        ctx, page = open_page(browser, "light", 1200, 900, 1.5)
        page.screenshot(path=f"{OUT}/hero-light.png", full_page=True)
        ctx.close()

        ctx, page = open_page(browser, "dark", 390, 844, 2, mobile=True)
        page.screenshot(path=f"{OUT}/mobile-dark.png")
        ctx.close()

        make_gif(browser, f"{OUT}/demo.gif")
        browser.close()

    for f in sorted(glob.glob(f"{OUT}/*.png")):
        shrink(f)
        print(f"{os.path.basename(f)}: {os.path.getsize(f) // 1024} KB")


if __name__ == "__main__":
    main()
