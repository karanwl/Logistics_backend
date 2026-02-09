import logging

from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from config import settings

logger = logging.getLogger(__name__)


class RouteOptimizer:
    """
    IMPORTANT CAPACITY MODEL (Skids + Frames)
    ----------------------------------------
    We normalize load into a shared "capacity_units" scale:

      - 1 med skid   = 20 units
      - 1 large skid = 25 units
      - 1 small skid = 10 units
      - 1 frame      = 1 unit
      - 1 exp frame  = 1 unit
      - truck capacity = 140 units

    Van truck-only constraints stay controlled by truck_req_cb rules.
    """

    # Defaults (kept here so solver is self-contained)
    MED_SKID_UNITS = 20
    LARGE_SKID_UNITS = 25
    SMALL_SKID_UNITS = 10
    FRAME_UNITS = 1
    TRUCK_CAPACITY_UNITS = 140
    VAN_FRAME_LIMIT = 3
    VAN_DOOR_LIMIT = 3
    VAN_EXP_FRAME_LIMIT = 10

    DEFAULT_PENALTIES = {
        "truck_cost_factor": 1.12,
        "truck_time_factor": 1.08,
        "truck_serve_van_eligible_penalty": 2500,
        "time_span_cost_coeff": 5,
        "downtown_soft_latest_s": 4 * 3600,   # 10:00 (from 6:00 start)
        "downtown_hard_latest_s": 6 * 3600,   # 12:00 hard cap
        "downtown_late_penalty_per_sec": 60,
        "van_fixed_cost": 0,
        "truck_fixed_cost_physical": 500,
        "truck_fixed_cost_overflow": 1000000,
        "drop_node_penalty": 1000000,
        "shift_seconds": 7.5 * 3600,
        "waiting_slack_seconds": 600,
        "search_time_limit_s": 30,
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

        # Allow overriding penalties via penalty_profile
        self.penalties = dict(self.DEFAULT_PENALTIES)
        if isinstance(penalty_profile, dict):
            for key, val in penalty_profile.items():
                if key in self.penalties and val is not None:
                    self.penalties[key] = val

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

    # ---------------------------
    # Utility / data access
    # ---------------------------
    def _prepare(self, matrix):
        return [
            [int(round(val)) if (val is not None and val < 10**7) else 999999 for val in row]
            for row in matrix
        ]

    def _delivery_id(self, d) -> str:
        if isinstance(d, dict):
            return str(d.get("id", "")).strip()
        return str(getattr(d, "id", "")).strip()

    def _delivery_field(self, d, field_name: str, default=0):
        if isinstance(d, dict):
            return d.get(field_name, default)
        return getattr(d, field_name, default)

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
            if pickup and requires_empty:
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

    def _solve_model(self, service_time=1200, max_route_time=43200):
        manager = pywrapcp.RoutingIndexManager(
            len(self.distance_matrix),
            len(self.vehicle_types),
            self.depot
        )
        routing = pywrapcp.RoutingModel(manager)

        # ---------------------------
        # Callbacks for constraints
        # ---------------------------
        def truck_req_cb(from_idx):
            node_idx = manager.IndexToNode(from_idx)
            if node_idx == self.depot:
                return 0

            door_total, frame_total, exp_frame_total, skid_present = self._node_cargo_summary(node_idx)
            if skid_present:
                return 1
            if (
                door_total > self.VAN_DOOR_LIMIT
                or frame_total > self.VAN_FRAME_LIMIT
                or exp_frame_total > self.VAN_EXP_FRAME_LIMIT
            ):
                return 1
            return 0

        def node_unit_demand(from_idx: int) -> int:
            node_idx = manager.IndexToNode(from_idx)
            if node_idx == self.depot:
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

        def make_vehicle_unit_cb(truck_flag: bool):
            def vehicle_unit_cb(from_idx, _to_idx):
                if not truck_flag:
                    return 0
                node_idx = manager.IndexToNode(from_idx)
                if node_idx == self.depot:
                    return 0
                return node_unit_demand(from_idx)

            return vehicle_unit_cb

        def node_requires_truck(node: int) -> bool:
            door_total, frame_total, exp_frame_total, skid_present = self._node_cargo_summary(node)
            if self._node_requires_empty_pickup(node):
                return True
            if skid_present:
                return True
            if (
                door_total > self.VAN_DOOR_LIMIT
                or frame_total > self.VAN_FRAME_LIMIT
                or exp_frame_total > self.VAN_EXP_FRAME_LIMIT
            ):
                return True
            return False

        def node_units(node: int) -> int:
            tokens = self.node_to_jobs.get(node, [])
            total = 0
            for t in tokens:
                deliv_id, _, units = self._parse_token(t)
                if units:
                    total += int(units)
                elif deliv_id:
                    d = self.delivery_by_id.get(deliv_id)
                    if d:
                        total += self._delivery_units(d)
            return int(total)

        van_eligible = [False] * len(self.distance_matrix)
        for node in range(1, len(self.distance_matrix)):
            van_eligible[node] = not node_requires_truck(node)

        truck_idx = routing.RegisterUnaryTransitCallback(truck_req_cb)
        unit_cbs = []
        for v_type in self.vehicle_types:
            cb_idx = routing.RegisterTransitCallback(make_vehicle_unit_cb(v_type == "truck"))
            unit_cbs.append(cb_idx)

        # ---------------------------
        # Vehicle-dependent economics
        # ---------------------------
        TRUCK_COST_FACTOR = float(self.penalties["truck_cost_factor"])
        TRUCK_TIME_FACTOR = float(self.penalties["truck_time_factor"])
        TRUCK_SERVE_VAN_ELIGIBLE_PENALTY = int(self.penalties["truck_serve_van_eligible_penalty"])

        TRUCK_SERVICE_TIME = 30 * 60
        VAN_SERVICE_TIME = 10 * 60

        def make_vehicle_cost_cb(truck_flag: bool):
            def cost_cb(i, j):
                from_node = manager.IndexToNode(i)
                to_node = manager.IndexToNode(j)
                base = self.distance_matrix[from_node][to_node]

                if not truck_flag:
                    return base

                scaled = int(base * TRUCK_COST_FACTOR)
                if to_node != self.depot and van_eligible[to_node]:
                    scaled += TRUCK_SERVE_VAN_ELIGIBLE_PENALTY
                return scaled
            return cost_cb

        for v_id, v_type in enumerate(self.vehicle_types):
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
        for v_id, v_type in enumerate(self.vehicle_types):
            is_truck = (v_type == "truck")
            cb_idx = routing.RegisterTransitCallback(make_vehicle_time_cb(is_truck))
            time_cbs.append(cb_idx)

        # ---------------------------
        # Time dimension
        # ---------------------------
        SHIFT_SECONDS = int(self.penalties["shift_seconds"])
        WAITING_SLACK = int(self.penalties["waiting_slack_seconds"])

        routing.AddDimensionWithVehicleTransitAndCapacity(
            time_cbs,
            WAITING_SLACK,
            [SHIFT_SECONDS] * len(self.vehicle_types),
            True,
            "Time"
        )
        time_dim = routing.GetDimensionOrDie("Time")

        for v_id in range(len(self.vehicle_types)):
            time_dim.CumulVar(routing.End(v_id)).SetMax(SHIFT_SECONDS)

        time_dim.SetGlobalSpanCostCoefficient(int(self.penalties["time_span_cost_coeff"]))

        # ---------------------------
        # Truck-only + Skids capacity
        # ---------------------------
        truck_caps = [999 if v == "truck" else 0 for v in self.vehicle_types]
        routing.AddDimensionWithVehicleCapacity(truck_idx, 0, truck_caps, True, "TruckOnly")

        unit_caps = [self.truck_capacity_units if v == "truck" else 0 for v in self.vehicle_types]
        routing.AddDimensionWithVehicleTransitAndCapacity(unit_cbs, 0, unit_caps, False, "Skids")
        skid_dim = routing.GetDimensionOrDie("Skids")

        pickup_empty_nodes = [
            node
            for node in range(1, len(self.distance_matrix))
            if self._node_requires_empty_pickup(node)
        ]
        if pickup_empty_nodes:
            truck_vehicle_ids = [int(idx) for idx, v_type in enumerate(self.vehicle_types) if v_type == "truck"]
            solver = routing.solver()
            ends = [routing.End(v_id) for v_id in range(len(self.vehicle_types))]
            for node in pickup_empty_nodes:
                idx = int(manager.NodeToIndex(node))
                if truck_vehicle_ids:
                    # Some OR-Tools builds have strict type expectations for SetAllowedVehiclesForIndex.
                    # Fall back to restricting the vehicle variable domain if the wrapper rejects lists.
                    try:
                        routing.SetAllowedVehiclesForIndex(list(map(int, truck_vehicle_ids)), idx)
                    except TypeError:
                        routing.VehicleVar(idx).SetValues(list(map(int, truck_vehicle_ids)))
                skid_dim.CumulVar(idx).SetValue(0)
                end_for_vehicle = solver.Element(ends, routing.VehicleVar(idx))
                solver.Add(routing.NextVar(idx) == end_for_vehicle)

        # ---------------------------
        # Downtown-first (hard + soft)
        # ---------------------------
        DOWNTOWN_SOFT_LATEST = int(self.penalties["downtown_soft_latest_s"])
        DOWNTOWN_HARD_LATEST = int(self.penalties["downtown_hard_latest_s"])
        LATE_PENALTY_PER_SEC = int(self.penalties["downtown_late_penalty_per_sec"])
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

        # ---------------------------
        # Fixed costs
        # ---------------------------
        VAN_FIXED_COST = int(self.penalties["van_fixed_cost"])
        TRUCK_FIXED_COST_PHYSICAL = int(self.penalties["truck_fixed_cost_physical"])
        TRUCK_FIXED_COST_OVERFLOW = int(self.penalties["truck_fixed_cost_overflow"])

        truck_seen = 0
        for v_id, v_type in enumerate(self.vehicle_types):
            if v_type == "van":
                routing.SetFixedCostOfVehicle(VAN_FIXED_COST, v_id)
            else:
                truck_seen += 1
                cost = TRUCK_FIXED_COST_PHYSICAL if truck_seen <= self.physical_truck_count else TRUCK_FIXED_COST_OVERFLOW
                routing.SetFixedCostOfVehicle(cost, v_id)

        # ---------------------------
        # Allow dropping any non-depot node
        # ---------------------------
        for node in range(1, len(self.distance_matrix)):
            routing.AddDisjunction([manager.NodeToIndex(node)], int(self.penalties["drop_node_penalty"]))

        # ---------------------------
        # Search parameters
        # ---------------------------
        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_params.time_limit.seconds = max(1, int(self.penalties["search_time_limit_s"]))

        solution = routing.SolveWithParameters(search_params)
        if not solution:
            return None

        output = self._format_output(manager, routing, solution)
        if output is None:
            return None

        return output

    # ---------------------------
    # Public solve (single model)
    # ---------------------------
    def solve(self, service_time=1200, max_route_time=43200):
        self._print_search("[solver] single-model solve started")
        output = self._solve_model(service_time=service_time, max_route_time=max_route_time)
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
    def _format_output(self, manager, routing, solution):
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

        TRUCK_SERVICE_TIME = 30 * 60
        VAN_SERVICE_TIME = 10 * 60

        def svc_for(v_type: str, node: int) -> int:
            if node == self.depot:
                return 0
            return TRUCK_SERVICE_TIME if v_type == "truck" else VAN_SERVICE_TIME

        def fmt_hhmm(seconds: int) -> str:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h:02d}:{m:02d}"

        for v_id, v_type in enumerate(self.vehicle_types):
            index = routing.Start(v_id)
            plan = []
            stops = []
            total_distance = 0

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                next_index = solution.Value(routing.NextVar(index))
                next_node = manager.IndexToNode(next_index)

                plan.append(self.node_to_jobs.get(node, ["Depot"]))

                arr = solution.Value(time_dim.CumulVar(index))
                dep = arr + svc_for(v_type, node)

                stops.append({
                    "node": node,
                    "jobs": self.node_to_jobs.get(node, ["Depot"]),
                    "arrival_s": int(arr),
                    "departure_s": int(dep),
                    "arrival_hhmm": fmt_hhmm(int(arr)),
                    "departure_hhmm": fmt_hhmm(int(dep)),
                })

                total_distance += int(self.distance_matrix[node][next_node])
                index = next_index

            end_node = manager.IndexToNode(index)
            plan.append(["Depot"])

            end_arr = solution.Value(time_dim.CumulVar(index))
            stops.append({
                "node": end_node,
                "jobs": ["Depot"],
                "arrival_s": int(end_arr),
                "departure_s": int(end_arr),
                "arrival_hhmm": fmt_hhmm(int(end_arr)),
                "departure_hhmm": fmt_hhmm(int(end_arr)),
            })

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

        return res
