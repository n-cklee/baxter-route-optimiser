import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

MIN_ROUTE_SECONDS = 9 * 3600   # 9-hour billing minimum / target fill level
MAX_ROUTE_SECONDS = 12 * 3600  # 12-hour optimal upper bound
END_OF_DAY_HOUR = 18           # hard curfew: all vans back to depot by 6 PM
_ITER_TIME_LIMIT = 30          # seconds per binary-search iteration


@dataclass
class Stop:
    index: int          # position in the coords list (0 = depot)
    receiver_name: str
    receiver_suburb: str
    booking_dt: datetime
    notification_required: bool
    lat: float
    lng: float
    df_row_index: int   # original DataFrame index for back-reference


@dataclass
class VanRoute:
    van_id: int
    colour: str
    stops: list[Stop] = field(default_factory=list)
    departure_time: datetime = None
    eta_list: list[datetime] = field(default_factory=list)
    total_seconds: int = 0
    waves_covered: list[str] = field(default_factory=list)

    @property
    def return_eta(self) -> datetime | None:
        if self.departure_time is None:
            return None
        return self.departure_time + timedelta(seconds=self.total_seconds)

    @property
    def stop_count(self) -> int:
        return len(self.stops)

    @property
    def total_hours(self) -> float:
        return self.total_seconds / 3600

    @property
    def billing_hours(self) -> float:
        return max(self.total_hours, 9.0)

    @property
    def route_status(self) -> str:
        if self.return_eta is not None and self.departure_time is not None:
            cutoff = self.departure_time.replace(
                hour=END_OF_DAY_HOUR, minute=0, second=0, microsecond=0
            )
            if self.return_eta > cutoff:
                return "LATE"
        if self.total_seconds >= MIN_ROUTE_SECONDS:
            return "OPTIMAL"
        return "UNDER MINIMUM"

    @property
    def over_9h(self) -> bool:
        return self.total_seconds > MAX_ROUTE_SECONDS


VAN_COLOURS = [
    "#E63946", "#2196F3", "#4CAF50", "#FF9800", "#9C27B0",
    "#00BCD4", "#FF5722", "#8BC34A", "#F44336", "#3F51B5",
]


def _seconds_since_midnight(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _solve_wave_fixed(
    stops: list[Stop],
    duration_matrix: np.ndarray,
    wave_departure: datetime,
    num_vehicles: int,
    time_limit_s: int = _ITER_TIME_LIMIT,
) -> list[VanRoute]:
    """
    Solve a single VRP wave with exactly num_vehicles available.

    Objective: minimize arc costs (OSRM travel + dwell, via SetArcCostEvaluatorOfAllVehicles)
    plus a span cost coefficient on the time dimension to balance route durations evenly.

    Hard constraint: all routes must return to depot by END_OF_DAY_HOUR.

    Returns empty list if OR-Tools finds no feasible solution within time_limit_s.
    """
    n_nodes = len(stops) + 1  # +1 for depot at index 0
    depot_index = 0

    int_matrix = np.round(duration_matrix).astype(int).tolist()

    manager = pywrapcp.RoutingIndexManager(n_nodes, num_vehicles, depot_index)
    routing = pywrapcp.RoutingModel(manager)

    # Transit callback — uses real OSRM durations with dwell already baked in
    def transit(from_idx, to_idx):
        i = manager.IndexToNode(from_idx)
        j = manager.IndexToNode(to_idx)
        return int_matrix[i][j]

    transit_cb = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    wave_start_s = _seconds_since_midnight(wave_departure)
    end_of_day_s = max(0, END_OF_DAY_HOUR * 3600 - wave_start_s)

    # Time dimension — hard ceiling enforces the 6 PM curfew
    routing.AddDimension(
        transit_cb,
        slack_max=3600,
        capacity=end_of_day_s,
        fix_start_cumul_to_zero=True,
        name="Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # Span cost: penalise long individual routes to balance durations across vans
    time_dim.SetSpanCostCoefficientForAllVehicles(1)

    # Time windows: each stop must be visited after its booking time and before 6 PM
    for stop_pos, stop in enumerate(stops):
        node = stop_pos + 1
        idx = manager.NodeToIndex(node)
        earliest = max(0, _seconds_since_midnight(stop.booking_dt) - wave_start_s)
        latest = min(earliest + 4 * 3600, end_of_day_s)
        time_dim.CumulVar(idx).SetRange(earliest, max(earliest, latest))

    # Hard 6 PM ceiling on every vehicle's end-depot cumulative time
    for v in range(num_vehicles):
        end_idx = routing.End(v)
        time_dim.CumulVar(end_idx).SetMax(end_of_day_s)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = time_limit_s

    solution = routing.SolveWithParameters(params)

    if not solution:
        return []

    routes: list[VanRoute] = []
    for v in range(num_vehicles):
        if routing.IsVehicleUsed(solution, v):
            route = VanRoute(
                van_id=len(routes) + 1,
                colour=VAN_COLOURS[len(routes) % len(VAN_COLOURS)],
                departure_time=wave_departure,
            )
            idx = routing.Start(v)
            while not routing.IsEnd(idx):
                node = manager.IndexToNode(idx)
                if node != depot_index:
                    stop = stops[node - 1]
                    cum_s = solution.Value(time_dim.CumulVar(idx))
                    eta = wave_departure + timedelta(seconds=cum_s)
                    route.stops.append(stop)
                    route.eta_list.append(eta)
                idx = solution.Value(routing.NextVar(idx))

            end_idx = routing.End(v)
            route.total_seconds = solution.Value(time_dim.CumulVar(end_idx))
            routes.append(route)

    return routes


def solve_wave(
    stops: list[Stop],
    duration_matrix: np.ndarray,
    wave_departure: datetime,
) -> list[VanRoute]:
    """
    Find the minimum number of vehicles for this wave via linear search from 1 to ceil(n/4).

    For each candidate k:
      - Solve VRP with k vehicles (objective: minimise arc costs + span cost for balance)
      - Accept if OR-Tools finds a feasible solution where every route fits within
        (END_OF_DAY_HOUR - wave_departure) — the 6 PM curfew window
    Safety cap: ceil(stop_count / 4) to bound solve time.
    """
    wave_start_s = _seconds_since_midnight(wave_departure)
    window_s = max(0, END_OF_DAY_HOUR * 3600 - wave_start_s)
    n_stops = len(stops)
    cap = max(1, math.ceil(n_stops / 4))

    print(
        f"[VRP] wave={wave_departure.strftime('%H:%M')} "
        f"stops={n_stops} window={window_s/3600:.2f}h cap={cap}",
        flush=True,
    )

    for k in range(1, cap + 1):
        routes = _solve_wave_fixed(stops, duration_matrix, wave_departure, k)

        if not routes:
            print(f"  k={k}: no feasible solution", flush=True)
            continue

        max_dur = max(r.total_seconds for r in routes)
        n_used = len(routes)
        print(
            f"  k={k}: {n_used} van(s) used  "
            + "  ".join(
                f"Van{i+1}={r.stop_count}stops/{r.total_seconds/3600:.2f}h"
                for i, r in enumerate(routes)
            )
            + f"  max={max_dur/3600:.2f}h  window={window_s/3600:.2f}h",
            flush=True,
        )

        if max_dur <= window_s:
            print(f"  → accepted with k={k} ({n_used} active vans)", flush=True)
            return routes

    # Fallback: return cap-vehicle solution even if it marginally breaches the window
    print(
        f"  → cap={cap} reached; returning best cap solution", flush=True
    )
    return _solve_wave_fixed(stops, duration_matrix, wave_departure, cap) or []


def assign_global_van_ids(
    routes_by_wave: dict[str, list[VanRoute]],
    wave_times: dict[str, datetime],
) -> None:
    """
    Assign globally unique van IDs across waves, reusing vans that have
    returned to the depot before a later wave's departure. Modifies routes
    in-place and populates each route's waves_covered list.
    """
    all_entries: list[tuple[datetime, str, VanRoute]] = []
    for wave_key, routes in routes_by_wave.items():
        dep = wave_times[wave_key]
        for route in routes:
            all_entries.append((dep, wave_key, route))
    all_entries.sort(key=lambda x: x[0])

    global_van_id = 1
    colour_slot: dict[int, int] = {}
    van_waves: dict[int, list[str]] = {}
    available: list[tuple[datetime, int]] = []

    for dep, wave_key, route in all_entries:
        reusable = [(rt, vid) for rt, vid in available if rt <= dep]
        still_out = [(rt, vid) for rt, vid in available if rt > dep]

        if reusable:
            reusable.sort()
            _, reused_id = reusable.pop(0)
            still_out.extend(reusable)
            route.van_id = reused_id
        else:
            route.van_id = global_van_id
            colour_slot[global_van_id] = global_van_id - 1
            van_waves[global_van_id] = []
            global_van_id += 1

        route.colour = VAN_COLOURS[colour_slot[route.van_id] % len(VAN_COLOURS)]
        van_waves[route.van_id].append(wave_key)
        route.waves_covered = van_waves[route.van_id]

        ret = route.return_eta
        if ret is not None:
            still_out.append((ret, route.van_id))
        available = still_out


def solve_all_waves(
    stops_by_wave: dict[str, list[Stop]],
    matrices_by_wave: dict[str, np.ndarray],
    wave_times: dict[str, datetime],
) -> dict[str, list[VanRoute]]:
    """Solve VRP for every booking-time wave independently."""
    results = {}
    for wave_key, wave_stops in stops_by_wave.items():
        if not wave_stops:
            continue
        matrix = matrices_by_wave[wave_key]
        departure = wave_times[wave_key]
        results[wave_key] = solve_wave(wave_stops, matrix, departure)
    return results
