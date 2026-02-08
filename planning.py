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

            # Single delivery exceeds per-stop capacity => split into multiple parts at same coord
            if units > capacity_units:
                if curr_tokens:
                    node_to_jobs[len(coords_list)] = curr_tokens
                    coords_list.append(coord)
                    curr_tokens = []
                    curr_units = 0

                rem_units = units
                p = 1
                while rem_units > 0:
                    chunk = min(rem_units, capacity_units)
                    # Encode in token so UI can display split and solver can reason if it wants.
                    node_to_jobs[len(coords_list)] = [f"{j.id}_Part{p}_Units{chunk}"]
                    coords_list.append(coord)
                    rem_units -= chunk
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
        "pickup": bool(getattr(d, "pickup", False)),
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

                stop_items.append(item)
            stops.append(stop_items)

            meta = raw_stop_meta[len(stops) - 1] if len(raw_stop_meta) >= len(stops) else {}
            stop_times.append(
                {
                    "arrival_hhmm": meta.get("arrival_hhmm"),
                    "departure_hhmm": meta.get("departure_hhmm"),
                    "arrival_s": meta.get("arrival_s"),
                    "departure_s": meta.get("departure_s"),
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
    coords_list, _node_to_jobs = _build_nodes_and_coords(deliveries)

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
) -> Dict[str, Any]:
    db = DatabaseService()
    mapbox = MapboxService()

    deliveries = db.get_tomorrow_deliveries(force_refresh=force_refresh)
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
        "largest_asymmetry_sample": asym[:20],
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
