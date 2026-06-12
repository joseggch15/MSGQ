# -*- coding: utf-8 -*-
"""Benchmark sintetico de los motores pesados (tag hopping + actividad).

Genera un historico realista (~120k despachos, 300 equipos, sitios alternantes)
y cronometra los detectores que recorren todo el historico. Uso:

    python tools/bench_audit.py
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.core import activity_audit, tag_hopping

N_EQ = 300
N_ROWS = 120_000

rng = np.random.default_rng(7)
eids = [f"EQ{i:04d}" for i in range(N_EQ)]
sites = ["LFO - Diesel - iTank 1", "TFL0847 - Diesel - iTank 6",
         "TFL0848 - Diesel - iTank 2", "WS - Diesel - iTank 8",
         "MER.13.1.6", "MER.14.2.1"]

eq_col = rng.choice(eids, N_ROWS)
t0 = pd.Timestamp("2024-01-01")
dates = t0 + pd.to_timedelta(np.sort(rng.integers(0, 730 * 86400, N_ROWS)), unit="s")
mv = pd.DataFrame({
    "id": [f"D{i}" for i in range(N_ROWS)],
    "kind": config.KIND_DISPENSE,
    "equipment_id": eq_col,
    "equipment_description": eq_col,
    "tank": rng.choice(sites, N_ROWS),
    "product": rng.choice(["Diesel", "Tellus S3M46"], N_ROWS, p=[0.9, 0.1]),
    "volume": rng.uniform(20, 1500, N_ROWS).round(1),
    "flow_duration_s": rng.uniform(30, 900, N_ROWS).round(0),
    "gps_coordinates": pd.NA,
    "smu_value": rng.uniform(100, 30000, N_ROWS).round(1),
    "smu_type": "hours",
    "record_collected_at": dates,
    "updated_at": dates,
})

eq = pd.DataFrame({
    "equipment_id": eids,
    "description": eids,
    "category": rng.choice(["Haul truck", "Dozer", "Light Vehicle"], N_EQ),
    "status": config.STATUS_IN,
    "rfid": [f"E280{i:08X}" for i in range(N_EQ)],
    "is_light_vehicle": rng.choice([True, False], N_EQ, p=[0.2, 0.8]),
})
limits = pd.DataFrame({
    "id": [f"L{i}" for i in range(N_EQ)],
    "equipment_id": eids, "internal_id": [str(i) for i in range(N_EQ)],
    "product": "Diesel", "product_code": "D",
    "sfl": rng.choice([560.0, 1000.0, 1893.0], N_EQ),
})

for label, fn in [
    ("tag_hopping.audit", lambda: tag_hopping.audit(mv, eq)),
    ("activity.fueling_without_activity",
     lambda: activity_audit.fueling_without_activity(mv, eq, limits)),
    ("activity.unfueled_activity",
     lambda: activity_audit.unfueled_activity(mv, eq, limits)),
]:
    t = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t
    n = len(out.events) if hasattr(out, "events") else len(out)
    print(f"{label:42s} {dt:8.2f}s   ({n} hallazgos)")
