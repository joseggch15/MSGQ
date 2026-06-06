"""Fuente de datos simulada — modo demo / pruebas sin red ni token.

Genera registros con la MISMA forma que la API GraphQL real (camelCase, sub-
objetos `target { equipmentId ... }` / `serviceTruck` / `fieldUser`, volumenes
como String, contaminacion `maxContamination4`...), de modo que transform,
storage, alertas e interfaz se ejercitan igual que en produccion.

Nota de fidelidad: el tipo Equipment Item de GraphQL expone POCOS campos
(equipmentId, description, fieldId, status, erpReference, rfidTags, projectCode,
sap, orderNumber, orderItem). El simulador respeta eso: marca/modelo/grupo NO
van en el nodo de equipo (esos solo viven por-movimiento via equipmentGroup /
equipmentCategory / product).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from msgq.config import (
    STATUS_IN, STATUS_OUT, STATUS_DECOM,
    KIND_DISPENSE, KIND_DELIVERY, KIND_TRANSFER,
    TYPE_AUTO, TYPE_MANUAL, TYPE_KEY_BYPASS, TYPE_SUP_OVERRIDE,
    TYPE_SPILLAGE, TYPE_UNAUTHORISED,
    SOURCE_DOCKET, SOURCE_METER,
    Settings,
)

_SITE = {"code": "MERIAN", "description": "Merian Newmont"}

# Catalogo de fabricacion (uso interno para dar realismo a los movimientos).
_FLEET_SPECS = [
    ("Haul Trucks",      "N-HT",  "Caterpillar", "785D"),
    ("Excavators",       "N-EX",  "Hitachi",     "EX2600"),
    ("Dozers",           "N-DZ",  "Caterpillar", "D10T"),
    ("Graders",          "N-GR",  "Caterpillar", "16M"),
    ("Backhoe loaders",  "N-BHL", "JCB",         "3CX"),
    ("Light Vehicles",   "N-LV",  "Toyota",      "Hilux"),
    ("Water Carts",      "N-WC",  "Caterpillar", "777"),
]
_PRODUCTS = ["DIESEL", "DIESEL", "DIESEL", "UNLEADED"]
_DEPARTMENTS = ["Mining", "Maintenance", "Civil", "Logistics"]
_COST_CENTRES = ["CC-1001", "CC-1002", "CC-2050", "CC-3010"]

# Tanques del sitio (espejo de la estructura real de Merian): el circuito Diesel
# (Main + Virtual logico + 3 satelites) y el de Gasolina (174-TK-01), mas un
# tanque de lubricante. (code, description, product, virtual, capacity, parent, type)
_TANKS_SPEC = [
    ("LFO - Main Tank",   "LFO - Main Tank",            "Diesel",            False, 1022400.0, None,                 "2in1 - Left"),
    ("LFO - Virtual Tank","LFO - Virtual Tank",         "Diesel",            True,   149847.0, None,                 "Level - 1 Cylinder"),
    ("LFO - 171-TK-03",   "LFO - Tank 1 - 171-TK-03",   "Diesel",            False,   49949.0, "LFO - Virtual Tank", "Level - 1 Cylinder"),
    ("LFO - 171-TK-04",   "LFO - Tank 2 - 171-TK-04",   "Diesel",            False,   49949.0, "LFO - Virtual Tank", "Level - 1 Cylinder"),
    ("LFO - 171-TK-05",   "LFO - Tank 3 - 171-TK-05",   "Diesel",            False,   49949.0, "LFO - Virtual Tank", "Level - 1 Cylinder"),
    ("LFO - 174-TK-01",   "LFO - 174-TK-01",            "Unleaded Gasoline", False,    4699.0, None,                 "Level - 1 Cylinder 4K"),
    ("WS - Tank 6",       "WS - Rimula R4X15W40 - Tank 6", "15W40",          False,   32257.0, None,                 "Level"),
]

# Tanques con reconciliacion diaria sintetica: (code, desc, product, stock base,
# inflow medio, outflow medio).
_RECON_SPEC = [
    ("LFO - Main Tank",    "LFO - Main Tank",              "Diesel",            760000.0, 120000.0, 130000.0),
    ("LFO - Virtual Tank", "LFO - Virtual Tank",           "Diesel",             90000.0,   8000.0,   9000.0),
    ("LFO - 174-TK-01",    "LFO - 174-TK-01",              "Unleaded Gasoline",   3000.0,    300.0,    350.0),
    ("WS - Tank 6",        "WS - Rimula R4X15W40 - Tank 6","15W40",              18000.0,      0.0,    600.0),
]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class SimulatorSource:
    """Implementa el contrato `DataSource` con datos sinteticos."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._rng = random.Random()          # flujo de movimientos: vivo
        roster_rng = random.Random(42)       # flota: estable entre arranques
        self._fleet = self._build_fleet(roster_rng)   # dicts internos ricos
        self._adaptmacs = self._build_adaptmacs(roster_rng)
        self._change_log = self._build_change_log(roster_rng)
        self._tanks = self._build_tanks()
        self._reconciliations = self._build_reconciliations(roster_rng)
        self._seq = 0
        self._equipment_sent = False

    # -- contrato DataSource -----------------------------------------------

    async def fetch_movements(self, updated_from: datetime | None) -> list[dict]:
        n = self._rng.randint(3, 12)
        return [self._make_movement() for _ in range(n)]

    async def fetch_movements_paged(self, updated_from: datetime | None, on_page) -> None:
        """Entrega los movimientos por pagina (el demo no tiene historial real, asi
        que es un solo lote). Mantiene el contrato usado por el backfill del poller."""
        nodes = await self.fetch_movements(updated_from)
        if nodes:
            on_page(nodes)

    async def fetch_equipment(self, updated_from: datetime | None) -> list[dict]:
        """Primer ciclo: roster completo (forma GraphQL documentada). Luego, pocos."""
        if not self._equipment_sent:
            self._equipment_sent = True
            return [self._equipment_node(e) for e in self._fleet]
        k = self._rng.randint(0, 3)
        changed = self._rng.sample(self._fleet, k) if k else []
        return [self._equipment_node(e) for e in changed]

    async def fetch_adaptmacs(self, updated_from: datetime | None) -> list[dict]:
        for mac in self._adaptmacs:
            if self._rng.random() < 0.15:
                mac["online"] = self._rng.random() > 0.2
                mac["keyBypass"] = self._rng.random() < 0.1
        return list(self._adaptmacs)

    async def fetch_changes(self, record_type: str,
                            changes_from: datetime | None) -> list[dict]:
        """Eventos sinteticos del log de auditoria del tipo pedido."""
        if self._rng.random() < 0.5:
            self._append_live_change()
        floor = self._naive(changes_from) if changes_from is not None else None
        out = []
        for e in self._change_log:
            if e["recordType"] != record_type:
                continue
            if floor is not None:
                ts = self._parse(e["changedAt"])
                if ts is not None and ts < floor:
                    continue
            out.append(e)
        return out

    async def fetch_changes_paged(self, record_type: str,
                                  changes_from: datetime | None, on_page) -> None:
        nodes = await self.fetch_changes(record_type, changes_from)
        for i in range(0, len(nodes), 200):   # emula paginacion para el progreso
            on_page(nodes[i:i + 200])

    async def fetch_tanks(self, updated_from: datetime | None) -> list[dict]:
        return list(self._tanks)

    async def fetch_reconciliations(self, updated_from: datetime | None) -> list[dict]:
        floor = self._naive(updated_from) if updated_from is not None else None
        if floor is None:
            return list(self._reconciliations)
        out = []
        for r in self._reconciliations:
            ts = self._parse(r["recordUpdatedAt"])
            if ts is None or ts >= floor:
                out.append(r)
        return out

    async def aclose(self) -> None:
        return None

    # -- generadores internos ----------------------------------------------

    def _build_fleet(self, rng: random.Random) -> list[dict]:
        fleet: list[dict] = []
        for i in range(1, 41):
            group, cat, make, model = rng.choice(_FLEET_SPECS)
            status = rng.choices([STATUS_IN, STATUS_OUT, STATUS_DECOM],
                                 weights=[85, 12, 3])[0]
            light = group == "Light Vehicles"
            fleet.append({
                "equipment_id": f"{i:03d}", "field_id": str(i),
                "description": f"{make} {model} #{i:03d}", "status": status,
                "group": group, "category": cat, "make": make, "model": model,
                "product": rng.choice(_PRODUCTS), "is_service_truck": False,
                "is_light_vehicle": light,
                "cost_centre": rng.choice(_COST_CENTRES),
                "zone": rng.choice(["North Pit", "South Pit", "ROM Pad", "Workshop"]),
                "department": rng.choice(_DEPARTMENTS),
                "smu": rng.randint(500, 18000),
                "interval_type": "kms" if light else "hrs",
            })
        for tag in ("TFL0846", "TFL0847", "TFL0848"):
            fleet.append({
                "equipment_id": tag, "field_id": tag,
                "description": f"Service Truck {tag}", "status": STATUS_IN,
                "group": "Service Trucks", "category": "N-ST",
                "make": "Isuzu", "model": "FVR", "product": "DIESEL",
                "is_service_truck": True, "is_light_vehicle": False,
                "cost_centre": "CC-2050", "zone": "Workshop",
                "department": "Maintenance", "smu": rng.randint(2000, 9000),
                "interval_type": "hrs",
            })
        for idx, item in enumerate(fleet, start=1):
            item["internal_id"] = str(idx)   # id numerico interno (== recordId)
            # Tag RFID estable por equipo: el mismo valor que veran `rfidTags` y el
            # log de cambios, para que el enlace por valor del modulo de inventario
            # de tags funcione tambien en modo demo (en vivo el enlace es por valor).
            item["rfid_tag"] = f"E280{rng.randint(10**11, 10**12 - 1)}"
            # Safe Fill Level del producto principal (para auditar sobrellenados).
            item["sfl"] = 600.0 if item["product"] == "UNLEADED" else 1893.0
        return fleet

    def _equipment_node(self, e: dict) -> dict:
        """Mapea un equipo interno a la forma GraphQL real de Merian (campos ricos)."""
        rng = self._rng
        light = e["is_light_vehicle"]
        return {
            "id": e["internal_id"],
            "equipmentId": e["equipment_id"],
            "fieldId": e["field_id"],
            "description": e["description"],
            "fieldDescription": "",
            "status": e["status"],
            "make": e["make"],
            "model": e["model"],
            "division": "",
            "contractor": "",
            "isLightVehicle": light,
            "isContractorVehicle": False,
            "isRebateEligible": True,
            "dispenseLimited": light,
            "dispenseLimitPeriod": "SHIFT" if light else None,
            "serviceInterval": 10000 if light else 250,
            "serviceIntervalType": e["interval_type"],
            "smuValueSource": "adaptsmu",
            "rfidTags": [e["rfid_tag"]],
            "consumptionTanks": [
                {"id": f"CT-{e['internal_id']}-main", "sfl": f"{e['sfl']:.0f}",
                 "product": {"code": e["product"], "description": e["product"].title()}},
                {"id": f"CT-{e['internal_id']}-oil", "sfl": "204",
                 "product": {"code": "15W40", "description": "15W40"}},
                {"id": f"CT-{e['internal_id']}-cool", "sfl": "379",
                 "product": {"code": "Coolant", "description": "Coolant"}},
            ],
            "projectCode": "P-MIN" if e["is_service_truck"] else rng.choice(["P-MIN", "P-CIV", None]),
            "sap": f"MP-{e['equipment_id']}",
            "orderNumber": None,
            "orderItem": None,
            "erpReference": f"SAP-{rng.randint(100000, 999999)}",
            "gpsCoordinates": None,
            "volumeUnit": "Litres",
            "expiryDate": None,
            "lastChangedAt": _iso(datetime.now()),
            "equipmentGroup": {"code": e["category"], "description": e["group"]},
            "equipmentCategory": {"code": e["category"], "description": e["category"]},
            "costCentre": {"code": e["cost_centre"], "description": e["cost_centre"]},
            "department": {"code": e["department"], "description": e["department"]},
        }

    def _build_adaptmacs(self, rng: random.Random) -> list[dict]:
        macs = []
        for i in range(1, 7):
            online = rng.random() > 0.2
            macs.append({
                "code": f"MAC-{i:02d}", "description": f"Consola {i:02d}",
                "erpReference": f"1083.{i}.{rng.randint(100, 999)}.1",
                "keyBypass": False, "online": online,
            })
        return macs

    # -- log de auditoria sintetico ----------------------------------------

    _EMAILS = ["m.venegas@plgims.com", "ryan.fredeluces@veridapt.com",
               "j.gomez@plgims.com"]

    def _chg(self, when, rtype, rid, event, who, attr, before, after) -> dict:
        return {
            "changedAt": when.isoformat(), "recordType": rtype, "recordId": rid,
            "event": event, "whodunnit": who,
            "changes": [{"attribute": attr, "before": before, "after": after}],
        }

    def _build_change_log(self, rng: random.Random) -> list[dict]:
        now = datetime.now()
        log: list[dict] = []
        # Transiciones de estado (1=In, 2=Out, 3=Decom) en ~15 equipos.
        for e in rng.sample(self._fleet, min(15, len(self._fleet))):
            cur = "1"
            for _ in range(rng.randint(1, 3)):
                when = now - timedelta(days=rng.randint(5, 175), hours=rng.randint(0, 23))
                nxt = rng.choice([s for s in ("1", "2", "3") if s != cur])
                log.append(self._chg(when, "EquipmentItem", e["internal_id"],
                                     "update", rng.choice(self._EMAILS),
                                     "equipment_status_id", cur, nxt))
                cur = nxt
        # RFID: alta de tag (y, en algunos, un reemplazo posterior). El valor
        # VIGENTE coincide con `rfidTags` del equipo (e["rfid_tag"]) para que el
        # enlace por valor del modulo de inventario funcione en modo demo.
        for i in range(1, 26):
            stable = self._fleet[i - 1]["rfid_tag"]
            if rng.random() < 0.30:
                # re-tagueo: alta con un tag viejo, reemplazado luego por el vigente.
                old = f"56B{rng.randint(0x10000, 0xFFFFF):05X}"
                when = now - timedelta(days=rng.randint(60, 179))
                log.append(self._chg(when, "EquipmentRfid", str(i), "create",
                                     rng.choice(self._EMAILS), "rfid", None, old))
                when2 = when + timedelta(days=rng.randint(10, 40))
                log.append(self._chg(when2, "EquipmentRfid", str(i), "update",
                                     rng.choice(self._EMAILS), "rfid", old, stable))
            else:
                when = now - timedelta(days=rng.randint(3, 179))
                log.append(self._chg(when, "EquipmentRfid", str(i), "create",
                                     rng.choice(self._EMAILS), "rfid", None, stable))
        # Algunas remociones: el tag se quito y ya no esta en ningun equipo, asi
        # que el modulo de inventario las muestra como REMOVAL sin equipo (igual
        # que en vivo: un tag removido no es enlazable a su equipo por valor).
        for j in range(3):
            when = now - timedelta(days=rng.randint(20, 160))
            removed = f"56B{rng.randint(0x10000, 0xFFFFF):05X}"
            log.append(self._chg(when, "EquipmentRfid", str(300 + j), "destroy",
                                 rng.choice(self._EMAILS), "rfid", removed, None))
        # Reasignaciones de cost centre y grupo en algunos equipos.
        for e in rng.sample(self._fleet, min(10, len(self._fleet))):
            for _ in range(rng.randint(1, 2)):
                when = now - timedelta(days=rng.randint(5, 170))
                log.append(self._chg(when, "EquipmentItem", e["internal_id"], "update",
                                     rng.choice(self._EMAILS), "cost_centre_id",
                                     str(rng.randint(10, 30)), str(rng.randint(10, 30))))
        for e in rng.sample(self._fleet, min(5, len(self._fleet))):
            when = now - timedelta(days=rng.randint(5, 170))
            log.append(self._chg(when, "EquipmentItem", e["internal_id"], "update",
                                 rng.choice(self._EMAILS), "equipment_group_id",
                                 str(rng.randint(20, 40)), str(rng.randint(20, 40))))
        log.sort(key=lambda x: x["changedAt"])
        return log

    def _append_live_change(self) -> None:
        rng = self._rng
        now = datetime.now()
        if rng.random() < 0.5 and self._fleet:
            e = rng.choice(self._fleet)
            before, after = rng.choice([("1", "2"), ("2", "1"), ("1", "3")])
            self._change_log.append(self._chg(
                now, "EquipmentItem", e["internal_id"], "update",
                rng.choice(self._EMAILS), "equipment_status_id", before, after))
        else:
            self._change_log.append(self._chg(
                now, "EquipmentRfid", str(rng.randint(1, 25)), "update",
                rng.choice(self._EMAILS), "rfid",
                f"56B{rng.randint(0x10000, 0xFFFFF):05X}",
                f"56B{rng.randint(0x10000, 0xFFFFF):05X}"))

    @staticmethod
    def _parse(iso: str):
        try:
            return datetime.fromisoformat(iso).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

    def _build_tanks(self) -> list[dict]:
        """Nodos de tanque en forma GraphQL (id/code/virtual/parentTank/...)."""
        nodes = []
        for i, (code, desc, prod, virtual, cap, parent, ttype) in enumerate(_TANKS_SPEC, start=1):
            nodes.append({
                "id": str(i), "code": code, "description": desc, "name": desc,
                "virtual": virtual, "enabled": True, "capacity": f"{cap:.2f}",
                "volumeUnit": "L",
                "product": {"code": prod[:4].upper(), "description": prod},
                "parentTank": {"code": parent} if parent else None,
                "tankType": {"description": ttype},
            })
        return nodes

    def _build_reconciliations(self, rng: random.Random) -> list[dict]:
        """Reconciliacion diaria sintetica (45 dias) por tanque de combustible.

        `volume` = error de reconciliacion = (closing-opening) - (inflow-outflow),
        que aqui es el 'ruido' del sensor (pequena discrepancia ~1%)."""
        recs: list[dict] = []
        rid = 1000
        today = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        for code, desc, prod, base, in_avg, out_avg in _RECON_SPEC:
            opening = base
            for d in range(45, 0, -1):
                period_end = today - timedelta(days=d - 1)
                period_start = period_end - timedelta(days=1)
                inflow = round(rng.uniform(0.7, 1.3) * in_avg, 2) if (in_avg and rng.random() < 0.5) else 0.0
                outflow = round(rng.uniform(0.7, 1.3) * out_avg, 2)
                noise = round(rng.uniform(-0.012, 0.012) * (base or 1.0), 2)
                closing = round(opening + inflow - outflow + noise, 2)
                error = round((closing - opening) - (inflow - outflow), 2)
                recs.append({
                    "id": str(rid),
                    "periodStart": _iso(period_start),
                    "periodEnd": _iso(period_end),
                    "openingStock": f"{opening:.2f}",
                    "closingStock": f"{closing:.2f}",
                    "inflowVolume": f"{inflow:.2f}",
                    "outflowVolume": f"{outflow:.2f}",
                    "volume": f"{error:.2f}",
                    "status": rng.choices(["all_ok", "unconfirmed", "pending"],
                                          weights=[85, 10, 5])[0],
                    "recordUpdatedAt": _iso(period_end + timedelta(hours=1)),
                    "target": {"code": code, "description": desc},
                    "product": {"code": prod[:4].upper(), "description": prod},
                })
                rid += 1
                opening = closing
        return recs

    def _make_movement(self) -> dict:
        rng = self._rng
        self._seq += 1
        now = datetime.now()
        e = rng.choice(self._fleet)

        kind = rng.choices([KIND_DISPENSE, KIND_DELIVERY, KIND_TRANSFER],
                           weights=[80, 8, 12])[0]
        mtype = rng.choices(
            [TYPE_AUTO, TYPE_MANUAL, TYPE_KEY_BYPASS, TYPE_SUP_OVERRIDE,
             TYPE_SPILLAGE, TYPE_UNAUTHORISED],
            weights=[70, 16, 5, 4, 2, 3])[0]

        if kind == KIND_DELIVERY:
            volume = rng.uniform(15000, 35000)
        elif kind == KIND_TRANSFER:
            volume = rng.uniform(2000, 24000)
        else:  # DISPENSE: ~6% sobrellenado (excede el SFL del equipo); el resto bajo.
            sfl = e.get("sfl", 1893.0)
            if rng.random() < 0.06:
                volume = sfl * rng.uniform(1.05, 1.4)
            else:
                volume = rng.uniform(50, sfl * 0.95)

        # Contaminacion: casi siempre limpia, a veces un pico.
        if rng.random() < 0.12:
            c4, c6, c14 = rng.randint(19, 23), rng.randint(17, 21), rng.randint(14, 18)
        else:
            c4, c6, c14 = rng.randint(12, 17), rng.randint(10, 15), rng.randint(8, 12)

        node = {
            "kind": kind,
            "id": f"SIM-{self._seq:08d}",
            "type": mtype,
            "status": "all_ok",
            "recordCollectedAt": _iso(now),
            "recordCreatedAt": _iso(now),
            "recordUpdatedAt": _iso(now),
            "transactionTemperature": round(rng.uniform(24, 38), 1),
            "peakFlowRate": f"{rng.uniform(40, 130):.1f}",
            "maxContamination4": c4 + 1, "avgContamination4": c4, "medContamination4": c4,
            "maxContamination6": c6 + 1, "avgContamination6": c6, "medContamination6": c6,
            "maxContamination14": c14 + 1, "avgContamination14": c14, "medContamination14": c14,
            "gpsCoordinates": f"{rng.uniform(5.0, 5.3):.5f},{rng.uniform(-54.2, -53.9):.5f}",
            "cost": f"{volume * rng.uniform(0.9, 1.3):.2f}",
            "rebateAmount": f"{volume * 0.05:.2f}",
            "movementType": None,
            "operator": rng.choice(["J. Doe", "M. Lee", "A. Singh", "R. Gomez"]),
            "product": {"code": e["product"], "description": e["product"].title()},
            "costCentre": {"code": e["cost_centre"], "description": e["cost_centre"]},
            "equipmentGroup": {"code": e["category"], "description": e["group"]},
            "equipmentCategory": {"code": e["category"], "description": e["group"]},
            "site": _SITE,
            "adaptMac": {"code": rng.choice(self._adaptmacs)["code"]},
        }

        if kind == KIND_DISPENSE:
            node["volume"] = f"{volume:.1f}"
            smu = str(e["smu"] + rng.randint(0, 5000))
            node["smuValue"] = smu
            node["smuType"] = e["interval_type"]
            node["source"] = {"code": "T-LFO", "name": rng.choice(["LFO Main", "Tank 084X"])}
            node["target"] = {"equipmentId": e["equipment_id"],
                              "description": e["description"], "status": e["status"]}
            node["fieldUser"] = {"name": node["operator"]}
            # Campos de hardware (espejo del export real): SMU crudo/calculado +
            # fuente, y la manguera/medidor con su caudal promedio y duracion.
            node["rawSmuValue"] = smu
            node["calculatedSmuValue"] = smu
            node["smuSource"] = "adaptsmu"
            node["smuValueSource"] = "adaptsmu"
            meter = rng.choice(["MER.1.1.1", "MER.2.1.2", "MER.4.1.1", "MER.6.1.1"])
            node["meter"] = {"code": meter, "description": f"LFO Lane {meter}", "erpReference": ""}
            peak = float(node["peakFlowRate"])
            node["averageFlowRate"] = f"{peak * rng.uniform(0.7, 0.95):.2f}"
            node["duration"] = str(int(max(1, volume / max(1.0, peak) * 60)))
        elif kind == KIND_DELIVERY:
            node["volume"] = f"{volume:.1f}"
            node["volumeSource"] = rng.choice([SOURCE_METER, SOURCE_DOCKET])
            node["secondaryVolume"] = f"{volume * 0.99:.1f}"
            node["secondaryVolumeSource"] = SOURCE_DOCKET
            node["docketNumber"] = f"D-{rng.randint(10000, 99999)}"
            node["driver"] = node["operator"]
            node["company"] = "Fuel Supplier Co"
            node["target"] = {"code": "T-LFO", "name": "LFO Main"}
        else:  # TRANSFER
            node["source"] = {"code": "T-LFO", "name": "LFO Main"}
            node["target"] = {"code": "T-ST", "name": "Service Tank"}
            if e["is_service_truck"]:
                if mtype == TYPE_KEY_BYPASS:
                    volume = rng.uniform(20000, 30000)
                node["serviceTruck"] = {"equipmentId": e["equipment_id"],
                                        "description": e["description"]}
            node["volume"] = f"{volume:.1f}"
        return node
