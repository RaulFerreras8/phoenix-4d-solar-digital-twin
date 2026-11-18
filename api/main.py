import os
import json
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, text

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://admin_gis:solar_twin_secure@postgis_solar_db:5432/phoenix_solar_db",
)

engine = create_engine(DB_URL, pool_pre_ping=True)

app = FastAPI(
    title="Phoenix 4D LiDAR Solar & Shadow Digital Twin API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def serve_dashboard():
    """Serve the 4D WebGL Digital Twin interface."""
    return FileResponse("/app/frontend/index.html")


@app.get("/api/summary")
def get_district_summary():
    """Return aggregate solar, financial, and ESG metrics across Downtown Phoenix."""
    sql = text("""
        SELECT
            COUNT(*) AS total_buildings,
            ROUND(SUM(system_capacity_kwp) / 1000.0, 2) AS total_capacity_mwp,
            ROUND(SUM(annual_generation_mwh) / 1000.0, 2) AS total_generation_gwh,
            ROUND(SUM(annual_savings_usd) / 1000000.0, 2) AS total_savings_musd,
            ROUND(SUM(co2_offset_tons_yr), 0) AS total_co2_tons,
            ROUND(AVG(shading_loss_pct), 1) AS avg_shading_loss_pct,
            (SELECT COUNT(*) FROM energy_demand_hubs) AS total_demand_hubs
        FROM phoenix_buildings;
    """)
    with engine.connect() as conn:
        row = conn.execute(sql).mappings().first()
    return dict(row)


@app.get("/api/buildings/solar")
def get_buildings_solar(
    hour: int = Query(12, ge=6, le=19, description="Local solar hour (6 to 19)"),
    max_payback: float = Query(25.0, ge=1.0, le=100.0, description="Max payback years filter"),
):
    """Return 3D buildings with live hourly irradiance, sunlit fraction, and financial ROI as GeoJSON."""
    sql = text("""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(json_agg(
                json_build_object(
                    'type', 'Feature',
                    'geometry', ST_AsGeoJSON(b.geom)::json,
                    'properties', json_build_object(
                        'building_id', b.building_id,
                        'osm_id', b.osm_id,
                        'building_type', b.building_type,
                        'footprint_area_sqm', b.footprint_area_sqm,
                        'lidar_height_m', b.lidar_height_m,
                        'roof_slope_deg', b.roof_slope_deg,
                        'roof_aspect_deg', b.roof_aspect_deg,
                        'usable_solar_area_sqm', b.usable_solar_area_sqm,
                        'annual_irradiance_kwh_m2', b.annual_irradiance_kwh_m2,
                        'shading_loss_pct', b.shading_loss_pct,
                        'system_capacity_kwp', b.system_capacity_kwp,
                        'annual_generation_mwh', b.annual_generation_mwh,
                        'estimated_capex_usd', b.estimated_capex_usd,
                        'annual_savings_usd', b.annual_savings_usd,
                        'payback_years', b.payback_years,
                        'co2_offset_tons_yr', b.co2_offset_tons_yr,
                        'sunlit_fraction', h.sunlit_fraction,
                        'irradiance_wm2', h.irradiance_wm2,
                        'generation_kw', h.generation_kw
                    )
                )
            ), '[]'::json)
        ) AS geojson
        FROM phoenix_buildings b
        JOIN building_hourly_solar h
            ON b.building_id = h.building_id
        WHERE h.hour_local = :hour
          AND b.payback_years <= :max_payback;
    """)
    with engine.connect() as conn:
        result = conn.execute(sql, {"hour": hour, "max_payback": max_payback}).scalar()
    if isinstance(result, str):
        return json.loads(result)
    return result


@app.get("/api/shadows/{hour}")
def get_hourly_shadows(hour: int):
    """Return ray-traced street-level urban shadow polygons and solar angles for a given hour."""
    if hour < 6 or hour > 19:
        raise HTTPException(status_code=400, detail="Hour must be between 6 and 19")

    sql = text("""
        SELECT
            hour_local,
            solar_elevation_deg,
            solar_azimuth_deg,
            ST_AsGeoJSON(geom)::json AS geometry
        FROM hourly_shadow_polygons
        WHERE hour_local = :hour
        LIMIT 1;
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"hour": hour}).mappings().first()

    if not row:
        return {
            "hour_local": hour,
            "solar_elevation_deg": 0,
            "solar_azimuth_deg": 0,
            "geojson": {"type": "FeatureCollection", "features": []},
        }

    geom_obj = json.loads(row["geometry"]) if isinstance(row["geometry"], str) else row["geometry"]
    return {
        "hour_local": row["hour_local"],
        "solar_elevation_deg": float(row["solar_elevation_deg"]),
        "solar_azimuth_deg": float(row["solar_azimuth_deg"]),
        "geojson": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": geom_obj, "properties": {"hour": hour}}],
        },
    }


@app.get("/api/energy-arcs")
def get_energy_arcs(hour: int = Query(12, ge=6, le=19)):
    """Compute live PostGIS KNN spatial routing arcs from top solar rooftops to EV & grid demand hubs."""
    sql = text("""
        WITH top_generators AS (
            SELECT
                b.building_id,
                b.building_type,
                b.lidar_height_m,
                ST_X(ST_Centroid(b.geom)) AS src_lon,
                ST_Y(ST_Centroid(b.geom)) AS src_lat,
                b.geom AS src_geom,
                h.generation_kw
            FROM phoenix_buildings b
            JOIN building_hourly_solar h ON b.building_id = h.building_id
            WHERE h.hour_local = :hour
              AND h.generation_kw >= 45.0
            ORDER BY h.generation_kw DESC
            LIMIT 40
        )
        SELECT
            g.building_id,
            g.building_type,
            g.lidar_height_m,
            g.src_lon,
            g.src_lat,
            g.generation_kw,
            hub.hub_name,
            hub.hub_type,
            ST_X(hub.geom) AS dst_lon,
            ST_Y(hub.geom) AS dst_lat,
            ROUND(ST_Distance(g.src_geom::geography, hub.geom::geography)::numeric, 0) AS distance_m
        FROM top_generators g
        CROSS JOIN LATERAL (
            SELECT hub_name, hub_type, geom
            FROM energy_demand_hubs
            ORDER BY g.src_geom <-> geom
            LIMIT 1
        ) hub;
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"hour": hour}).mappings().all()

    return [
        {
            "building_id": r["building_id"],
            "building_type": r["building_type"],
            "source": [float(r["src_lon"]), float(r["src_lat"]), float(r["lidar_height_m"])],
            "target": [float(r["dst_lon"]), float(r["dst_lat"]), 5.0],
            "generation_kw": float(r["generation_kw"]),
            "hub_name": r["hub_name"],
            "hub_type": r["hub_type"],
            "distance_m": float(r["distance_m"]),
        }
        for r in rows
    ]


@app.get("/api/demand-hubs")
def get_demand_hubs():
    """Return EV charging hubs, grid substations, and major civic anchors."""
    sql = text("""
        SELECT
            hub_id,
            hub_name,
            hub_type,
            peak_demand_kw,
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat
        FROM energy_demand_hubs;
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()

    return [
        {
            "hub_id": r["hub_id"],
            "hub_name": r["hub_name"],
            "hub_type": r["hub_type"],
            "peak_demand_kw": float(r["peak_demand_kw"]),
            "coordinates": [float(r["lon"]), float(r["lat"])],
        }
        for r in rows
    ]