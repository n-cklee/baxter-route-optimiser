import folium
from folium import plugins

DEPOT_COORDS = (-33.7969, 150.9707)  # Old Toongabbie NSW 2146
SYDNEY_CENTRE = (-33.8688, 151.2093)


def _stop_popup(stop, eta, stop_num: int) -> str:
    bell = "🔔 Notify before arrival<br>" if stop.notification_required else ""
    return (
        f"<b>Stop {stop_num}: {stop.receiver_name}</b><br>"
        f"{stop.receiver_suburb}<br>"
        f"ETA: {eta.strftime('%H:%M')}<br>"
        f"Booking: {stop.booking_dt.strftime('%H:%M')}<br>"
        f"{bell}"
    )


def build_map(routes_by_wave: dict) -> folium.Map:
    """
    Build a folium map showing all van routes across all waves.
    routes_by_wave: {wave_key -> list[VanRoute]}
    """
    m = folium.Map(location=SYDNEY_CENTRE, zoom_start=10, tiles="OpenStreetMap")

    # Depot marker
    folium.Marker(
        location=DEPOT_COORDS,
        tooltip="Depot — Old Toongabbie",
        icon=folium.Icon(color="darkblue", icon="home", prefix="fa"),
    ).add_to(m)

    all_routes = []
    for wave_routes in routes_by_wave.values():
        all_routes.extend(wave_routes)

    for route in all_routes:
        colour = route.colour
        route_coords = [DEPOT_COORDS]

        for i, (stop, eta) in enumerate(zip(route.stops, route.eta_list)):
            stop_coords = (stop.lat, stop.lng)
            route_coords.append(stop_coords)

            # Circle marker with stop number
            folium.CircleMarker(
                location=stop_coords,
                radius=8,
                color=colour,
                fill=True,
                fill_color=colour,
                fill_opacity=0.85,
                popup=folium.Popup(
                    _stop_popup(stop, eta, i + 1), max_width=250
                ),
                tooltip=f"Van {route.van_id} · Stop {i + 1} · {stop.receiver_suburb}",
            ).add_to(m)

            # Stop number label
            folium.Marker(
                location=stop_coords,
                icon=folium.DivIcon(
                    html=f'<div style="font-size:9px;font-weight:bold;color:white;'
                         f'text-align:center;line-height:16px;">{i + 1}</div>',
                    icon_size=(16, 16),
                    icon_anchor=(8, 8),
                ),
            ).add_to(m)

        # Route line
        route_coords.append(DEPOT_COORDS)  # return to depot
        if len(route_coords) > 1:
            folium.PolyLine(
                locations=route_coords,
                color=colour,
                weight=2.5,
                opacity=0.7,
                tooltip=f"Van {route.van_id} ({route.stop_count} stops)",
            ).add_to(m)

    # Fit map to markers if there are any
    if all_routes:
        all_lats = [DEPOT_COORDS[0]]
        all_lngs = [DEPOT_COORDS[1]]
        for route in all_routes:
            for stop in route.stops:
                all_lats.append(stop.lat)
                all_lngs.append(stop.lng)
        sw = (min(all_lats) - 0.05, min(all_lngs) - 0.05)
        ne = (max(all_lats) + 0.05, max(all_lngs) + 0.05)
        m.fit_bounds([sw, ne])

    return m
