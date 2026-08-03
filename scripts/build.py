#!/usr/bin/env python3
"""
Build the combined VPS/GPU price index.

Pulls from public, unauthenticated vendor APIs and merges them with the
hand-verified rows under sources/. Writes data/prices.json and data/prices.csv.

No credentials are required or read anywhere in this pipeline. If a fetcher ever
needs an API key, it does not belong in this repo.
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.request
from datetime import date, timezone, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SOURCES = ROOT / "sources"

TODAY = date.today().isoformat()
UA = "vps-gpu-price-index/1.0 (+https://github.com/frechdi/vps-gpu-price-index)"

# Field order for the CSV and for every emitted record.
FIELDS = [
    "provider", "plan", "label", "vcpu", "ram_gb", "disk_gb", "arch",
    "gpu", "price_monthly", "price_hourly", "currency", "vat_included",
    "region", "collection", "source_url", "verified_date",
]


def get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def row(**kw):
    """Build a record with every field present, so JSON and CSV stay aligned."""
    base = {f: None for f in FIELDS}
    base.update(kw)
    unknown = set(kw) - set(FIELDS)
    if unknown:
        raise KeyError(f"unknown field(s): {sorted(unknown)}")
    return base


# --------------------------------------------------------------------------
# Vultr — https://www.vultr.com/api/#tag/plans  (public, no auth)
# --------------------------------------------------------------------------
def fetch_vultr():
    url = "https://api.vultr.com/v2/plans?per_page=500"
    out, seen = [], set()
    while url:
        payload = get_json(url)
        for p in payload.get("plans", []):
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            monthly = p.get("monthly_cost")
            if monthly in (None, 0):
                continue  # skip the free/promo tier
            gpu_vram = p.get("gpu_vram_gb") or 0
            out.append(row(
                provider="vultr",
                plan=pid,
                label=p.get("type"),
                vcpu=p.get("vcpu_count"),
                ram_gb=round((p.get("ram") or 0) / 1024, 2) if p.get("ram") else None,
                disk_gb=p.get("disk"),
                arch="x86",
                gpu=(p.get("gpu_type") or None) if gpu_vram else None,
                price_monthly=monthly,
                price_hourly=p.get("hourly_cost"),
                currency="USD",
                vat_included=False,
                region="global",
                collection="api",
                source_url="https://api.vultr.com/v2/plans",
                verified_date=TODAY,
            ))
        nxt = (payload.get("meta") or {}).get("links", {}).get("next")
        url = f"https://api.vultr.com/v2/plans?per_page=500&cursor={nxt}" if nxt else None
    return out


# --------------------------------------------------------------------------
# Linode / Akamai — https://techdocs.akamai.com/linode-api  (public, no auth)
# --------------------------------------------------------------------------
def fetch_linode():
    payload = get_json("https://api.linode.com/v4/linode/types")
    out = []
    for t in payload.get("data", []):
        price = t.get("price") or {}
        monthly = price.get("monthly")
        if monthly in (None, 0):
            continue
        gpus = t.get("gpus") or 0
        out.append(row(
            provider="linode",
            plan=t.get("id"),
            label=t.get("label"),
            vcpu=t.get("vcpus"),
            ram_gb=round((t.get("memory") or 0) / 1024, 2) if t.get("memory") else None,
            disk_gb=round((t.get("disk") or 0) / 1024, 2) if t.get("disk") else None,
            arch="x86",
            gpu=f"{gpus}x GPU" if gpus else None,
            price_monthly=monthly,
            price_hourly=price.get("hourly"),
            currency="USD",
            vat_included=False,
            region="global (base price; some regions cost more)",
            collection="api",
            source_url="https://api.linode.com/v4/linode/types",
            verified_date=TODAY,
        ))
    return out


# --------------------------------------------------------------------------
# Hand-verified sources under sources/*.json
# --------------------------------------------------------------------------
def load_manual():
    out = []
    for path in sorted(SOURCES.glob("*.json")):
        spec = json.loads(path.read_text())
        for p in spec["plans"]:
            out.append(row(
                provider=spec["provider"],
                plan=p["plan"],
                label=spec.get("provider_name"),
                vcpu=p.get("vcpu"),
                ram_gb=p.get("ram_gb"),
                disk_gb=p.get("disk_gb"),
                arch=p.get("arch"),
                gpu=p.get("gpu"),
                price_monthly=p.get("price_monthly"),
                price_hourly=p.get("price_hourly"),
                currency=spec["currency"],
                vat_included=spec["vat_included"],
                region=spec.get("region"),
                collection="manual",
                source_url=spec["source_url"],
                verified_date=spec["verified_date"],
            ))
    return out


FETCHERS = {"vultr": fetch_vultr, "linode": fetch_linode}


def main() -> int:
    records, errors = [], []

    for name, fn in FETCHERS.items():
        try:
            got = fn()
            records.extend(got)
            print(f"[{name}] {len(got)} plans", file=sys.stderr)
        except Exception as exc:  # a vendor outage must not produce a silently empty file
            errors.append(f"{name}: {exc}")
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)

    manual = load_manual()
    records.extend(manual)
    print(f"[manual] {len(manual)} plans", file=sys.stderr)

    if errors and not records:
        print("every source failed; refusing to write an empty index", file=sys.stderr)
        return 1

    records.sort(key=lambda r: (r["provider"], r["price_monthly"] or 0))

    DATA.mkdir(exist_ok=True)
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_count": len(records),
        "providers": sorted({r["provider"] for r in records}),
        "failed_sources": errors,
        "currency_note": "Prices are listed in each vendor's own billing currency and are NOT converted. Compare within a currency, not across.",
        "vat_note": "vat_included is false for every row currently in the index. EU consumers pay VAT on top.",
        "license": "CC0-1.0",
        "records": records,
    }
    (DATA / "prices.json").write_text(json.dumps(doc, indent=2) + "\n")

    with (DATA / "prices.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)

    print(f"wrote {len(records)} records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
