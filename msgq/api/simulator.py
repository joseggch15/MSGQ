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
        self._seq = 0
        self._equipment_sent = False

    # -- contrato DataSource -----------------------------------------------

    async def fetch_movements(self, updated_from: datetime | None) -> list[dict]:
        n = self._rng.randint(3, 12)
        return [self._make_movement() for _ in range(n)]

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
            "rfidTags": [f"E280{rng.randint(10**11, 10**12 - 1)}"],
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
        # RFID: alta de tag + cambios posteriores en algunos.
        for i in range(1, 26):
            when = now - timedelta(days=rng.randint(10, 179))
            tag = f"56B{rng.randint(0x10000, 0xFFFFF):05X}"
            log.append(self._chg(when, "EquipmentRfid", str(i), "create",
                                 rng.choice(self._EMAILS), "rfid", None, tag))
            if rng.random() < 0.35:
                when2 = when + timedelta(days=rng.randint(5, 60))
                if when2 < now:
                    log.append(self._chg(when2, "EquipmentRfid", str(i), "update",
                                         rng.choice(self._EMAILS), "rfid", tag,
                                         f"56B{rng.randint(0x10000, 0xFFFFF):05X}"))
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
        else:
            volume = rng.uniform(50, 1800)

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
            node["smuValue"] = str(e["smu"] + rng.randint(0, 5000))
            node["smuType"] = e["interval_type"]
            node["source"] = {"code": "T-LFO", "name": rng.choice(["LFO Main", "Tank 084X"])}
            node["target"] = {"equipmentId": e["equipment_id"],
                              "description": e["description"], "status": e["status"]}
            node["fieldUser"] = {"name": node["operator"]}
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
