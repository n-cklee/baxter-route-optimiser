import requests
import numpy as np

OSRM_BASE = "http://router.project-osrm.org/table/v1/driving"
DWELL_SECONDS = 20 * 60  # 20 minutes per stop
BATCH_SIZE = 80           # stay well under OSRM's ~100-coordinate cap


def _build_coord_string(coords: list[tuple[float, float]]) -> str:
    """coords: list of (lat, lng) -> OSRM expects lng,lat order."""
    return ";".join(f"{lng},{lat}" for lat, lng in coords)


def _fetch_osrm_table(coords: list[tuple[float, float]]) -> np.ndarray:
    """Single OSRM table request. Returns duration matrix in seconds."""
    coord_str = _build_coord_string(coords)
    url = f"{OSRM_BASE}/{coord_str}?annotations=duration"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data.get('code')} — {data.get('message')}")
    matrix = np.array(data["durations"], dtype=float)
    # OSRM returns None for unreachable pairs — replace with large penalty
    matrix = np.where(matrix is None, 9999 * 60, matrix)
    return matrix


def build_duration_matrix(
    coords: list[tuple[float, float]], include_dwell: bool = True
) -> np.ndarray:
    """
    Build an N×N travel-duration matrix for all stops.

    For large inputs (>BATCH_SIZE), split into row-batches and stitch.
    Dwell time is added to the DEPARTURE side (i.e., the time spent at each stop
    before leaving for the next) by adding DWELL_SECONDS to each row except
    the depot (index 0).

    Returns matrix in seconds.
    """
    n = len(coords)
    if n <= BATCH_SIZE:
        matrix = _fetch_osrm_table(coords)
    else:
        matrix = _stitch_large_matrix(coords)

    if include_dwell:
        # Add dwell time to all non-depot rows (stop indices 1..n-1)
        # matrix[i][j] = travel_time(i→j) + dwell_at_i
        dwell_row = np.full(n, DWELL_SECONDS)
        dwell_row[0] = 0  # no dwell at depot
        matrix = matrix + dwell_row[:, np.newaxis]
        # Zero out the diagonal (self-loops keep their dwell — that's intentional
        # for OR-Tools time window accounting, so we leave it)

    return matrix


def _stitch_large_matrix(coords: list[tuple[float, float]]) -> np.ndarray:
    """
    For >BATCH_SIZE stops, fetch partial matrices with the depot always included,
    then assemble the full square matrix.

    Strategy: fix the depot (index 0) as a permanent participant in every batch
    and treat each batch as an independent sub-matrix. This is an approximation
    but avoids hitting OSRM with >100 coords at once.
    """
    n = len(coords)
    full = np.zeros((n, n))

    depot = coords[0]
    non_depot = coords[1:]

    # Process non-depot coords in batches
    for batch_start in range(0, len(non_depot), BATCH_SIZE - 1):
        batch_coords_nd = non_depot[batch_start : batch_start + BATCH_SIZE - 1]
        batch_coords = [depot] + batch_coords_nd
        batch_indices = [0] + list(range(batch_start + 1, batch_start + 1 + len(batch_coords_nd)))

        sub = _fetch_osrm_table(batch_coords)
        for local_i, global_i in enumerate(batch_indices):
            for local_j, global_j in enumerate(batch_indices):
                full[global_i][global_j] = sub[local_i][local_j]

    return full
