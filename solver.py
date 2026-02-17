import logging

import math
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from config import settings

logger = logging.getLogger(__name__)


class RouteOptimizer:
    """
    Normalize load into a shared "capacity_units" scale:

      - 1 med skid   = 20 units
      - 1 large skid = 25 units
      - 1 small skid = 10 units
      - 1 frame      = 1 unit
      - 1 exp frame  = 1 unit
      - truck capacity = 140 units
      - van capacity = 25 units

    Van truck-only constraints stay controlled by truck_req_cb rules.
    """

    # Defaults
    MED_SKID_UNITS = 20
    LARGE_SKID_UNITS = 25
    SMALL_SKID_UNITS = 10
    FRAME_UNITS = 1
    TRUCK_CAPACITY_UNITS = 140
    VAN_CAPACITY_UNITS = 25
    VAN_FRAME_LIMIT = 3
    VAN_DOOR_LIMIT = 3
    VAN_EXP_FRAME_LIMIT = 10
    RELOAD_THRESHOLD_UNITS = 140

    DEFAULT_PENALTIES = {
        "truck_cost_factor": 1.12,
        "truck_time_factor": 1.08,
        "truck_serve_van_eligible_penalty": 2500,
        "time_span_cost_coeff": 5,
        "downtown_soft_latest_s": 4 * 3600,   # 10:00 (from 6:00 start)
        "downtown_hard_latest_s": 6 * 3600,   # 12:00 hard cap
        "downtown_late_penalty_per_sec": 60,
        "van_fixed_cost": 0,
        "truck_fixed_cost_physical": 15000,
        "truck_fixed_cost_overflow": 100000,
        "drop_node_penalty": 100000,
        "truck_service_time_s": 30 * 60,
        "van_service_time_s": 10 * 60,
        "shift_seconds": 8 * 3600,
        "waiting_slack_seconds": 600,
        "search_time_limit_s": 60,
        "search_print_results": True,
    }

    def __init__(
        self,
        duration_matrix,
        distance_matrix,
        node_to_jobs,
        deliveries,
        vehicle_types,
        physical_truck_count=2,
        node_is_downtown=None,
        node_time_windows=None,
        penalty_profile=None,
    ):
        self.duration_matrix = self._prepare(duration_matrix)
        self.distance_matrix = self._prepare(distance_matrix)
        self.node_to_jobs = node_to_jobs
        self.deliveries = deliveries
        self.vehicle_types = vehicle_types
        self.physical_truck_count = physical_truck_count
        self.depot = 0

        # Support deliveries as dicts or objects
        self.delivery_by_id = {
            self._delivery_id(d): d
            for d in deliveries
            if self._delivery_id(d)
        }

        self.node_is_downtown = node_is_downtown or {}
        self.node_time_windows = node_time_windows or {}
        self.reload_nodes = []
        self.base_node_count = len(self.distance_matrix)

        # Allow overriding penalties via penalty_profile
        self.penalties = dict(self.DEFAULT_PENALTIES)
        if isinstance(penalty_profile, dict):
            for key, val in penalty_profile.items():
                if key in self.penalties and val is not None:
                    self.penalties[key] = val
        self.costs = self._build_costs()

        self.med_skid_units = max(
            1,
            int(getattr(settings, "MED_SKID_UNITS", getattr(settings, "LONG_SKID_UNITS", self.MED_SKID_UNITS))),
        )
        self.large_skid_units = max(1, int(getattr(settings, "LARGE_SKID_UNITS", self.LARGE_SKID_UNITS)))
        self.small_skid_units = max(1, int(getattr(settings, "SMALL_SKID_UNITS", self.SMALL_SKID_UNITS)))
        self.frame_units = max(1, int(getattr(settings, "FRAME_UNITS", self.FRAME_UNITS)))
        self.truck_capacity_units = max(
            1,
            int(getattr(settings, "VEHICLE_CAPACITY_UNITS", self.TRUCK_CAPACITY_UNITS)),
        )
        reload_threshold = int(getattr(settings, "SOLVER_RELOAD_THRESHOLD_UNITS", self.RELOAD_THRESHOLD_UNITS))
        if reload_threshold <= 0:
            reload_threshold = int(self.truck_capacity_units)
        self.reload_threshold_units = max(1, int(reload_threshold))
        self.van_capacity_units = max(
            0,
            int(getattr(settings, "VAN_CAPACITY_UNITS", self.VAN_CAPACITY_UNITS)),
        )

        self._auto_add_reload_nodes()

    def _build_costs(self) -> dict:
        return {
            "truck_cost_factor": float(self.penalties["truck_cost_factor"]),
            "truck_time_factor": float(self.penalties["truck_time_factor"]),
            "truck_serve_van_eligible_penalty": int(self.penalties["truck_serve_van_eligible_penalty"]),
            "time_span_cost_coeff": int(self.penalties["time_span_cost_coeff"]),
            "downtown_soft_latest_s": int(self.penalties["downtown_soft_latest_s"]),
            "downtown_hard_latest_s": int(self.penalties["downtown_hard_latest_s"]),
            "downtown_late_penalty_per_sec": int(self.penalties["downtown_late_penalty_per_sec"]),
            "van_fixed_cost": int(self.penalties["van_fixed_cost"]),
            "truck_fixed_cost_physical": int(self.penalties["truck_fixed_cost_physical"]),
            "truck_fixed_cost_overflow": int(self.penalties["truck_fixed_cost_overflow"]),
            "drop_node_penalty": int(self.penalties["drop_node_penalty"]),
            "truck_service_time_s": int(self.penalties.get("truck_service_time_s", 30 * 60)),
            "van_service_time_s": int(self.penalties.get("van_service_time_s", 10 * 60)),
            "shift_seconds": int(self.penalties["shift_seconds"]),
            "waiting_slack_seconds": int(self.penalties["waiting_slack_seconds"]),
            "search_time_limit_s": int(self.penalties["search_time_limit_s"]),
        }

    # ---------------------------
    # Utility / data access
    # ---------------------------
    def _prepare(self, matrix):
        return [
            [int(round(val)) if (val is not None and val < 10**7) else 999999 for val in row]
            for row in matrix
        ]

    def _node_requires_truck(self, node: int) -> bool:
        if node in self.reload_nodes:
            return True
        van_only, truck_only = self._node_pickup_vehicle_flags(node)
        if truck_only:
            return True
        if van_only and not truck_only:
            return False
        door_total, frame_total, exp_frame_total, skid_present = self._node_cargo_summary(node)
        if self._node_requires_empty_pickup(node):
            return True
        if skid_present and (door_total > 0 or frame_total > 0 or exp_frame_total > 0):
            return True
        if (
            door_total > self.VAN_DOOR_LIMIT
            or frame_total > self.VAN_FRAME_LIMIT
            or exp_frame_total > self.VAN_EXP_FRAME_LIMIT
        ):
            return True
        units = self._node_capacity_units(node)
        van_cap = int(self.van_capacity_units or 0)
        if van_cap <= 0:
            if units > 0:
                return True
        else:
            if units > van_cap:
                return True
        return False

    def _estimate_reload_count(self, truck_count: int | None = None) -> int:
        capacity = int(self.truck_capacity_units or 0)
        if capacity <= 0:
            return 0
        if truck_count is None:
            truck_count = sum(1 for v in self.vehicle_types if v == "truck")
        truck_count = int(truck_count or 0)
        if truck_count <= 0:
            return 0

        total_units = self._total_delivery_units()

        max_truck_capacity = int(truck_count * capacity)
        if total_units <= max_truck_capacity:
            return 0
        required_loads = int(math.ceil(total_units / capacity))
        return max(0, required_loads - truck_count)

    def _total_truck_only_units(self) -> int:
        total_units = 0
        for node in range(1, self.base_node_count):
            if self._node_requires_truck(node):
                tokens = self.node_to_jobs.get(node, [])
                for t in tokens:
                    deliv_id, _, units = self._parse_token(t)
                    d = self.delivery_by_id.get(deliv_id) if deliv_id else None
                    pickup_flag = bool(self._delivery_field(d, "pickup", False)) if d else False
                    if pickup_flag:
                        continue
                    if units:
                        unit_val = int(units)
                    elif d:
                        unit_val = self._delivery_units(d)
                    else:
                        unit_val = 0
                    total_units += max(0, int(unit_val))
        return int(total_units)

    def _total_delivery_units(self) -> int:
        total_units = 0
        for node in range(1, self.base_node_count):
            tokens = self.node_to_jobs.get(node, [])
            for t in tokens:
                deliv_id, _, units = self._parse_token(t)
                d = self.delivery_by_id.get(deliv_id) if deliv_id else None
                pickup_flag = bool(self._delivery_field(d, "pickup", False)) if d else False
                if pickup_flag:
                    continue
                if units:
                    unit_val = int(units)
                elif d:
                    unit_val = self._delivery_units(d)
                else:
                    unit_val = 0
                total_units += max(0, int(unit_val))
        return int(total_units)

    def _append_depot_clone(self, matrix: list[list[int]]):
        depot_row = list(matrix[self.depot])
        for i in range(len(matrix)):
            matrix[i].append(matrix[i][self.depot])
        matrix.append(depot_row + [depot_row[self.depot]])

    def _auto_add_reload_nodes(self):
        reload_count = self._estimate_reload_count()
        if reload_count <= 0:
            return

        for _ in range(reload_count):
            new_idx = len(self.distance_matrix)
            self._append_depot_clone(self.distance_matrix)
            self._append_depot_clone(self.duration_matrix)
            self.node_to_jobs[new_idx] = ["Depot - Reload"]
            self.node_is_downtown[new_idx] = False
            self.reload_nodes.append(new_idx)

    def _ensure_reload_nodes_for_truck_count(self, truck_count: int):
        required = self._estimate_reload_count(truck_count=truck_count)
        missing = int(required - len(self.reload_nodes))
        if missing <= 0:
            return
        for _ in range(missing):
            new_idx = len(self.distance_matrix)
            self._append_depot_clone(self.distance_matrix)
            self._append_depot_clone(self.duration_matrix)
            self.node_to_jobs[new_idx] = ["Depot - Reload"]
            self.node_is_downtown[new_idx] = False
            self.reload_nodes.append(new_idx)

    def _build_vehicle_subset(self, max_trucks: int) -> list[str]:
        """
        Keep vehicle order but cap how many trucks are included.
        Non-truck vehicles are always retained.
        """
        subset = []
        trucks_seen = 0
        for v_type in self.vehicle_types:
            if v_type == "truck":
                if trucks_seen < max_trucks:
                    subset.append(v_type)
                    trucks_seen += 1
            else:
                subset.append(v_type)
        return subset

    def _delivery_id(self, d) -> str:
        if isinstance(d, dict):
            return str(d.get("id", "")).strip()
        return str(getattr(d, "id", "")).strip()

    def _delivery_field(self, d, field_name: str, default=0):
        if isinstance(d, dict):
            return d.get(field_name, default)
        return getattr(d, field_name, default)

    def _normalize_pickup_vehicle(self, value) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "both"
        if raw in ("van", "vans", "van-only", "van only", "van_only"):
            return "van"
        if raw in ("truck", "trucks", "truck-only", "truck only", "truck_only"):
            return "truck"
        if raw in ("both", "any", "either", "all", "van/truck", "truck/van"):
            return "both"
        return "both"

    def _delivery_pickup_vehicle(self, d) -> str:
        return self._normalize_pickup_vehicle(self._delivery_field(d, "pickup_vehicle", ""))

    def _delivery_units(self, d) -> int:
        """Fallback when token doesn't encode units explicitly."""
        med_qty = int(self._delivery_field(d, "med_skid", self._delivery_field(d, "skid", 0)) or 0)
        large_qty = int(self._delivery_field(d, "large_skid", 0) or 0)
        small_qty = int(self._delivery_field(d, "small_skid", 0) or 0)
        frame_qty = int(self._delivery_field(d, "frame", 0) or 0)
        exp_frame_qty = int(self._delivery_field(d, "exp_frame", 0) or 0)
        units = (
            (med_qty * self.med_skid_units)
            + (large_qty * self.large_skid_units)
            + (small_qty * self.small_skid_units)
            + ((frame_qty + exp_frame_qty) * self.frame_units)
        )
        return max(0, int(units))

    def _parse_token(self, token: str):
        """
        Parse a job token and return (base_id, part_no, units).
        Token formats supported:
          - "{id}_Part{p}_Units{u}"
          - "{id}_Part{p}_Qty{q}" (legacy: q med skids -> q*MED_SKID_UNITS)
          - "{id}_Part{p}_Big{b}_Small{s}" (units=b*MED_SKID_UNITS+s*SMALL_SKID_UNITS)
          - "{id}_Part{p}_Med{m}_Large{l}_Small{s}_Frame{f}_Exp{e}_Door{d}_Box{b}"
          - "{id}" (units fallback from delivery fields)
        """
        token = str(token).strip()
        if token.lower() == "depot":
            return (None, None, 0)

        base = token.split("_Part")[0].split("_Big")[0].split("_Small")[0].strip()

        part_no = None
        if "_Part" in token:
            try:
                part_str = token.split("_Part", 1)[1]
                part_no = int(part_str.split("_", 1)[0])
            except (ValueError, IndexError):
                part_no = None

        if "_Units" in token:
            try:
                units = int(token.split("_Units", 1)[1].split("_", 1)[0])
                return (base, part_no, max(0, units))
            except (ValueError, IndexError):
                pass

        token_counts = self._token_item_counts(token)
        if token_counts:
            units = (
                (token_counts["med"] * self.med_skid_units)
                + (token_counts["large"] * self.large_skid_units)
                + (token_counts["small"] * self.small_skid_units)
                + ((token_counts["frame"] + token_counts["exp"]) * self.frame_units)
            )
            return (base, part_no, max(0, int(units)))

        if "_Qty" in token:
            try:
                qty = int(token.split("_Qty", 1)[1].split("_", 1)[0])
                units = qty * self.med_skid_units
                return (base, part_no, max(0, units))
            except (ValueError, IndexError):
                pass

        big_qty = 0
        small_qty = 0
        if "_Big" in token:
            try:
                big_qty = int(token.split("_Big", 1)[1].split("_", 1)[0])
            except Exception:
                big_qty = 0
        if "_Small" in token:
            try:
                small_qty = int(token.split("_Small", 1)[1].split("_", 1)[0])
            except Exception:
                small_qty = 0

        units = (big_qty * self.med_skid_units) + (small_qty * self.small_skid_units)
        return (base, part_no, max(0, int(units)))

    def _token_item_counts(self, token: str) -> dict | None:
        def _parse_field(field: str) -> int | None:
            key = f"_{field}"
            if key not in token:
                return None
            try:
                val = token.split(key, 1)[1].split("_", 1)[0]
                return max(0, int(val))
            except (ValueError, IndexError):
                return None

        fields = ["Med", "Large", "Small", "Frame", "Exp", "Door", "Box"]
        parsed = {}
        found = False
        for field in fields:
            val = _parse_field(field)
            if val is not None:
                found = True
                parsed[field.lower()] = val
            else:
                parsed[field.lower()] = 0

        return parsed if found else None

    def _node_pickup_vehicle_flags(self, node_idx: int) -> tuple[bool, bool]:
        van_only = False
        truck_only = False
        tokens = self.node_to_jobs.get(node_idx, [])
        for tok in tokens:
            base_id, _, _ = self._parse_token(tok)
            if not base_id:
                continue
            d = self.delivery_by_id.get(base_id)
            if not d:
                continue
            if not bool(self._delivery_field(d, "pickup", False)):
                continue
            if bool(self._delivery_field(d, "requires_empty_truck", False)):
                truck_only = True
            pickup_vehicle = self._delivery_pickup_vehicle(d)
            if pickup_vehicle == "van":
                van_only = True
            elif pickup_vehicle == "truck":
                truck_only = True
            if van_only and truck_only:
                break
        return van_only, truck_only

    def _node_cargo_summary(self, node_idx: int) -> tuple[int, int, int, bool]:
        """
        Sum door/frame/exp_frame counts (plus skid presence) for all deliveries at this node.
        """
        door_total = 0
        frame_total = 0
        exp_frame_total = 0
        skid_present = False
        small_skid_total = 0

        tokens = self.node_to_jobs.get(node_idx, [])
        for tok in tokens:
            token_counts = self._token_item_counts(str(tok))
            if token_counts:
                med_qty = int(token_counts.get("med", 0) or 0)
                large_qty = int(token_counts.get("large", 0) or 0)
                small_qty = int(token_counts.get("small", 0) or 0)
                frame_qty = int(token_counts.get("frame", 0) or 0)
                exp_qty = int(token_counts.get("exp", 0) or 0)
                door_qty = int(token_counts.get("door", 0) or 0)

                if med_qty > 0 or large_qty > 0 or small_qty > 0:
                    skid_present = True
                small_skid_total += small_qty
                door_total += door_qty
                frame_total += frame_qty
                exp_frame_total += exp_qty
                continue

            base_id, _, _ = self._parse_token(tok)
            if not base_id:
                continue

            d = self.delivery_by_id.get(base_id)
            if not d:
                continue

            if (self._delivery_field(d, "med_skid", self._delivery_field(d, "skid", 0)) or 0) > 0:
                skid_present = True

            small_skid_total += int(self._delivery_field(d, "small_skid", 0) or 0)
            if (self._delivery_field(d, "large_skid", 0) or 0) > 0:
                skid_present = True
            door_total += int(self._delivery_field(d, "door", 0) or 0)
            frame_total += int(self._delivery_field(d, "frame", 0) or 0)
            exp_frame_total += int(self._delivery_field(d, "exp_frame", 0) or 0)

        if small_skid_total > 0:
            skid_present = True

        return door_total, frame_total, exp_frame_total, skid_present

    def _node_capacity_units(self, node_idx: int) -> int:
        if node_idx == self.depot:
            return 0
        total_units = 0
        tokens = self.node_to_jobs.get(node_idx, [])
        for tok in tokens:
            deliv_id, _, units = self._parse_token(tok)
            if units:
                unit_val = int(units)
            else:
                d = self.delivery_by_id.get(deliv_id) if deliv_id else None
                unit_val = self._delivery_units(d) if d else 0
            total_units += max(0, int(unit_val))
        return int(total_units)

    def _node_requires_empty_pickup(self, node_idx: int) -> bool:
        tokens = self.node_to_jobs.get(node_idx, [])
        for tok in tokens:
            base_id, _, _ = self._parse_token(tok)
            if not base_id:
                continue
            d = self.delivery_by_id.get(base_id)
            if not d:
                continue
            pickup = bool(self._delivery_field(d, "pickup", False))
            requires_empty = bool(self._delivery_field(d, "requires_empty_truck", False))
            pickup_vehicle = self._delivery_pickup_vehicle(d)
            if pickup and requires_empty and pickup_vehicle != "van":
                return True
        return False

    # ---------------------------
    # Logging + single-model solve
    # ---------------------------
    def _print_search(self, message: str):
        if bool(self.penalties.get("search_print_results", True)):
            logger.info("%s", message)

    def _summarize_output(self, output: list[dict]) -> dict:
        if not output:
            return {
                "routes_used": 0,
                "total_distance_m": 0,
                "total_time_s": 0,
                "truck_routes": 0,
                "van_routes": 0,
                "total_capacity_units": 0,
            }
        return {
            "routes_used": len(output),
            "total_distance_m": sum(int(r.get("total_distance_m", 0)) for r in output),
            "total_time_s": sum(int(r.get("total_time_s", 0)) for r in output),
            "truck_routes": sum(1 for r in output if r.get("v_type") == "truck"),
            "van_routes": sum(1 for r in output if r.get("v_type") == "van"),
            "total_capacity_units": sum(int(r.get("capacity_units", r.get("skids", 0)) or 0) for r in output),
        }

    def _solve_model(self, service_time=1200, max_route_time=43200, vehicle_types=None, physical_truck_count=None):
        vehicle_types = vehicle_types or self.vehicle_types
        physical_truck_count = (
            self.physical_truck_count
            if physical_truck_count is None
            else int(physical_truck_count)
        )
        manager = pywrapcp.RoutingIndexManager(
            len(self.distance_matrix),
            len(vehicle_types),
            self.depot
        )
        routing = pywrapcp.RoutingModel(manager)

        node_has_skid = [False] * len(self.distance_matrix)
        node_has_doorframe = [False] * len(self.distance_matrix)
        node_pickup_van_only = [False] * len(self.distance_matrix)
        node_pickup_truck_only = [False] * len(self.distance_matrix)
        for node in range(1, len(self.distance_matrix)):
            if node in self.reload_nodes:
                continue
            door_total, frame_total, exp_frame_total, skid_present = self._node_cargo_summary(node)
            node_has_skid[node] = bool(skid_present)
            node_has_doorframe[node] = (door_total + frame_total + exp_frame_total) > 0
            van_only, truck_only = self._node_pickup_vehicle_flags(node)
            node_pickup_van_only[node] = bool(van_only and not truck_only)
            node_pickup_truck_only[node] = bool(truck_only)

        # ---------------------------
        # Callbacks for constraints
        # ---------------------------
        def truck_req_cb(from_idx):
            node_idx = manager.IndexToNode(from_idx)
            if node_idx == self.depot:
                return 0
            return 1 if self._node_requires_truck(node_idx) else 0

        def node_unit_demand(from_idx: int) -> int:
            node_idx = manager.IndexToNode(from_idx)
            if node_idx == self.depot:
                return 0
            if node_idx in self.reload_nodes:
                return 0

            tokens = self.node_to_jobs.get(node_idx, [])
            total_units = 0

            for t in tokens:
                deliv_id, _, units = self._parse_token(t)
                pickup_flag = False
                d = None
                if deliv_id:
                    d = self.delivery_by_id.get(deliv_id)
                    if d:
                        pickup_flag = bool(self._delivery_field(d, "pickup", False))

                if units:
                    unit_val = int(units)
                elif d:
                    unit_val = self._delivery_units(d)
                else:
                    unit_val = 0

                if unit_val:
                    total_units += unit_val if pickup_flag else -unit_val

            return int(total_units)

        def make_vehicle_unit_cb():
            def vehicle_unit_cb(from_idx, _to_idx):
                node_idx = manager.IndexToNode(from_idx)
                if node_idx == self.depot:
                    return 0
                return node_unit_demand(from_idx)

            return vehicle_unit_cb

        def skid_presence_cb(from_idx, _to_idx):
            node_idx = manager.IndexToNode(from_idx)
            return 1 if node_has_skid[node_idx] else 0

        def doorframe_presence_cb(from_idx, _to_idx):
            node_idx = manager.IndexToNode(from_idx)
            return 1 if node_has_doorframe[node_idx] else 0

        van_eligible = [False] * len(self.distance_matrix)
        for node in range(1, len(self.distance_matrix)):
            van_eligible[node] = not self._node_requires_truck(node)

        truck_idx = routing.RegisterUnaryTransitCallback(truck_req_cb)
        unit_cbs = []
        for _ in vehicle_types:
            cb_idx = routing.RegisterTransitCallback(make_vehicle_unit_cb())
            unit_cbs.append(cb_idx)

        van_vehicle_ids = [int(idx) for idx, v_type in enumerate(vehicle_types) if v_type == "van"]
        truck_vehicle_ids = [int(idx) for idx, v_type in enumerate(vehicle_types) if v_type == "truck"]
        for node in range(1, len(self.distance_matrix)):
            if node in self.reload_nodes:
                if truck_vehicle_ids:
                    idx = int(manager.NodeToIndex(node))
                    try:
                        routing.SetAllowedVehiclesForIndex(list(map(int, truck_vehicle_ids)), idx)
                    except TypeError:
                        routing.VehicleVar(idx).SetValues(list(map(int, truck_vehicle_ids)))
                continue
            allowed = None
            if node_pickup_van_only[node]:
                allowed = van_vehicle_ids
            elif node_pickup_truck_only[node]:
                allowed = truck_vehicle_ids
            if allowed is None:
                continue
            idx = int(manager.NodeToIndex(node))
            if allowed:
                try:
                    routing.SetAllowedVehiclesForIndex(list(map(int, allowed)), idx)
                except TypeError:
                    routing.VehicleVar(idx).SetValues(list(map(int, allowed)))

        skid_presence_idx = routing.RegisterTransitCallback(skid_presence_cb)
        doorframe_presence_idx = routing.RegisterTransitCallback(doorframe_presence_cb)
        presence_cap = max(1, len(self.distance_matrix))
        routing.AddDimension(skid_presence_idx, 0, presence_cap, True, "SkidPresence")
        routing.AddDimension(doorframe_presence_idx, 0, presence_cap, True, "DoorFramePresence")

        # ---------------------------
        # Vehicle-dependent economics
        # ---------------------------
        TRUCK_COST_FACTOR = float(self.costs["truck_cost_factor"])
        TRUCK_TIME_FACTOR = float(self.costs["truck_time_factor"])
        TRUCK_SERVICE_TIME = int(self.costs["truck_service_time_s"])
        VAN_SERVICE_TIME = int(self.costs["van_service_time_s"])

        def make_vehicle_cost_cb(truck_flag: bool):
            def cost_cb(i, j):
                from_node = manager.IndexToNode(i)
                to_node = manager.IndexToNode(j)
                base = self.distance_matrix[from_node][to_node]

                if not truck_flag:
                    return base

                scaled = int(base * TRUCK_COST_FACTOR)
                return scaled
            return cost_cb

        for v_id, v_type in enumerate(vehicle_types):
            is_truck = (v_type == "truck")
            cost_idx = routing.RegisterTransitCallback(make_vehicle_cost_cb(is_truck))
            routing.SetArcCostEvaluatorOfVehicle(cost_idx, v_id)

        def make_vehicle_time_cb(truck_flag: bool):
            def vtime_cb(i, j):
                from_node = manager.IndexToNode(i)
                to_node = manager.IndexToNode(j)

                base_leg = self.duration_matrix[from_node][to_node]
                base_leg = int(base_leg * (TRUCK_TIME_FACTOR if truck_flag else 1.0))

                if from_node == self.depot:
                    svc = 0
                elif truck_flag and van_eligible[from_node]:
                    svc = VAN_SERVICE_TIME
                else:
                    svc = TRUCK_SERVICE_TIME if truck_flag else VAN_SERVICE_TIME

                return base_leg + svc
            return vtime_cb

        time_cbs = []
        for v_id, v_type in enumerate(vehicle_types):
            is_truck = (v_type == "truck")
            cb_idx = routing.RegisterTransitCallback(make_vehicle_time_cb(is_truck))
            time_cbs.append(cb_idx)

        # ---------------------------
        # Time dimension
        # ---------------------------
        SHIFT_SECONDS = int(self.costs["shift_seconds"])
        WAITING_SLACK = int(self.costs["waiting_slack_seconds"])

        routing.AddDimensionWithVehicleTransitAndCapacity(
            time_cbs,
            WAITING_SLACK,
            [SHIFT_SECONDS] * len(vehicle_types),
            True,
            "Time"
        )
        time_dim = routing.GetDimensionOrDie("Time")

        for v_id in range(len(vehicle_types)):
            time_dim.CumulVar(routing.End(v_id)).SetMax(SHIFT_SECONDS)

        time_dim.SetGlobalSpanCostCoefficient(int(self.costs["time_span_cost_coeff"]))

        # ---------------------------
        # Truck-only + Skids capacity
        # ---------------------------
        truck_caps = [999 if v == "truck" else 0 for v in vehicle_types]
        routing.AddDimensionWithVehicleCapacity(truck_idx, 0, truck_caps, True, "TruckOnly")

        unit_caps = [
            self.truck_capacity_units if v == "truck" else int(self.van_capacity_units or 0)
            for v in vehicle_types
        ]
        max_node_units = 0
        for node in range(1, len(self.distance_matrix)):
            if node in self.reload_nodes:
                continue
            max_node_units = max(max_node_units, abs(int(self._node_capacity_units(node) or 0)))
        slack_cap = max(
            int(self.truck_capacity_units or 0),
            int(self.van_capacity_units or 0),
            int(max_node_units),
        )
        routing.AddDimensionWithVehicleTransitAndCapacity(unit_cbs, slack_cap, unit_caps, True, "Skids")
        skid_dim = routing.GetDimensionOrDie("Skids")
        solver = routing.solver()
        skid_presence_dim = routing.GetDimensionOrDie("SkidPresence")
        doorframe_presence_dim = routing.GetDimensionOrDie("DoorFramePresence")
        for v_id, v_type in enumerate(vehicle_types):
            if v_type != "van":
                continue
            end_idx = routing.End(v_id)
            skid_zero = solver.IsEqualCstVar(skid_presence_dim.CumulVar(end_idx), 0)
            door_zero = solver.IsEqualCstVar(doorframe_presence_dim.CumulVar(end_idx), 0)
            solver.Add(skid_zero + door_zero >= 1)

        # Partial reloads via slack: allow loading only at depot starts and reload nodes.
        for idx in range(routing.Size()):
            skid_dim.SlackVar(idx).SetRange(0, int(slack_cap))
        for v_id, v_type in enumerate(vehicle_types):
            start_idx = routing.Start(v_id)
            cap = self.truck_capacity_units if v_type == "truck" else int(self.van_capacity_units or 0)
            skid_dim.SlackVar(start_idx).SetRange(0, int(cap))
        if self.reload_nodes:
            for node in self.reload_nodes:
                idx = int(manager.NodeToIndex(node))
                skid_dim.SlackVar(idx).SetRange(0, int(self.truck_capacity_units))
        for node in range(1, len(self.distance_matrix)):
            if node in self.reload_nodes:
                continue
            idx = int(manager.NodeToIndex(node))
            slack = skid_dim.SlackVar(idx)
            active = routing.ActiveVar(idx)
            solver.Add(slack <= int(slack_cap) * (1 - active))

        pickup_empty_nodes = [
            node
            for node in range(1, len(self.distance_matrix))
            if self._node_requires_empty_pickup(node)
        ]
        if pickup_empty_nodes:
            ends = [routing.End(v_id) for v_id in range(len(vehicle_types))]
            for node in pickup_empty_nodes:
                idx = int(manager.NodeToIndex(node))
                if truck_vehicle_ids:
                    # Some OR-Tools builds have strict type expectations for SetAllowedVehiclesForIndex.
                    # Fall back to restricting the vehicle variable domain if the wrapper rejects lists.
                    try:
                        routing.SetAllowedVehiclesForIndex(list(map(int, truck_vehicle_ids)), idx)
                    except TypeError:
                        routing.VehicleVar(idx).SetValues(list(map(int, truck_vehicle_ids)))
                # Must return to depot immediately after.
                end_for_vehicle = solver.Element(ends, routing.VehicleVar(idx))
                solver.Add(routing.NextVar(idx) == end_for_vehicle)

        if self.reload_nodes:
            # Reload nodes are only meaningful when followed by additional work.
            # Prevent routes from ending immediately after a depot-reload stop.
            ends = [routing.End(v_id) for v_id in range(len(vehicle_types))]
            for node in self.reload_nodes:
                idx = int(manager.NodeToIndex(node))
                end_for_vehicle = solver.Element(ends, routing.VehicleVar(idx))
                solver.Add(routing.NextVar(idx) != end_for_vehicle)

        # ---------------------------
        # Downtown-first (hard + soft)
        # ---------------------------
        DOWNTOWN_SOFT_LATEST = int(self.costs["downtown_soft_latest_s"])
        DOWNTOWN_HARD_LATEST = int(self.costs["downtown_hard_latest_s"])
        LATE_PENALTY_PER_SEC = int(self.costs["downtown_late_penalty_per_sec"])
        if DOWNTOWN_HARD_LATEST < DOWNTOWN_SOFT_LATEST:
            DOWNTOWN_HARD_LATEST = DOWNTOWN_SOFT_LATEST

        for node in range(1, len(self.distance_matrix)):
            if not self.node_is_downtown.get(node, False):
                continue
            idx = manager.NodeToIndex(node)
            time_dim.CumulVar(idx).SetMax(DOWNTOWN_HARD_LATEST)
            time_dim.SetCumulVarSoftUpperBound(idx, DOWNTOWN_SOFT_LATEST, LATE_PENALTY_PER_SEC)

        # ---------------------------
        # Hard time windows
        # ---------------------------
        for node, (tw_start, tw_end) in self.node_time_windows.items():
            if node == self.depot:
                continue
            if tw_start is None or tw_end is None:
                continue
            idx = manager.NodeToIndex(node)
            time_dim.CumulVar(idx).SetRange(int(tw_start), int(tw_end))

        if self.reload_nodes:
            for node in self.reload_nodes:
                idx = manager.NodeToIndex(node)
                time_dim.CumulVar(idx).SetRange(0, SHIFT_SECONDS)

        # ---------------------------
        # Fixed costs
        # ---------------------------
        VAN_FIXED_COST = int(self.costs["van_fixed_cost"])
        TRUCK_FIXED_COST_PHYSICAL = int(self.costs["truck_fixed_cost_physical"])
        TRUCK_FIXED_COST_OVERFLOW = int(self.costs["truck_fixed_cost_overflow"])

        truck_seen = 0
        for v_id, v_type in enumerate(vehicle_types):
            if v_type == "van":
                routing.SetFixedCostOfVehicle(VAN_FIXED_COST, v_id)
            else:
                truck_seen += 1
                cost = TRUCK_FIXED_COST_PHYSICAL if truck_seen <= physical_truck_count else TRUCK_FIXED_COST_OVERFLOW
                routing.SetFixedCostOfVehicle(cost, v_id)

        # ---------------------------
        # Allow dropping any non-depot node
        # ---------------------------
        for node in range(1, len(self.distance_matrix)):
            idx = manager.NodeToIndex(node)
            if node in self.reload_nodes:
                routing.AddDisjunction([idx], 0)
            else:
                routing.AddDisjunction([idx], int(self.costs["drop_node_penalty"]))

        # ---------------------------
        # Search parameters
        # ---------------------------
        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_params.time_limit.seconds = max(1, int(self.costs["search_time_limit_s"]))

        solution = routing.SolveWithParameters(search_params)
        if not solution:
            return None

        output = self._format_output(manager, routing, solution, vehicle_types=vehicle_types)
        if output is None:
            return None

        return output

    # ---------------------------
    # Public solve (single model)
    # ---------------------------
    def solve(self, service_time=1200, max_route_time=43200):
        self._print_search("[solver] single-model solve started")
        total_trucks = sum(1 for v in self.vehicle_types if v == "truck")
        total_units = self._total_delivery_units()
        truck_only_units = self._total_truck_only_units()
        prefer_reload = total_units > int(self.reload_threshold_units or 0)
        preferred_physical_truck_count = min(self.physical_truck_count, 1) if prefer_reload else self.physical_truck_count
        max_truck_capacity = int(max(0, total_trucks) * int(self.truck_capacity_units or 0))
        self._print_search(
            "[solver] reload check "
            f"total_delivery_units={total_units} "
            f"truck_only_units={truck_only_units} "
            f"max_truck_capacity={max_truck_capacity} "
            f"reload_threshold={int(self.reload_threshold_units or 0)} "
            f"prefer_reload={prefer_reload} "
            f"physical_trucks={self.physical_truck_count} "
            f"physical_trucks_used={preferred_physical_truck_count}"
        )
        if total_trucks > 1:
            # Ensure the 1-truck attempt has enough synthetic reload nodes available.
            self._ensure_reload_nodes_for_truck_count(1)
            reduced_types = self._build_vehicle_subset(1)
            self._print_search("[solver] trying reduced fleet (1 truck + all vans)")
            output = self._solve_model(
                service_time=service_time,
                max_route_time=max_route_time,
                vehicle_types=reduced_types,
                physical_truck_count=preferred_physical_truck_count,
            )
            if output:
                dropped_count = int(output[0].get("dropped_count", 0) or 0)
                if dropped_count > 0:
                    self._print_search(
                        "[solver] reduced fleet dropped nodes; retrying with full fleet "
                        f"dropped_count={dropped_count}"
                    )
                    output = None
                else:
                    summary = self._summarize_output(output)
                    self._print_search(
                        "[solver] reduced fleet solution "
                        f"routes_used={summary['routes_used']} "
                        f"truck_routes={summary['truck_routes']} van_routes={summary['van_routes']} "
                        f"total_distance_m={summary['total_distance_m']} total_time_s={summary['total_time_s']} "
                        f"total_capacity_units={summary['total_capacity_units']}"
                    )
                    return output

        output = self._solve_model(
            service_time=service_time,
            max_route_time=max_route_time,
            physical_truck_count=preferred_physical_truck_count,
        )
        if not output:
            self._print_search("[solver] no solution")
            return None
        summary = self._summarize_output(output)
        self._print_search(
            "[solver] solution "
            f"routes_used={summary['routes_used']} "
            f"truck_routes={summary['truck_routes']} van_routes={summary['van_routes']} "
            f"total_distance_m={summary['total_distance_m']} total_time_s={summary['total_time_s']} "
            f"total_capacity_units={summary['total_capacity_units']}"
        )
        return output

    # ---------------------------
    # Output formatting (unchanged structure)
    # ---------------------------
    def _format_output(self, manager, routing, solution, vehicle_types=None):
        """
        Output structure kept intact:
          - returns list of dicts with keys: v_id, v_type, jobs, skids
        Additional keys added (non-breaking):
          - capacity_units: same numeric value as "skids" (normalized units)
          - stops: list with arrival/departure times
        """
        if not solution:
            return None

        res = []
        skid_dim = routing.GetDimensionOrDie("Skids")  # normalized units
        time_dim = routing.GetDimensionOrDie("Time")

        TRUCK_SERVICE_TIME = int(self.costs["truck_service_time_s"])
        VAN_SERVICE_TIME = int(self.costs["van_service_time_s"])

        def svc_for(v_type: str, node: int) -> int:
            if node == self.depot:
                return 0
            return TRUCK_SERVICE_TIME if v_type == "truck" else VAN_SERVICE_TIME

        def fmt_hhmm(seconds: int) -> str:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h:02d}:{m:02d}"

        def stop_unit_summary(node: int) -> tuple[int, int]:
            if node == self.depot:
                return 0, 0
            tokens = self.node_to_jobs.get(node, [])
            signed_units = 0
            abs_units = 0
            for t in tokens:
                deliv_id, _, units = self._parse_token(t)
                d = self.delivery_by_id.get(deliv_id) if deliv_id else None
                pickup_flag = bool(self._delivery_field(d, "pickup", False)) if d else False
                if units:
                    unit_val = int(units)
                elif d:
                    unit_val = self._delivery_units(d)
                else:
                    unit_val = 0
                if unit_val:
                    signed_units += unit_val if pickup_flag else -unit_val
                    abs_units += abs(int(unit_val))
            return abs_units, signed_units

        vehicle_types = vehicle_types or self.vehicle_types
        visited_nodes = set()
        for v_id, v_type in enumerate(vehicle_types):
            index = routing.Start(v_id)
            plan = []
            stops = []
            total_distance = 0

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                next_index = solution.Value(routing.NextVar(index))
                next_node = manager.IndexToNode(next_index)

                plan.append(self.node_to_jobs.get(node, ["Depot"]))
                if node != self.depot:
                    visited_nodes.add(int(node))

                load_before = int(solution.Value(skid_dim.CumulVar(index)))
                load_after = int(solution.Value(skid_dim.CumulVar(next_index)))
                arr = solution.Value(time_dim.CumulVar(index))
                dep = arr + svc_for(v_type, node)
                stop_units, stop_units_signed = stop_unit_summary(node)

                stops.append({
                    "node": node,
                    "jobs": self.node_to_jobs.get(node, ["Depot"]),
                    "arrival_s": int(arr),
                    "departure_s": int(dep),
                    "arrival_hhmm": fmt_hhmm(int(arr)),
                    "departure_hhmm": fmt_hhmm(int(dep)),
                    "stop_units": int(stop_units),
                    "stop_units_signed": int(stop_units_signed),
                })

                is_reload = node in self.reload_nodes
                self._print_search(
                    "[solver][load] "
                    f"v_id={v_id} v_type={v_type} "
                    f"node={node} reload={is_reload} "
                    f"load_before={load_before} "
                    f"demand={int(stop_units_signed)} "
                    f"load_after={load_after} "
                    f"jobs={self.node_to_jobs.get(node, ['Depot'])}"
                )

                total_distance += int(self.distance_matrix[node][next_node])
                index = next_index

            end_node = manager.IndexToNode(index)
            plan.append(["Depot"])

            end_arr = solution.Value(time_dim.CumulVar(index))
            end_load = int(solution.Value(skid_dim.CumulVar(index)))
            stops.append({
                "node": end_node,
                "jobs": ["Depot"],
                "arrival_s": int(end_arr),
                "departure_s": int(end_arr),
                "arrival_hhmm": fmt_hhmm(int(end_arr)),
                "departure_hhmm": fmt_hhmm(int(end_arr)),
                "stop_units": 0,
                "stop_units_signed": 0,
            })
            self._print_search(
                "[solver][load] "
                f"v_id={v_id} v_type={v_type} "
                f"node={end_node} reload=False "
                f"load_before={end_load} demand=0 load_after={end_load} jobs=['Depot']"
            )

            if len(plan) > 2:
                used_units = int(solution.Value(skid_dim.CumulVar(index)))
                res.append({
                    "v_id": v_id,
                    "v_type": v_type,
                    "jobs": plan,
                    "skids": used_units,          # legacy key name (now normalized units)
                    "capacity_units": used_units, # explicit
                    "total_distance_m": int(total_distance),
                    "total_time_s": int(end_arr),
                    "stops": stops,
                })

        required_nodes = {
            int(n)
            for n in range(1, len(self.distance_matrix))
            if n not in self.reload_nodes
        }
        dropped_nodes = sorted(required_nodes - visited_nodes)
        if res:
            for r in res:
                r["dropped_nodes"] = dropped_nodes
                r["dropped_count"] = len(dropped_nodes)

        return res
