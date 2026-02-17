import math
from typing import Any, Dict, List, Tuple

from config import settings
from services import DatabaseService, MapboxService
from solver import RouteOptimizer

def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2) + math.cos(p1) * math.cos(p2) * (math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def parse_hhmm_to_seconds_from_6am(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        seconds = int(val)
        return seconds if seconds >= 0 else None

    s = str(val).strip()
    if not s:
        return None

    try:
        parts = s.split(":")
        if len(parts) < 2:
            return None
        hh = int(parts[0])
        mm = int(parts[1])
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return None
        seconds = (hh - 6) * 3600 + mm * 60
        return max(0, seconds)
    except (TypeError, ValueError):
        return None


# ----------------------------
# Capacity normalization helpers
# ----------------------------
def _med_skid_units() -> int:
    return int(getattr(settings, "MED_SKID_UNITS", getattr(settings, "LONG_SKID_UNITS", 20)))


def _large_skid_units() -> int:
    return int(getattr(settings, "LARGE_SKID_UNITS", 25))


def _small_skid_units() -> int:
    return int(getattr(settings, "SMALL_SKID_UNITS", 10))


def _frame_units() -> int:
    return int(getattr(settings, "FRAME_UNITS", 1))


def _vehicle_capacity_units() -> int:
    # Preferred new setting:
    # VEHICLE_CAPACITY_UNITS = 140 (== 7 med skids OR 14 small skids OR 140 frames)
    cap = getattr(settings, "VEHICLE_CAPACITY_UNITS", None)
    if cap is not None:
        return int(cap)

    # Backward compatible fallback: MAX_SKIDS means "med skids" capacity.
    # So convert to units.
    max_long = int(getattr(settings, "MAX_SKIDS", 7))
    return max_long * _med_skid_units()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    raw = str(value).strip().lower()
    return raw in {"true", "1", "yes", "y", "checked"}


def delivery_capacity_units(d) -> int:
    """
    Normalize mixed cargo into a single capacity unit system.
      - 1 med skid   = MED_SKID_UNITS   (default 20)
      - 1 large skid = LARGE_SKID_UNITS (default 25)
      - 1 small skid = SMALL_SKID_UNITS (default 10)
      - 1 frame      = FRAME_UNITS      (default 1)
      - 1 exp frame  = FRAME_UNITS      (default 1)
    Vehicle capacity is VEHICLE_CAPACITY_UNITS (default 140).
    """
    med_qty = int(getattr(d, "med_skid", getattr(d, "skid", 0)) or 0)
    large_qty = int(getattr(d, "large_skid", 0) or 0)
    small_qty = int(getattr(d, "small_skid", 0) or 0)
    frame_qty = int(getattr(d, "frame", 0) or 0)
    exp_frame_qty = int(getattr(d, "exp_frame", 0) or 0)

    units = (
        (med_qty * _med_skid_units())
        + (large_qty * _large_skid_units())
        + (small_qty * _small_skid_units())
        + ((frame_qty + exp_frame_qty) * _frame_units())
    )
    return max(0, int(units))


def _delivery_value(delivery: Any, key: str, default: Any = None) -> Any:
    if isinstance(delivery, dict):
        return delivery.get(key, default)
    return getattr(delivery, key, default)


def _normalize_pickup_vehicle(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in {"van", "vans", "van-only", "van only", "van_only"}:
        return "van"
    if raw in {"truck", "trucks", "truck-only", "truck only", "truck_only"}:
        return "truck"
    if raw in {"both", "any", "either", "all", "van/truck", "truck/van"}:
        return "both"
    return ""


def _parse_token_item_counts(token: str) -> Dict[str, int] | None:
    def _parse_field(field: str) -> int | None:
        key = f"_{field}"
        if key not in token:
            return None
        try:
            value = token.split(key, 1)[1].split("_", 1)[0]
            return max(0, int(value))
        except (TypeError, ValueError, IndexError):
            return None

    fields = ["Med", "Large", "Small", "Frame", "Exp", "Door", "Box"]
    out: Dict[str, int] = {}
    found = False
    for field in fields:
        parsed = _parse_field(field)
        if parsed is not None:
            found = True
            out[field.lower()] = parsed
        else:
            out[field.lower()] = 0
    return out if found else None


def _node_requires_truck_for_tokens(tokens: List[str], delivery_by_id: Dict[str, Any]) -> bool:
    van_only = False
    truck_only = False
    door_total = 0
    frame_total = 0
    exp_frame_total = 0
    skid_present = False
    total_units = 0
    has_delivery = False

    for raw_token in tokens or []:
        token = str(raw_token).strip()
        if not token or token.lower() in {"depot", "depot - reload"}:
            continue
        delivery_id = token.split("_Part", 1)[0].strip()
        delivery = delivery_by_id.get(delivery_id)
        if delivery is None:
            return True

        has_delivery = True
        if _to_bool(_delivery_value(delivery, "requires_truck", False)):
            return True

        pickup = _to_bool(_delivery_value(delivery, "pickup", False))
        if pickup:
            if _to_bool(_delivery_value(delivery, "requires_empty_truck", False)):
                truck_only = True
            pickup_vehicle = _normalize_pickup_vehicle(_delivery_value(delivery, "pickup_vehicle", ""))
            if pickup_vehicle == "van":
                van_only = True
            elif pickup_vehicle == "truck":
                truck_only = True

        token_counts = _parse_token_item_counts(token)
        if token_counts:
            med_qty = int(token_counts.get("med", 0) or 0)
            large_qty = int(token_counts.get("large", 0) or 0)
            small_qty = int(token_counts.get("small", 0) or 0)
            frame_qty = int(token_counts.get("frame", 0) or 0)
            exp_qty = int(token_counts.get("exp", 0) or 0)
            door_qty = int(token_counts.get("door", 0) or 0)
        else:
            med_qty = int(_delivery_value(delivery, "med_skid", _delivery_value(delivery, "skid", 0)) or 0)
            large_qty = int(_delivery_value(delivery, "large_skid", 0) or 0)
            small_qty = int(_delivery_value(delivery, "small_skid", 0) or 0)
            frame_qty = int(_delivery_value(delivery, "frame", 0) or 0)
            exp_qty = int(_delivery_value(delivery, "exp_frame", 0) or 0)
            door_qty = int(_delivery_value(delivery, "door", 0) or 0)

        if med_qty > 0 or large_qty > 0 or small_qty > 0:
            skid_present = True
        door_total += door_qty
        frame_total += frame_qty
        exp_frame_total += exp_qty
        total_units += (
            (med_qty * _med_skid_units())
            + (large_qty * _large_skid_units())
            + (small_qty * _small_skid_units())
            + ((frame_qty + exp_qty) * _frame_units())
        )

    if not has_delivery:
        return True
    if truck_only:
        return True
    if van_only and not truck_only:
        return False
    if skid_present and (door_total > 0 or frame_total > 0 or exp_frame_total > 0):
        return True
    if (
        door_total > int(getattr(RouteOptimizer, "VAN_DOOR_LIMIT", 3))
        or frame_total > int(getattr(RouteOptimizer, "VAN_FRAME_LIMIT", 3))
        or exp_frame_total > int(getattr(RouteOptimizer, "VAN_EXP_FRAME_LIMIT", 10))
    ):
        return True

    van_capacity_units = int(getattr(settings, "VAN_CAPACITY_UNITS", RouteOptimizer.VAN_CAPACITY_UNITS) or 0)
    if van_capacity_units <= 0:
        return total_units > 0
    return total_units > van_capacity_units


def _build_node_van_eligibility(node_to_tokens: Dict[int, List[str]], delivery_by_id: Dict[str, Any]) -> Dict[int, bool]:
    node_van_eligible: Dict[int, bool] = {}
    for node_idx, tokens in (node_to_tokens or {}).items():
        if int(node_idx) == 0:
            continue
        requires_truck = _node_requires_truck_for_tokens(tokens or [], delivery_by_id)
        node_van_eligible[int(node_idx)] = not requires_truck
    return node_van_eligible


def _geocode_missing(deliveries, db: DatabaseService, mapbox: MapboxService):
    geocode_failures = []
    for d in deliveries:
        # Treat missing coords as None or 0
        lat = getattr(d, "lat", None)
        lon = getattr(d, "lon", None)
        try:
            lat_f = float(lat) if lat is not None else 0.0
            lon_f = float(lon) if lon is not None else 0.0
        except Exception:
            lat_f, lon_f = 0.0, 0.0

        if lat_f == 0.0 or lon_f == 0.0:
            lat2, lon2 = mapbox.get_coordinates(d.job_address)
            if lat2 and lon2:
                d.lat, d.lon = lat2, lon2
                if settings.UPDATE_AIRTABLE_COORDS:
                    job_id = getattr(d, "job_id", None) or getattr(d, "job", None)
                    if job_id:
                        db.update_job_coordinates(str(job_id), lat2, lon2)
            else:
                geocode_failures.append({"id": str(d.id), "address": d.job_address})
    return geocode_failures


def _build_nodes_and_coords(deliveries) -> Tuple[List[Tuple[float, float]], Dict[int, List[str]]]:
    """
    Build node coordinates and node->job tokens, grouping deliveries at identical coords.
    Enforces per-stop capacity using *normalized units*:
        VEHICLE_CAPACITY_UNITS (default 140) == 7 med skids OR 14 small skids OR 140 frames
    Oversized single deliveries are split into multiple tokens at the same node.
    """
    coords_list: List[Tuple[float, float]] = [(settings.DEPOT_LON, settings.DEPOT_LAT)]
    node_to_jobs: Dict[int, List[str]] = {0: ["Depot"]}

    capacity_units = _vehicle_capacity_units()
    med_units = _med_skid_units()
    large_units = _large_skid_units()
    small_units = _small_skid_units()
    frame_units = _frame_units()

    def _split_delivery_parts(
        med_qty: int,
        large_qty: int,
        small_qty: int,
        frame_qty: int,
        exp_frame_qty: int,
        door_qty: int,
        box_qty: int,
    ) -> List[Dict[str, int]]:
        remaining = {
            "med": max(0, int(med_qty)),
            "large": max(0, int(large_qty)),
            "small": max(0, int(small_qty)),
            "frame": max(0, int(frame_qty)),
            "exp": max(0, int(exp_frame_qty)),
        }
        weights = {
            "large": max(1, int(large_units)),
            "med": max(1, int(med_units)),
            "small": max(1, int(small_units)),
            "frame": max(1, int(frame_units)),
            "exp": max(1, int(frame_units)),
        }
        order = ["large", "med", "small", "frame", "exp"]

        parts: List[Dict[str, int]] = []
        while any(remaining[k] > 0 for k in remaining):
            part = {"med": 0, "large": 0, "small": 0, "frame": 0, "exp": 0, "door": 0, "box": 0}
            cap = int(capacity_units)
            for key in order:
                if remaining[key] <= 0:
                    continue
                weight = weights[key]
                if weight <= 0:
                    continue
                max_fit = cap // weight
                if max_fit <= 0:
                    continue
                take = min(remaining[key], max_fit)
                if take > 0:
                    part[key] = int(take)
                    remaining[key] -= int(take)
                    cap -= int(take) * weight

            if all(part[k] == 0 for k in ["med", "large", "small", "frame", "exp"]):
                # Fallback: force at least one item if capacity is unexpectedly too small.
                for key in order:
                    if remaining[key] > 0:
                        part[key] = 1
                        remaining[key] -= 1
                        break

            parts.append(part)

        if parts:
            # Distribute non-capacity items across parts (integer, round-robin).
            for key, qty in [("door", door_qty), ("box", box_qty)]:
                qty = max(0, int(qty))
                if qty <= 0:
                    continue
                idx = 0
                while qty > 0:
                    parts[idx % len(parts)][key] += 1
                    idx += 1
                    qty -= 1

        return parts

    groups: Dict[Tuple[float, float], List[Any]] = {}
    for d in deliveries:
        if not d.lat or not d.lon:
            continue
        key = (float(d.lon), float(d.lat))
        groups.setdefault(key, []).append(d)

        # Optional: expose computed units for downstream logic/solver/UI debugging
        try:
            setattr(d, "capacity_units", delivery_capacity_units(d))
        except (AttributeError, TypeError, ValueError):
            continue

    for coord in sorted(groups.keys()):
        jobs = groups[coord]
        curr_units = 0
        curr_tokens: List[str] = []

        for j in jobs:
            units = delivery_capacity_units(j)
            med_qty = int(getattr(j, "med_skid", getattr(j, "skid", 0)) or 0)
            large_qty = int(getattr(j, "large_skid", 0) or 0)
            small_qty = int(getattr(j, "small_skid", 0) or 0)
            frame_qty = int(getattr(j, "frame", 0) or 0)
            exp_frame_qty = int(getattr(j, "exp_frame", 0) or 0)
            door_qty = int(getattr(j, "door", 0) or 0)
            box_qty = int(getattr(j, "box", 0) or 0)

            # Single delivery exceeds per-stop capacity => split into multiple parts at same coord
            if units > capacity_units:
                if curr_tokens:
                    node_to_jobs[len(coords_list)] = curr_tokens
                    coords_list.append(coord)
                    curr_tokens = []
                    curr_units = 0

                parts = _split_delivery_parts(
                    med_qty,
                    large_qty,
                    small_qty,
                    frame_qty,
                    exp_frame_qty,
                    door_qty,
                    box_qty,
                )
                p = 1
                for part in parts:
                    token = (
                        f"{j.id}_Part{p}"
                        f"_Med{part['med']}"
                        f"_Large{part['large']}"
                        f"_Small{part['small']}"
                        f"_Frame{part['frame']}"
                        f"_Exp{part['exp']}"
                        f"_Door{part['door']}"
                        f"_Box{part['box']}"
                    )
                    node_to_jobs[len(coords_list)] = [token]
                    coords_list.append(coord)
                    p += 1

            else:
                # Pack multiple deliveries at the same coord into a single node, up to capacity
                if curr_units + units > capacity_units:
                    node_to_jobs[len(coords_list)] = curr_tokens
                    coords_list.append(coord)
                    curr_tokens = [str(j.id)]
                    curr_units = units
                else:
                    curr_tokens.append(str(j.id))
                    curr_units += units

        if curr_tokens:
            node_to_jobs[len(coords_list)] = curr_tokens
            coords_list.append(coord)

    return coords_list, node_to_jobs


def _normalize_delivery_for_ui(d) -> Dict[str, Any]:
    med_qty = int(getattr(d, "med_skid", getattr(d, "skid", 0)) or 0)
    large_qty = int(getattr(d, "large_skid", 0) or 0)
    small_qty = int(getattr(d, "small_skid", 0) or 0)
    frame_qty = int(getattr(d, "frame", 0) or 0)
    exp_frame_qty = int(getattr(d, "exp_frame", 0) or 0)
    units = (
        (med_qty * _med_skid_units())
        + (large_qty * _large_skid_units())
        + (small_qty * _small_skid_units())
        + ((frame_qty + exp_frame_qty) * _frame_units())
    )

    pickup_flag = bool(getattr(d, "pickup", False))
    pickup_vehicle = str(getattr(d, "pickup_vehicle", "") or "").strip().lower()
    if pickup_flag and not pickup_vehicle:
        pickup_vehicle = "both"
    if not pickup_flag:
        pickup_vehicle = ""

    return {
        "id": str(d.id),
        "job_name": getattr(d, "job_name", ""),
        "job_address": getattr(d, "job_address", ""),
        "sales_order": getattr(d, "sales_order", ""),
        "packing_slip": getattr(d, "packing_slip", ""),
        "driver": getattr(d, "driver", ""),
        "lat": float(getattr(d, "lat", 0) or 0),
        "lon": float(getattr(d, "lon", 0) or 0),
        "door": int(getattr(d, "door", 0) or 0),
        "frame": int(getattr(d, "frame", 0) or 0),
        "exp_frame": int(getattr(d, "exp_frame", 0) or 0),
        "med_skid": med_qty,
        "small_skid": small_qty,
        "large_skid": large_qty,
        "box": int(getattr(d, "box", 0) or 0),
        "requires_truck": bool(getattr(d, "requires_truck", False)),
        "pickup": pickup_flag,
        "pickup_vehicle": pickup_vehicle,
        "requires_empty_truck": bool(getattr(d, "requires_empty_truck", False)),
        "window_start": getattr(d, "window_start", None),
        "window_end": getattr(d, "window_end", None),
        # Helpful for debugging and capacity UX:
        "capacity_units": int(units),
    }


def _format_routes_for_ui(routes, deliveries):
    """
    Convert solver output tokens into a UI-friendly structure with job names.
    Keeps IDs for traceability.
    """
    delivery_by_id = {str(d.id).strip(): d for d in deliveries}

    formatted = []
    for r in routes or []:
        raw_stop_meta = r.get("stops") or []
        stops = []
        stop_times = []
        for stop in r.get("jobs", []):
            stop_items = []
            for token in stop:
                t = str(token).strip()
                if t.lower() == "depot":
                    stop_items.append({"type": "depot", "label": "Depot"})
                    continue
                if t.lower() == "depot - reload":
                    stop_items.append({"type": "depot", "label": "Depot - Reload"})
                    continue

                base_id = t.split("_Part")[0].strip()
                d = delivery_by_id.get(base_id)

                job_name = (d.job_name if d else f"Unknown ({base_id})") or f"Unknown ({base_id})"

                item = {
                    "type": "delivery",
                    "id": base_id,
                    "job_name": job_name,
                }

                if "_Part" in t:
                    try:
                        part_str = t.split("_Part", 1)[1]
                        part_no = int(part_str.split("_", 1)[0].split("_Qty", 1)[0].split("_Units", 1)[0])
                        item["part_no"] = part_no
                    except (ValueError, IndexError):
                        pass

                # Backward compat: old split format used _Qty{n} (long skids). Keep parsing.
                if "_Qty" in t:
                    try:
                        item["qty"] = int(t.split("_Qty")[-1])
                    except (ValueError, IndexError):
                        pass

                # New split format: _Units{n} (normalized capacity units)
                if "_Units" in t:
                    try:
                        item["units"] = int(t.split("_Units")[-1])
                    except (ValueError, IndexError):
                        pass
                if "_Med" in t:
                    try:
                        item["med_skid"] = int(t.split("_Med")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass
                if "_Large" in t:
                    try:
                        item["large_skid"] = int(t.split("_Large")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass
                if "_Small" in t:
                    try:
                        item["small_skid"] = int(t.split("_Small")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass
                if "_Frame" in t:
                    try:
                        item["frame"] = int(t.split("_Frame")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass
                if "_Exp" in t:
                    try:
                        item["exp_frame"] = int(t.split("_Exp")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass
                if "_Door" in t:
                    try:
                        item["door"] = int(t.split("_Door")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass
                if "_Box" in t:
                    try:
                        item["box"] = int(t.split("_Box")[-1].split("_", 1)[0])
                    except (ValueError, IndexError):
                        pass

                stop_items.append(item)
            stops.append(stop_items)

            meta = raw_stop_meta[len(stops) - 1] if len(raw_stop_meta) >= len(stops) else {}
            stop_times.append(
                {
                    "arrival_hhmm": meta.get("arrival_hhmm"),
                    "departure_hhmm": meta.get("departure_hhmm"),
                    "arrival_s": meta.get("arrival_s"),
                    "departure_s": meta.get("departure_s"),
                    "stop_units": meta.get("stop_units"),
                    "stop_units_signed": meta.get("stop_units_signed"),
                }
            )

        stop_nodes = []
        for stop in r.get("stops", []) or []:
            node = stop.get("node")
            if node is None:
                continue
            stop_nodes.append(int(node))

        formatted.append(
            {
                "vehicle_id": r.get("v_id"),
                "vehicle_type": r.get("v_type"),
                "skids": r.get("skids"),
                "total_distance_m": r.get("total_distance_m"),
                "total_time_s": r.get("total_time_s"),
                "stops": stops,
                "stop_times": stop_times,
                "stop_nodes": stop_nodes,
            }
        )

    return formatted


def generate_tomorrow_plan(
    trucks: int | None = None,
    vans: int = 0,
    force_refresh: bool = False,
    target_date: str | None = None,
) -> Dict[str, Any]:
    db = DatabaseService()
    mapbox = MapboxService()

    deliveries = db.get_tomorrow_deliveries(force_refresh=force_refresh, target_date=target_date)
    target_date = getattr(db, "_last_target_date", None)
    if not deliveries:
        return {
            "deliveries": [],
            "routes": [],
            "message": "No deliveries found.",
            "target_date": target_date,
        }

    geocode_failures = _geocode_missing(deliveries, db, mapbox)

    coords_list, node_to_jobs = _build_nodes_and_coords(deliveries)

    if len(coords_list) <= 1:
        return {
            "deliveries": [],
            "routes": [],
            "message": "No valid delivery locations to route.",
            "target_date": target_date,
        }

    # ---- Fleet selection from UI inputs ----
    if trucks is None:
        trucks = settings.PHYSICAL_TRUCKS
    trucks = max(0, int(trucks))
    vans = max(0, int(vans))

    fleet = (["truck"] * trucks) + (["van"] * vans)

    if len(fleet) == 0:
        return {
            "deliveries": [_normalize_delivery_for_ui(d) for d in deliveries],
            "routes": [],
            "geocode_failures": geocode_failures,
            "meta": {
                "delivery_count": len(deliveries),
                "node_count": len(coords_list),
                "vehicle_count": 0,
                "error": "No vehicles selected (set trucks/vans above 0).",
                "plan_date": target_date,
            },
        }

    matrix = mapbox.get_matrix(coords_list, force_refresh=force_refresh)

    node_is_downtown = {0: False}
    for node_idx in range(1, len(coords_list)):
        lon, lat = coords_list[node_idx]
        node_is_downtown[node_idx] = (
            haversine_km(lon, lat, settings.DT_CENTER_LON, settings.DT_CENTER_LAT) <= settings.DT_RADIUS_KM
        )

    node_time_windows = {}
    for node_idx in range(1, len(coords_list)):
        tokens = node_to_jobs.get(node_idx, [])
        starts = []
        ends = []

        for tok in tokens:
            base_id = str(tok).split("_Part")[0].strip()
            d = next((x for x in deliveries if str(x.id).strip() == base_id), None)
            if not d:
                continue

            ws = parse_hhmm_to_seconds_from_6am(getattr(d, "window_start", None))
            we = parse_hhmm_to_seconds_from_6am(getattr(d, "window_end", None))
            if ws is not None and we is not None:
                starts.append(ws)
                ends.append(we)

        if starts and ends:
            node_time_windows[node_idx] = (max(starts), min(ends))

    # IMPORTANT:
    # physical_truck_count should match the number of trucks you actually made available,
    # otherwise the solver might mark some trucks as "overflow" and assign huge fixed costs.
    #
    # NOTE ON SMALL SKIDS:
    # This file now groups/splits stops using normalized capacity units.
    # Default profile: 140 units = 7 med skids = 14 small skids = 140 frames.
    # Ensure RouteOptimizer capacity logic is updated to also use the same unit model, e.g.:
    #   demand_units = d.med_skid*MED_SKID_UNITS + d.large_skid*LARGE_SKID_UNITS
    #                + d.small_skid*SMALL_SKID_UNITS + (d.frame+d.exp_frame)*FRAME_UNITS
    #   vehicle_capacity = VEHICLE_CAPACITY_UNITS
    penalty_profile = {
        "truck_cost_factor": settings.SOLVER_TRUCK_COST_FACTOR,
        "truck_time_factor": settings.SOLVER_TRUCK_TIME_FACTOR,
        "truck_serve_van_eligible_penalty": settings.SOLVER_TRUCK_SERVE_VAN_ELIGIBLE_PENALTY,
        "time_span_cost_coeff": settings.SOLVER_TIME_SPAN_COST_COEFFICIENT,
        "downtown_soft_latest_s": settings.SOLVER_DOWNTOWN_SOFT_LATEST_SECONDS,
        "downtown_hard_latest_s": settings.SOLVER_DOWNTOWN_HARD_LATEST_SECONDS,
        "downtown_late_penalty_per_sec": settings.SOLVER_DOWNTOWN_LATE_PENALTY_PER_SECOND,
        "van_fixed_cost": settings.SOLVER_VAN_FIXED_COST,
        "truck_fixed_cost_physical": settings.SOLVER_TRUCK_FIXED_COST_PHYSICAL,
        "truck_fixed_cost_overflow": settings.SOLVER_TRUCK_FIXED_COST_OVERFLOW,
        "drop_node_penalty": settings.SOLVER_DROP_NODE_PENALTY,
        "shift_seconds": settings.SOLVER_SHIFT_SECONDS,
        "waiting_slack_seconds": settings.SOLVER_WAITING_SLACK_SECONDS,
        "search_time_limit_s": settings.SOLVER_TIME_LIMIT_SECONDS,
    }

    opt = RouteOptimizer(
        matrix["durations"],
        matrix["distances"],
        node_to_jobs,
        deliveries,
        fleet,
        physical_truck_count=trucks,
        node_is_downtown=node_is_downtown,
        node_time_windows=node_time_windows,
        penalty_profile=penalty_profile,
    )

    raw_results = opt.solve() or []
    deliveries_payload = [_normalize_delivery_for_ui(d) for d in deliveries]
    formatted_routes = _format_routes_for_ui(raw_results, deliveries)
    meta_payload = {
        "delivery_count": len(deliveries),
        "node_count": len(coords_list),
        "vehicle_count": len(fleet),
        "fleet": {"trucks": trucks, "vans": vans},
        "plan_date": target_date,
        "depot_lat": settings.DEPOT_LAT,
        "depot_lon": settings.DEPOT_LON,
        "capacity": {
            "vehicle_capacity_units": _vehicle_capacity_units(),
            "med_skid_units": _med_skid_units(),
            "large_skid_units": _large_skid_units(),
            "small_skid_units": _small_skid_units(),
            "frame_units": _frame_units(),
        },
        "solver_penalties": penalty_profile,
    }
    return {
        "deliveries": deliveries_payload,
        "routes": formatted_routes,
        "geocode_failures": geocode_failures,
        "meta": meta_payload,
        "target_date": target_date,
    }


def recalculate_tomorrow_plan(routes: List[Dict[str, Any]], force_refresh: bool = False) -> Dict[str, Any]:
    db = DatabaseService()
    mapbox = MapboxService()

    deliveries = db.get_tomorrow_deliveries(force_refresh=force_refresh)
    if not deliveries:
        return {"routes": [], "message": "No deliveries found."}

    geocode_failures = _geocode_missing(deliveries, db, mapbox)
    coords_list, node_to_jobs = _build_nodes_and_coords(deliveries)

    delivery_by_id = {str(getattr(d, "id", "")).strip(): d for d in deliveries if str(getattr(d, "id", "")).strip()}
    node_van_eligible = _build_node_van_eligibility(node_to_jobs, delivery_by_id)

    if len(coords_list) <= 1:
        return {"routes": [], "message": "No valid delivery locations to route."}

    matrix = mapbox.get_matrix(coords_list, force_refresh=force_refresh)
    distances = matrix.get("distances", [])
    durations = matrix.get("durations", [])

    TRUCK_TIME_FACTOR = 1.10
    TRUCK_SERVICE_TIME = 30 * 60
    VAN_SERVICE_TIME = 10 * 60

    recalced = []
    for route in routes or []:
        v_type = (route.get("vehicle_type") or route.get("v_type") or "truck").lower()
        nodes = route.get("stop_nodes") or route.get("nodes") or route.get("stops") or []

        if nodes and isinstance(nodes[0], dict):
            nodes = [n.get("node") for n in nodes if n and n.get("node") is not None]

        stop_nodes = []
        for n in nodes:
            try:
                n_int = int(n)
            except Exception:
                continue
            if n_int == 0:
                continue
            stop_nodes.append(n_int)

        total_distance = 0
        total_time = 0
        prev = 0
        stop_times = []

        stop_times.append(
            {
                "arrival_s": 0,
                "departure_s": 0,
            }
        )

        for node in stop_nodes:
            leg_dist = distances[prev][node] if distances else 0
            leg_time = durations[prev][node] if durations else 0

            if v_type == "truck":
                leg_time = int(leg_time * TRUCK_TIME_FACTOR)
                if node != 0 and node_van_eligible.get(node, False):
                    svc = VAN_SERVICE_TIME
                else:
                    svc = TRUCK_SERVICE_TIME
            else:
                svc = VAN_SERVICE_TIME

            total_distance += int(leg_dist)
            total_time += int(leg_time)
            arr = int(total_time)
            total_time += svc
            dep = int(total_time)
            stop_times.append(
                {
                    "arrival_s": arr,
                    "departure_s": dep,
                }
            )
            prev = node

        leg_dist = distances[prev][0] if distances else 0
        leg_time = durations[prev][0] if durations else 0
        if v_type == "truck":
            leg_time = int(leg_time * TRUCK_TIME_FACTOR)
        total_distance += int(leg_dist)
        total_time += int(leg_time)

        stop_times.append(
            {
                "arrival_s": int(total_time),
                "departure_s": int(total_time),
            }
        )

        recalced.append(
            {
                "vehicle_id": route.get("vehicle_id"),
                "vehicle_type": v_type,
                "total_distance_m": int(total_distance),
                "total_time_s": int(total_time),
                "stop_times": stop_times,
            }
        )

    return {
        "routes": recalced,
        "geocode_failures": geocode_failures,
    }


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _seconds_to_hhmm_from_6am(seconds: int) -> str:
    total = (6 * 3600) + max(0, int(seconds))
    hh = (total // 3600) % 24
    mm = (total % 3600) // 60
    return f"{hh:02d}:{mm:02d}"


def recalculate_manual_plan(
    routes: List[Dict[str, Any]],
    deliveries: List[Dict[str, Any]],
    depot_lat: float | None = None,
    depot_lon: float | None = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    mapbox = MapboxService()

    depot_lat_f = _to_float(depot_lat)
    depot_lon_f = _to_float(depot_lon)
    if depot_lat_f is None:
        depot_lat_f = float(getattr(settings, "DEPOT_LAT", 0.0) or 0.0)
    if depot_lon_f is None:
        depot_lon_f = float(getattr(settings, "DEPOT_LON", 0.0) or 0.0)

    coords_list: List[Tuple[float, float]] = [(depot_lon_f, depot_lat_f)]
    coord_node_index: Dict[Tuple[float, float], int] = {}
    node_by_delivery_id: Dict[str, int] = {}
    missing_delivery_coordinates: List[str] = []

    for item in deliveries or []:
        if not isinstance(item, dict):
            continue
        delivery_id = str(item.get("id") or item.get("delivery_id") or "").strip()
        if not delivery_id:
            continue
        lat = _to_float(item.get("lat"))
        lon = _to_float(item.get("lon"))
        if lat is None or lon is None:
            missing_delivery_coordinates.append(delivery_id)
            continue
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            missing_delivery_coordinates.append(delivery_id)
            continue
        # Group identical coordinates onto the same node to keep matrix size bounded.
        coord_key = (round(float(lon), 6), round(float(lat), 6))
        node_idx = coord_node_index.get(coord_key)
        if node_idx is None:
            node_idx = len(coords_list)
            coords_list.append((float(lon), float(lat)))
            coord_node_index[coord_key] = node_idx
        node_by_delivery_id[delivery_id] = node_idx

    manual_delivery_by_id: Dict[str, Dict[str, Any]] = {}
    node_to_tokens: Dict[int, List[str]] = {}
    for item in deliveries or []:
        if not isinstance(item, dict):
            continue
        delivery_id = str(item.get("id") or item.get("delivery_id") or "").strip()
        if not delivery_id:
            continue
        manual_delivery_by_id[delivery_id] = item
        node_idx = node_by_delivery_id.get(delivery_id)
        if node_idx is None:
            continue
        node_to_tokens.setdefault(int(node_idx), []).append(delivery_id)

    node_van_eligible = _build_node_van_eligibility(node_to_tokens, manual_delivery_by_id)

    distances: List[List[float]] = []
    durations: List[List[float]] = []

    if len(coords_list) >= 2:
        matrix_max_coords = int(getattr(settings, "MANUAL_RECALC_MATRIX_MAX_COORDS", 25) or 25)
        if len(coords_list) > matrix_max_coords:
            raise ValueError(
                "Manual recalculation exceeds Mapbox Matrix limit: "
                f"{len(coords_list)} coordinates (max {matrix_max_coords}). "
                "Split routes or reduce unique stops."
            )
        matrix = mapbox.get_matrix(coords_list, force_refresh=force_refresh)
        distances = matrix.get("distances", []) or []
        durations = matrix.get("durations", []) or []

    truck_time_factor = 1.10
    truck_service_time_s = 30 * 60
    van_service_time_s = 10 * 60
    reload_service_time_s = int(getattr(settings, "RELOAD_SERVICE_TIME_S", 0) or 0)

    def _delivery_id_from_stop_item(stop_item: Any) -> str:
        if isinstance(stop_item, dict):
            raw_id = stop_item.get("id") or stop_item.get("delivery_id")
            return str(raw_id or "").strip()
        if isinstance(stop_item, (str, int)):
            token = str(stop_item).strip()
            if not token:
                return ""
            lowered = token.lower()
            if lowered in {"depot", "depot - reload"}:
                return ""
            return token.split("_Part", 1)[0].strip()
        return ""

    def _is_depot_stop(stop_group: List[Any]) -> bool:
        for item in stop_group:
            if isinstance(item, dict):
                if str(item.get("type") or "").strip().lower() == "depot":
                    return True
                label = str(item.get("label") or "").strip().lower()
                if label in {"depot", "depot - reload"}:
                    return True
            elif isinstance(item, (str, int)):
                token = str(item).strip().lower()
                if token in {"depot", "depot - reload"}:
                    return True
        return False

    recalced: List[Dict[str, Any]] = []
    total_distance_all = 0
    total_duration_all = 0
    total_stop_count = 0

    for route in routes or []:
        if not isinstance(route, dict):
            continue
        v_type = str(route.get("vehicle_type") or route.get("v_type") or "truck").strip().lower()
        raw_stops = route.get("stops") if isinstance(route.get("stops"), list) else []
        route_nodes: List[int] = []

        for stop in raw_stops:
            group = stop if isinstance(stop, list) else [stop]
            if _is_depot_stop(group):
                route_nodes.append(0)
                continue
            found_node: int | None = None
            for stop_item in group:
                delivery_id = _delivery_id_from_stop_item(stop_item)
                if not delivery_id:
                    continue
                node_idx = node_by_delivery_id.get(delivery_id)
                if node_idx is not None:
                    found_node = node_idx
                    break
            if found_node is not None:
                route_nodes.append(int(found_node))

        if not route_nodes and isinstance(route.get("stop_nodes"), list):
            for raw_node in route.get("stop_nodes") or []:
                try:
                    node_val = int(raw_node)
                except Exception:
                    continue
                if node_val < 0:
                    continue
                if len(coords_list) > 0 and node_val >= len(coords_list):
                    continue
                route_nodes.append(node_val)

        total_distance = 0
        total_time = 0
        prev_node = 0
        stop_times: List[Dict[str, Any]] = []

        for node in route_nodes:
            leg_dist = 0
            leg_time = 0
            if (
                distances
                and durations
                and prev_node < len(distances)
                and node < len(distances[prev_node])
                and prev_node < len(durations)
                and node < len(durations[prev_node])
            ):
                leg_dist = int(distances[prev_node][node] or 0)
                leg_time = int(durations[prev_node][node] or 0)

            if v_type == "truck":
                leg_time = int(leg_time * truck_time_factor)
                if node != 0 and node_van_eligible.get(node, False):
                    service_time = van_service_time_s
                else:
                    service_time = truck_service_time_s
            else:
                service_time = van_service_time_s

            if node == 0:
                service_time = reload_service_time_s

            total_distance += leg_dist
            total_time += leg_time
            arrival_s = int(total_time)
            total_time += service_time
            departure_s = int(total_time)
            stop_times.append(
                {
                    "arrival_s": arrival_s,
                    "departure_s": departure_s,
                    "arrival_hhmm": _seconds_to_hhmm_from_6am(arrival_s),
                    "departure_hhmm": _seconds_to_hhmm_from_6am(departure_s),
                }
            )
            prev_node = node

        back_dist = 0
        back_time = 0
        if (
            distances
            and durations
            and prev_node < len(distances)
            and 0 < len(distances[prev_node])
            and prev_node < len(durations)
            and 0 < len(durations[prev_node])
        ):
            back_dist = int(distances[prev_node][0] or 0)
            back_time = int(durations[prev_node][0] or 0)
        if v_type == "truck":
            back_time = int(back_time * truck_time_factor)

        total_distance += back_dist
        total_time += back_time

        recalced.append(
            {
                "vehicle_id": route.get("vehicle_id"),
                "vehicle_type": v_type,
                "total_distance_m": int(total_distance),
                "total_time_s": int(total_time),
                "stop_nodes": route_nodes,
                "stop_times": stop_times,
            }
        )

        total_distance_all += int(total_distance)
        total_duration_all += int(total_time)
        total_stop_count += len(route_nodes)

    return {
        "routes": recalced,
        "summary": {
            "route_count": len(recalced),
            "total_distance_m": int(total_distance_all),
            "total_time_s": int(total_duration_all),
            "total_stops": int(total_stop_count),
            "delivery_nodes": int(len(node_by_delivery_id)),
            "matrix_node_count": int(len(coords_list)),
        },
        "missing_delivery_coordinates": sorted(set(missing_delivery_coordinates)),
    }


def _parse_node_list(value: Any) -> List[int]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = [v.strip() for v in str(value).split(",")]
    out: List[int] = []
    for item in raw:
        try:
            n = int(item)
        except Exception:
            continue
        out.append(n)
    return out


def _node_label(node_idx: int, node_to_jobs: Dict[int, List[str]], deliveries_by_id: Dict[str, Any]) -> str:
    if node_idx == 0:
        return "Depot"
    tokens = node_to_jobs.get(node_idx, [])
    if not tokens:
        return f"Node {node_idx}"
    names: List[str] = []
    for token in tokens:
        base_id = str(token).split("_Part")[0].strip()
        d = deliveries_by_id.get(base_id)
        if d:
            names.append(str(getattr(d, "job_address", None) or getattr(d, "job_name", None) or base_id))
        else:
            names.append(base_id)
    # Keep label compact for API output.
    if len(names) == 1:
        return names[0]
    return f"{names[0]} (+{len(names)-1} more)"


def _matrix_route_breakdown(
    stop_nodes: List[int],
    distances: List[List[float]],
    durations: List[List[float]],
    node_to_jobs: Dict[int, List[str]],
    deliveries_by_id: Dict[str, Any],
) -> Dict[str, Any]:
    nodes = [n for n in stop_nodes if n != 0]
    seq = [0] + nodes + [0]
    legs: List[Dict[str, Any]] = []
    total_distance = 0
    total_duration = 0
    for idx in range(len(seq) - 1):
        frm = seq[idx]
        to = seq[idx + 1]
        dist = 0
        dur = 0
        try:
            dist = int(distances[frm][to]) if distances else 0
            dur = int(durations[frm][to]) if durations else 0
        except Exception:
            dist = 0
            dur = 0
        total_distance += dist
        total_duration += dur
        legs.append(
            {
                "from_node": frm,
                "to_node": to,
                "from_label": _node_label(frm, node_to_jobs, deliveries_by_id),
                "to_label": _node_label(to, node_to_jobs, deliveries_by_id),
                "distance_m": dist,
                "duration_s": dur,
            }
        )
    return {
        "stop_nodes": nodes,
        "legs": legs,
        "total_distance_m": int(total_distance),
        "total_duration_s": int(total_duration),
    }


def debug_tomorrow_matrix(
    route_nodes: Any = None,
    compare_nodes: Any = None,
    force_refresh: bool = False,
    target_date: str | None = None,
    trucks: int | None = None,
    vans: int = 0,
) -> Dict[str, Any]:
    db = DatabaseService()
    mapbox = MapboxService()

    deliveries = db.get_tomorrow_deliveries(force_refresh=force_refresh, target_date=target_date)
    target_date = getattr(db, "_last_target_date", None)
    if not deliveries:
        return {"ok": False, "message": "No deliveries found.", "target_date": target_date}

    geocode_failures = _geocode_missing(deliveries, db, mapbox)
    coords_list, node_to_jobs = _build_nodes_and_coords(deliveries)
    if len(coords_list) <= 1:
        return {"ok": False, "message": "No valid delivery locations to route.", "target_date": target_date}

    matrix = mapbox.get_matrix(coords_list, force_refresh=force_refresh)
    distances = matrix.get("distances", [])
    durations = matrix.get("durations", [])

    def _matrix_to_int_rows(raw: Any) -> List[List[int | None]]:
        out: List[List[int | None]] = []
        if not isinstance(raw, list):
            return out
        for row in raw:
            if not isinstance(row, list):
                out.append([])
                continue
            out_row: List[int | None] = []
            for cell in row:
                try:
                    out_row.append(int(cell) if cell is not None else None)
                except Exception:
                    out_row.append(None)
            out.append(out_row)
        return out

    deliveries_by_id = {str(d.id).strip(): d for d in deliveries}
    deliveries_debug: List[Dict[str, Any]] = []
    for d in deliveries:
        deliveries_debug.append(
            {
                "delivery_id": str(getattr(d, "id", "")),
                "job_name": getattr(d, "job_name", None),
                "job_address": getattr(d, "job_address", None),
                "lon": float(getattr(d, "lon", 0) or 0),
                "lat": float(getattr(d, "lat", 0) or 0),
            }
        )
    node_rows: List[Dict[str, Any]] = []
    for node_idx, (lon, lat) in enumerate(coords_list):
        tokens = node_to_jobs.get(node_idx, [])
        token_details = []
        for token in tokens:
            base_id = str(token).split("_Part")[0].strip()
            d = deliveries_by_id.get(base_id)
            token_details.append(
                {
                    "token": str(token),
                    "delivery_id": base_id,
                    "job_name": getattr(d, "job_name", None) if d else None,
                    "job_address": getattr(d, "job_address", None) if d else None,
                }
            )
        node_rows.append(
            {
                "node": node_idx,
                "lon": lon,
                "lat": lat,
                "label": _node_label(node_idx, node_to_jobs, deliveries_by_id),
                "tokens": token_details,
            }
        )

    asym: List[Dict[str, Any]] = []
    limit = min(len(coords_list), 60)
    for i in range(limit):
        for j in range(i + 1, limit):
            try:
                dij = int(distances[i][j])
                dji = int(distances[j][i])
            except Exception:
                continue
            diff = abs(dij - dji)
            if diff <= 0:
                continue
            asym.append({"from_node": i, "to_node": j, "distance_ij_m": dij, "distance_ji_m": dji, "delta_m": diff})
    asym.sort(key=lambda x: x["delta_m"], reverse=True)

    primary_nodes = _parse_node_list(route_nodes)
    compare = _parse_node_list(compare_nodes)
    direction_checks = matrix.get("distance_corrections", []) or []
    directions_api_matrix_m: List[List[Any]] = [["-" for _ in range(len(coords_list))] for _ in range(len(coords_list))]
    for entry in direction_checks:
        try:
            i = int(entry.get("from_node"))
            j = int(entry.get("to_node"))
        except Exception:
            continue
        if i < 0 or j < 0 or i >= len(coords_list) or j >= len(coords_list):
            continue
        if entry.get("directions_m") is not None:
            try:
                directions_api_matrix_m[i][j] = int(entry.get("directions_m"))
            except Exception:
                directions_api_matrix_m[i][j] = str(entry.get("directions_m"))
        elif entry.get("error"):
            directions_api_matrix_m[i][j] = "ERR"

    payload: Dict[str, Any] = {
        "ok": True,
        "target_date": target_date,
        "node_count": len(coords_list),
        "delivery_count": len(deliveries),
        "geocode_failures": geocode_failures,
        "mapbox_raw": matrix.get("mapbox_raw", matrix),
        "coordinates": [
            {"node": idx, "lon": float(lon), "lat": float(lat)}
            for idx, (lon, lat) in enumerate(coords_list)
        ],
        "deliveries": deliveries_debug,
        "haversine_matrix_m": _matrix_to_int_rows(matrix.get("haversine_distances_m", [])),
        "original_distance_matrix_m": _matrix_to_int_rows((matrix.get("mapbox_raw", {}) or {}).get("distances", [])),
        "corrected_distance_matrix_m": _matrix_to_int_rows(distances),
        "directions_api_matrix_m": directions_api_matrix_m,
        "directions_checked_pairs": len(direction_checks),
        "distance_corrections": matrix.get("distance_corrections", []),
        "distance_correction_meta": matrix.get("distance_correction_meta", {}),
        "raw_haversine_flags": matrix.get("raw_haversine_flags", []),
        "distance_matrix_m": _matrix_to_int_rows(distances),
        "duration_matrix_s": _matrix_to_int_rows(durations),
        "nodes": node_rows,
        "matrix_index": [
            {
                "node": row.get("node"),
                "label": row.get("label"),
                "tokens": row.get("tokens", []),
            }
            for row in node_rows
        ],
        "largest_asymmetry_sample": asym[:20],
    }

    def _matrix_max_leg(raw_matrix: Any, value_field: str) -> Dict[str, Any] | None:
        best: Dict[str, Any] | None = None
        if not isinstance(raw_matrix, list):
            return None
        for i, row in enumerate(raw_matrix):
            if not isinstance(row, list):
                continue
            for j, cell in enumerate(row):
                if i == j or cell is None:
                    continue
                try:
                    numeric = int(float(cell))
                except Exception:
                    continue
                if numeric < 0:
                    continue
                if best is None or numeric > int(best.get(value_field, -1)):
                    best = {
                        "from_node": i,
                        "to_node": j,
                        value_field: numeric,
                        "from_label": _node_label(i, node_to_jobs, deliveries_by_id),
                        "to_label": _node_label(j, node_to_jobs, deliveries_by_id),
                    }
        return best

    payload["matrix_extremes"] = {
        "max_distance": _matrix_max_leg(distances, "distance_m"),
        "max_duration": _matrix_max_leg(durations, "duration_s"),
    }

    requested_trucks = settings.PHYSICAL_TRUCKS if trucks is None else max(0, int(trucks))
    requested_vans = max(0, int(vans))
    run_payload = generate_tomorrow_plan(
        trucks=requested_trucks,
        vans=requested_vans,
        force_refresh=force_refresh,
        target_date=target_date,
    )
    run_routes = (
        run_payload.get("routes", [])
        if isinstance(run_payload, dict) and isinstance(run_payload.get("routes"), list)
        else []
    )
    visited_nodes: set[int] = set()
    run_route_rows: List[Dict[str, Any]] = []
    for route in run_routes:
        stop_nodes_raw = route.get("stop_nodes") if isinstance(route, dict) else []
        matrix_stop_nodes: List[int] = []
        for item in stop_nodes_raw or []:
            try:
                node_idx = int(item)
            except Exception:
                continue
            if node_idx <= 0 or node_idx >= len(coords_list):
                continue
            matrix_stop_nodes.append(node_idx)
            visited_nodes.add(node_idx)
        run_route_rows.append(
            {
                "vehicle_id": route.get("vehicle_id"),
                "vehicle_type": route.get("vehicle_type"),
                "total_distance_m": route.get("total_distance_m"),
                "total_time_s": route.get("total_time_s"),
                "matrix_stop_nodes": matrix_stop_nodes,
                "matrix_stop_labels": [
                    _node_label(node_idx, node_to_jobs, deliveries_by_id) for node_idx in matrix_stop_nodes
                ],
                "matrix_breakdown": _matrix_route_breakdown(
                    matrix_stop_nodes, distances, durations, node_to_jobs, deliveries_by_id
                ),
            }
        )

    required_nodes = set(range(1, len(coords_list)))
    dropped_nodes = sorted(required_nodes - visited_nodes)
    node_lookup = {int(row.get("node", -1)): row for row in node_rows}
    dropped_node_rows: List[Dict[str, Any]] = []
    for node_idx in dropped_nodes:
        row = node_lookup.get(node_idx) or {}
        dropped_node_rows.append(
            {
                "node": node_idx,
                "label": _node_label(node_idx, node_to_jobs, deliveries_by_id),
                "lon": row.get("lon"),
                "lat": row.get("lat"),
                "tokens": row.get("tokens", []),
            }
        )

    payload["run_debug"] = {
        "requested_fleet": {
            "trucks": int(requested_trucks),
            "vans": int(requested_vans),
            "vehicle_count": int(max(0, requested_trucks) + max(0, requested_vans)),
        },
        "route_count": len(run_route_rows),
        "routes": run_route_rows,
        "required_node_count": len(required_nodes),
        "visited_node_count": len(visited_nodes),
        "dropped_count": len(dropped_node_rows),
        "dropped_nodes": dropped_node_rows,
        "run_message": run_payload.get("message") if isinstance(run_payload, dict) else None,
        "run_meta": run_payload.get("meta") if isinstance(run_payload, dict) else {},
        "run_geocode_failures": run_payload.get("geocode_failures") if isinstance(run_payload, dict) else [],
    }

    if primary_nodes:
        payload["primary_route"] = _matrix_route_breakdown(
            primary_nodes, distances, durations, node_to_jobs, deliveries_by_id
        )
    if compare:
        payload["compare_route"] = _matrix_route_breakdown(
            compare, distances, durations, node_to_jobs, deliveries_by_id
        )
    return payload
