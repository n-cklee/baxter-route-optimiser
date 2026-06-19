import streamlit as st
import pandas as pd
from datetime import datetime
from pathlib import Path

st.set_page_config(
    page_title="Baxter Route Optimiser",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ──────────────────────────────────────────────────────────────
css_path = Path(__file__).parent / "ui" / "styles.css"
if css_path.exists():
    st.markdown(f"<style>{css_path.read_text()}</style>", unsafe_allow_html=True)

# ── Header bar ──────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="baxter-header">
        <h1>🚐 Baxter Healthcare — Route Optimiser</h1>
        <p>AI-powered compounding drug fleet planning · Sydney, NSW</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Lazy imports (only loaded after button press) ────────────────────────────
DEPOT_ADDRESS = "Old Toongabbie NSW 2146"
DEPOT_COORDS = (-33.7969, 150.9707)
DEMO_FILE = Path(__file__).parent / "data" / "sample.xlsx"


def _load_css_variable(key: str, default: str) -> str:
    return default


# ── Session state defaults ───────────────────────────────────────────────────
if "optimised" not in st.session_state:
    st.session_state.optimised = False
if "results" not in st.session_state:
    st.session_state.results = None


# ════════════════════════════════════════════════════════════════════════════
# PAGE 1 — Upload & Configure
# ════════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-title">📂 Upload & Configure</div>', unsafe_allow_html=True)

col_upload, col_config = st.columns([1, 1], gap="large")

with col_upload:
    uploaded_file = st.file_uploader(
        "Upload consignment Excel file",
        type=["xlsx", "xls"],
        help="The daily dispatch spreadsheet (DisplayExcel2007_*.xlsx)",
    )
    demo_mode = False
    if not uploaded_file:
        if DEMO_FILE.exists():
            demo_mode = st.checkbox("Use demo dataset (sample.xlsx)", value=True)
        else:
            st.info("Upload an Excel file to get started.")

with col_config:
    depot_address = st.text_input("Depot address", value=DEPOT_ADDRESS)
    st.caption("Geocoded automatically — edit only if the depot has changed.")

# Determine file source
file_source = None
if uploaded_file:
    file_source = uploaded_file
elif demo_mode and DEMO_FILE.exists():
    file_source = str(DEMO_FILE)

# Load and show date picker once file is known
selected_date = None
raw_df = None

if file_source is not None:
    from optimiser.data_loader import load_excel, available_dates

    with st.spinner("Reading file…"):
        try:
            raw_df = load_excel(file_source)
            dates = available_dates(raw_df)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            dates = []

    if dates:
        selected_date = st.selectbox(
            "Select delivery date to optimise",
            options=dates,
            format_func=lambda d: d.strftime("%A, %-d %B %Y") if hasattr(d, "strftime") else str(d),
            index=len(dates) - 1,
        )

# ── Run button ───────────────────────────────────────────────────────────────
run_ready = file_source is not None and selected_date is not None
run_btn = st.button(
    "▶ Run Optimisation",
    type="primary",
    disabled=not run_ready,
    use_container_width=False,
)

if run_btn and run_ready:
    st.session_state.optimised = False
    st.session_state.results = None

    from optimiser.data_loader import prepare
    from optimiser.geocoder import geocode_stops, attach_coords, failed_geocodes
    from optimiser.distance_matrix import build_duration_matrix
    from optimiser.vrp_solver import solve_wave, assign_global_van_ids, Stop, VAN_COLOURS
    from optimiser.comparison import compare
    import numpy as np

    progress = st.progress(0, text="Loading consignments…")
    status = st.empty()

    # ── Step 1: Load & filter ──────────────────────────────────────────────
    status.info("⏳ Filtering Set Run consignments…")
    try:
        df, _ = prepare(file_source, selected_date)
    except Exception as e:
        st.error(f"Data preparation failed: {e}")
        st.stop()

    if df.empty:
        st.warning("No 'Set Run' consignments found for the selected date.")
        st.stop()

    progress.progress(10, text=f"Found {len(df)} consignments — geocoding stops…")

    # ── Step 2: Geocode ────────────────────────────────────────────────────
    status.info("📍 Geocoding delivery addresses (this may take a minute)…")

    receiver_stops = [
        {"suburb": row["Receiver Suburb"], "postcode": row["Receiver Postcode"]}
        for _, row in df.iterrows()
    ]
    depot_stop = [{"suburb": "Old Toongabbie", "postcode": "2146"}]

    def geo_progress(i, total, suburb):
        pct = 10 + int(40 * i / max(total, 1))
        progress.progress(pct, text=f"Geocoding {suburb}… ({i}/{total})")

    try:
        geocoded = geocode_stops(depot_stop + receiver_stops, progress_callback=geo_progress)
    except Exception as e:
        st.error(f"Geocoding failed: {e}")
        st.stop()

    df = attach_coords(df, "Receiver Suburb", "Receiver Postcode", geocoded)

    failed = failed_geocodes(geocoded)
    if failed:
        st.markdown(
            f'<div class="geocode-warning">⚠️ {len(failed)} address(es) could not be geocoded '
            f'and were skipped: {", ".join(failed[:10])}</div>',
            unsafe_allow_html=True,
        )

    df_valid = df.dropna(subset=["lat", "lng"]).copy()
    if df_valid.empty:
        st.error("No consignments could be geocoded. Check address data.")
        st.stop()

    progress.progress(55, text="Building travel-time matrix…")

    # ── Step 3: Derive waves dynamically from data ─────────────────────────
    status.info("🗺️ Building distance/time matrix via OSRM…")

    # Wave key = hour-truncated departure, formatted as zero-padded 24-h "HH:MM".
    # Sorting alphabetically on this format is identical to sorting chronologically.
    df_valid["wave_key"] = df_valid["booking_dt"].dt.floor("h").dt.strftime("%H:%M")

    # Build a chronologically sorted Series: wave_key → representative departure datetime
    wave_departure_series = (
        df_valid.groupby("wave_key")["booking_dt"]
        .first()
        .dt.floor("h")
        .sort_values()
    )

    routes_by_wave: dict = {}
    wave_times: dict = {}  # wave_key → departure datetime
    wave_step = 30 / max(len(wave_departure_series), 1)

    for wi, (wave_key, wave_departure_ts) in enumerate(wave_departure_series.items()):
        wave_departure = wave_departure_ts.to_pydatetime()
        wave_df = df_valid[df_valid["wave_key"] == wave_key].copy()
        if wave_df.empty:
            continue

        # Build stop list (depot at index 0)
        depot_lat, depot_lng = DEPOT_COORDS
        coords = [(depot_lat, depot_lng)] + list(zip(wave_df["lat"], wave_df["lng"]))

        stops = []
        for idx, (_, row) in enumerate(wave_df.iterrows()):
            stops.append(Stop(
                index=idx + 1,
                receiver_name=str(row.get("Receiver Name", "")),
                receiver_suburb=str(row.get("Receiver Suburb", "")),
                booking_dt=row["booking_dt"],
                notification_required=bool(row.get("notification_required", False)),
                lat=float(row["lat"]),
                lng=float(row["lng"]),
                df_row_index=idx,
            ))

        # ── Step 4: Distance matrix ────────────────────────────────────────
        try:
            matrix = build_duration_matrix(coords)
        except Exception as e:
            st.warning(f"OSRM call failed for wave {wave_key}: {e} — skipping wave.")
            continue

        pct = 55 + int(wave_step * (wi + 1))
        progress.progress(min(pct, 85), text=f"Optimising wave {wave_key}…")
        status.info(f"🧮 Running VRP optimiser for {wave_key} wave ({len(stops)} stops)…")

        # ── Step 5: VRP solve — binary search on vehicle count ────────────
        wave_times[wave_key] = wave_departure

        # Binary search on vehicle count; details printed to terminal
        try:
            wave_routes = solve_wave(stops, matrix, wave_departure)
        except Exception as e:
            st.warning(f"Optimiser failed for wave {wave_key}: {e}")
            wave_routes = []

        routes_by_wave[wave_key] = wave_routes

    # Assign globally unique van IDs, reusing IDs of vans that have returned
    assign_global_van_ids(routes_by_wave, wave_times)

    progress.progress(90, text="Computing comparison metrics…")
    status.info("📊 Comparing with manual plan…")

    comparison = compare(df_valid, routes_by_wave)

    progress.progress(100, text="Done!")
    status.empty()
    progress.empty()

    st.session_state.optimised = True
    st.session_state.results = {
        "df": df_valid,
        "routes_by_wave": routes_by_wave,
        "wave_times": wave_times,
        "comparison": comparison,
        "selected_date": selected_date,
    }


# ════════════════════════════════════════════════════════════════════════════
# PAGE 2 — Optimisation Results
# ════════════════════════════════════════════════════════════════════════════
if st.session_state.optimised and st.session_state.results:
    res = st.session_state.results
    df_res = res["df"]
    routes_by_wave = res["routes_by_wave"]
    wave_times = res["wave_times"]
    comparison = res["comparison"]
    date_label = pd.to_datetime(res["selected_date"]).strftime("%A, %-d %B %Y")

    st.markdown(
        f'<div class="section-title">📊 Optimisation Results — {date_label}</div>',
        unsafe_allow_html=True,
    )

    # Section A — KPI Cards + debug panel
    from ui.kpi_cards import render_kpi_row, render_wave_breakdown, render_route_cards, render_van_assignments, render_debug_panel
    render_kpi_row(comparison)
    render_wave_breakdown(comparison)
    render_debug_panel(routes_by_wave, wave_times)

    # Section B — Map
    st.markdown('<div class="section-title">🗺️ Interactive Route Map</div>', unsafe_allow_html=True)
    all_routes = [r for routes in routes_by_wave.values() for r in routes]
    if all_routes:
        from ui.map_builder import build_map
        from streamlit_folium import st_folium

        fmap = build_map(routes_by_wave)
        st_folium(fmap, use_container_width=True, height=550, returned_objects=[])
    else:
        st.info("No routes were generated — check geocoding and OSRM connectivity.")

    # Section C — Van Assignments
    st.markdown('<div class="section-title">📋 Van Assignments</div>', unsafe_allow_html=True)
    render_van_assignments(routes_by_wave, df_res)

    # Section D — Route Cards
    st.markdown('<div class="section-title">🚐 Route Cards</div>', unsafe_allow_html=True)
    render_route_cards(routes_by_wave)

    # ════════════════════════════════════════════════════════════════════════
    # PAGE 3 — Data Table
    # ════════════════════════════════════════════════════════════════════════
    st.markdown('<div class="section-title">📋 Filtered Consignment Data</div>', unsafe_allow_html=True)

    display_cols = [
        "Service Level", "Booking Time", "Sender Name", "Sender Suburb",
        "Receiver Name", "Receiver Suburb", "Receiver Postcode",
        "Weight", "Qty", "Status", "notification_required",
        "manual_driver", "manual_wave",
    ]
    display_cols = [c for c in display_cols if c in df_res.columns]
    from ui.kpi_cards import _html_table
    table_rows = df_res[display_cols].fillna("").astype(str).to_dict(orient="records")
    st.markdown(_html_table(table_rows), unsafe_allow_html=True)

    csv = df_res[display_cols].to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download CSV",
        data=csv,
        file_name=f"baxter_set_run_{res['selected_date']}.csv",
        mime="text/csv",
    )
