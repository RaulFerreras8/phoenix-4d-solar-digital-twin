import os
import logging
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import rasterize, shapes
from rasterio.transform import Affine
from shapely.geometry import shape, MultiPolygon, Polygon
from sqlalchemy import create_engine, text
import pvlib

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

PHOENIX_LAT = 33.4484
PHOENIX_LON = -112.0740
PHOENIX_ALT = 331.0
PHOENIX_TZ = "America/Phoenix"
METRIC_CRS = "EPSG:26912"
DATA_DIR = "/app/data"

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://admin_gis:solar_twin_secure@postgis_solar_db:5432/phoenix_solar_db",
)

# Commercial Solar & Financial Engineering Parameters (Arizona / APS Market)
ANNUAL_POA_BASE_KWH_M2 = 2240.0   # High desert solar resource in Phoenix, AZ
PANEL_EFFICIENCY_KW_M2 = 0.215    # 215 Wp/m2 Tier-1 monocrystalline modules
SYSTEM_PERFORMANCE_RATIO = 0.84   # Includes Phoenix thermal derating & inverter losses
NET_CAPEX_USD_PER_KWP = 1480.0    # Commercial turnkey cost net of 30% Federal ITC
COMMERCIAL_TARIFF_USD_KWH = 0.145 # APS commercial blended electricity rate
EPA_CO2_TONS_PER_MWH = 0.385      # EPA eGRID WECC Southwest carbon intensity


def ensure_shadow_table(engine):
    """Create table for hourly cast urban shadow polygons if it does not exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS hourly_shadow_polygons (
        shadow_id SERIAL PRIMARY KEY,
        hour_local INTEGER NOT NULL CHECK (hour_local BETWEEN 0 AND 23),
        solar_elevation_deg NUMERIC(5, 2),
        solar_azimuth_deg NUMERIC(6, 2),
        geom GEOMETRY(MultiPolygon, 4326)
    );
    CREATE INDEX IF NOT EXISTS idx_hourly_shadows_hour ON hourly_shadow_polygons (hour_local);
    CREATE INDEX IF NOT EXISTS idx_hourly_shadows_geom ON hourly_shadow_polygons USING GIST (geom);
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


def block_max_downsample(arr: np.ndarray, factor: int = 2) -> np.ndarray:
    """Downsample 2D elevation grid preserving maximum building heights."""
    h, w = arr.shape
    h_crop = (h // factor) * factor
    w_crop = (w // factor) * factor
    cropped = arr[:h_crop, :w_crop]
    return cropped.reshape(h_crop // factor, factor, w_crop // factor, factor).max(axis=(1, 3))


def trace_solar_shadows(dsm: np.ndarray, res_m: float, elevation_deg: float, azimuth_deg: float) -> np.ndarray:
    """Vectorized 3D ray-tracing across DSM grid returning boolean shadow mask (True = shaded)."""
    if elevation_deg <= 2.0:
        return np.ones_like(dsm, dtype=bool)

    el_rad = np.deg2rad(elevation_deg)
    az_rad = np.deg2rad(azimuth_deg)
    tan_el = np.tan(el_rad)

    dcol_per_m = np.sin(az_rad) / res_m
    drow_per_m = -np.cos(az_rad) / res_m

    rows, cols = dsm.shape
    shadow_mask = np.zeros((rows, cols), dtype=bool)

    distances = np.concatenate([
        np.arange(2.0, 60.0, 2.0),
        np.arange(60.0, 180.0, 4.0),
        np.arange(180.0, 345.0, 8.0),
    ])

    for dist in distances:
        dc = int(np.round(dist * dcol_per_m))
        dr = int(np.round(dist * drow_per_m))
        if dc == 0 and dr == 0:
            continue

        z_offset = dist * tan_el + 0.5

        src_r0 = max(0, dr)
        src_r1 = min(rows, rows + dr)
        src_c0 = max(0, dc)
        src_c1 = min(cols, cols + dc)

        dst_r0 = src_r0 - dr
        dst_r1 = src_r1 - dr
        dst_c0 = src_c0 - dc
        dst_c1 = src_c1 - dc

        if src_r1 <= src_r0 or src_c1 <= src_c0:
            continue

        blocker_height = dsm[src_r0:src_r1, src_c0:src_c1]
        ray_height = dsm[dst_r0:dst_r1, dst_c0:dst_c1] + z_offset
        shadow_mask[dst_r0:dst_r1, dst_c0:dst_c1] |= (blocker_height > ray_height)

    return shadow_mask


def run_solar_4d_simulation(engine):
    ensure_shadow_table(engine)

    dsm_path = os.path.join(DATA_DIR, "phoenix_dsm_1m.tif")
    ndsm_path = os.path.join(DATA_DIR, "phoenix_ndsm_1m.tif")

    logging.info("Loading 1m elevation rasters (DSM & nDSM)...")
    with rasterio.open(dsm_path) as src_dsm:
        dsm_1m = src_dsm.read(1).astype(np.float32)
        transform_1m = src_dsm.transform

    with rasterio.open(ndsm_path) as src_ndsm:
        ndsm_1m = src_ndsm.read(1).astype(np.float32)

    gy, gx = np.gradient(dsm_1m, 1.0, 1.0)
    slope_1m = np.rad2deg(np.arctan(np.sqrt(gx**2 + gy**2)))
    aspect_1m = (np.rad2deg(np.arctan2(-gx, gy)) + 360.0) % 360.0

    factor = 2
    res_m = 2.0
    dsm_2m = block_max_downsample(dsm_1m, factor)
    ndsm_2m = block_max_downsample(ndsm_1m, factor)
    h2, w2 = dsm_2m.shape
    transform_2m = transform_1m * Affine.scale(factor, factor)

    logging.info("Loading 2,080 Phoenix buildings from PostGIS...")
    gdf_bldgs = gpd.read_postgis(
        "SELECT building_id, building_type, footprint_area_sqm, lidar_height_m, geom FROM phoenix_buildings ORDER BY building_id;",
        con=engine,
        geom_col="geom",
        crs="EPSG:4326",
    )
    gdf_bldgs_m = gdf_bldgs.to_crs(METRIC_CRS)

    bldg_shapes = [(geom, int(bid)) for geom, bid in zip(gdf_bldgs_m.geometry, gdf_bldgs_m["building_id"])]
    bldg_grid_2m = rasterize(
        shapes=bldg_shapes,
        out_shape=(h2, w2),
        transform=transform_2m,
        fill=0,
        dtype=np.int32,
        all_touched=True,
    )

    bldg_grid_1m = rasterize(
        shapes=bldg_shapes,
        out_shape=dsm_1m.shape,
        transform=transform_1m,
        fill=0,
        dtype=np.int32,
        all_touched=False,
    )

    max_bid = int(gdf_bldgs["building_id"].max())
    bldg_grid_2m_flat = bldg_grid_2m.ravel()
    pixel_counts_2m = np.bincount(bldg_grid_2m_flat, minlength=max_bid + 1)

    interior_mask = (bldg_grid_1m > 0) & (slope_1m <= 35.0)
    roof_px_1m = np.bincount(bldg_grid_1m[interior_mask], minlength=max_bid + 1)
    slope_sum = np.bincount(bldg_grid_1m[interior_mask], weights=slope_1m[interior_mask], minlength=max_bid + 1)
    aspect_sin = np.bincount(bldg_grid_1m[interior_mask], weights=np.sin(np.deg2rad(aspect_1m[interior_mask])), minlength=max_bid + 1)
    aspect_cos = np.bincount(bldg_grid_1m[interior_mask], weights=np.cos(np.deg2rad(aspect_1m[interior_mask])), minlength=max_bid + 1)

    avg_slope = np.where(roof_px_1m > 0, slope_sum / np.maximum(roof_px_1m, 1), 5.0)
    avg_aspect = (np.rad2deg(np.arctan2(aspect_sin, aspect_cos)) + 360.0) % 360.0
    avg_aspect = np.where(roof_px_1m > 0, avg_aspect, 180.0)

    logging.info("Computing solar trajectory and clear-sky irradiance with pvlib (06:00 - 19:00)...")
    site = pvlib.location.Location(latitude=PHOENIX_LAT, longitude=PHOENIX_LON, tz=PHOENIX_TZ, altitude=PHOENIX_ALT)
    times = pd.date_range("2026-06-21 06:00:00", "2026-06-21 19:00:00", freq="1h", tz=PHOENIX_TZ)
    solpos = site.get_solarposition(times)
    clearsky = site.get_clearsky(times, model="ineichen")

    usable_ratio = np.where(gdf_bldgs["footprint_area_sqm"].values >= 500.0, 0.65, 0.52)
    usable_area = np.round(gdf_bldgs["footprint_area_sqm"].values * usable_ratio, 2)
    capacity_kwp = np.round(usable_area * PANEL_EFFICIENCY_KW_M2, 2)

    bldg_ids = gdf_bldgs["building_id"].values
    hourly_records = []
    shadow_poly_records = []

    weighted_sunlit_sum = np.zeros(len(gdf_bldgs), dtype=np.float64)
    total_ghi_weight = 0.0

    for idx, ts in enumerate(times):
        hour_local = int(ts.hour)
        el_deg = float(solpos["apparent_elevation"].iloc[idx])
        az_deg = float(solpos["azimuth"].iloc[idx])
        ghi_wm2 = float(max(clearsky["ghi"].iloc[idx], 0.0))

        logging.info(f"Ray-tracing 3D shadows for {hour_local:02d}:00 (Sun El: {el_deg:.1f}°, Az: {az_deg:.1f}°, GHI: {ghi_wm2:.0f} W/m²)...")
        shadow_mask = trace_solar_shadows(dsm_2m, res_m, el_deg, az_deg)

        sunlit_px = np.bincount(
            bldg_grid_2m_flat,
            weights=(~shadow_mask).ravel().astype(np.float32),
            minlength=max_bid + 1,
        )
        sunlit_frac_all = np.where(
            pixel_counts_2m > 0,
            sunlit_px / np.maximum(pixel_counts_2m, 1),
            1.0,
        )
        sunlit_frac = np.clip(sunlit_frac_all[bldg_ids], 0.0, 1.0)

        effective_wm2 = ghi_wm2 * (0.85 * sunlit_frac + 0.15) if ghi_wm2 > 0 else np.zeros_like(sunlit_frac)
        gen_kw = capacity_kwp * (effective_wm2 / 1000.0) * SYSTEM_PERFORMANCE_RATIO

        weighted_sunlit_sum += sunlit_frac * ghi_wm2
        total_ghi_weight += ghi_wm2

        for b_idx, bid in enumerate(bldg_ids):
            hourly_records.append(
                (
                    int(bid),
                    hour_local,
                    round(float(sunlit_frac[b_idx]), 4),
                    round(float(effective_wm2[b_idx]), 2),
                    round(float(gen_kw[b_idx]), 2),
                )
            )

        street_shadow = (shadow_mask & (ndsm_2m < 3.0)).astype(np.uint8)
        geoms = [
            shape(g).simplify(2.0, preserve_topology=True)
            for g, val in shapes(street_shadow, mask=(street_shadow == 1), transform=transform_2m)
            if val == 1
        ]
        geoms = [g for g in geoms if not g.is_empty and g.area >= 25.0]
        if geoms:
            gs_m = gpd.GeoSeries(geoms, crs=METRIC_CRS)
            gs_4326 = gs_m.to_crs("EPSG:4326")
            polys = []
            for g in gs_4326:
                if isinstance(g, Polygon):
                    polys.append(g)
                elif isinstance(g, MultiPolygon):
                    polys.extend(list(g.geoms))
            if polys:
                shadow_poly_records.append(
                    {
                        "hour_local": hour_local,
                        "solar_elevation_deg": round(el_deg, 2),
                        "solar_azimuth_deg": round(az_deg, 2),
                        "geom": MultiPolygon(polys),
                    }
                )

    # Calculate annual solar & financial KPIs per building (1 kWh/m2 = 1 Peak Sun Hour)
    avg_sunlit = weighted_sunlit_sum / max(total_ghi_weight, 1.0)
    shading_loss_pct = np.round((1.0 - avg_sunlit) * 100.0, 2)
    annual_irr = np.round(ANNUAL_POA_BASE_KWH_M2 * (1.0 - shading_loss_pct / 100.0), 2)
    annual_gen_mwh = np.round((capacity_kwp * annual_irr * SYSTEM_PERFORMANCE_RATIO) / 1000.0, 2)
    capex_usd = np.round(capacity_kwp * NET_CAPEX_USD_PER_KWP, 2)
    annual_savings_usd = np.round(annual_gen_mwh * 1000.0 * COMMERCIAL_TARIFF_USD_KWH, 2)
    raw_payback = np.where(annual_savings_usd > 0, capex_usd / np.maximum(annual_savings_usd, 1e-6), 99.0)
    payback_years = np.round(np.clip(raw_payback, 0.0, 99.99), 2)
    co2_tons_yr = np.round(annual_gen_mwh * EPA_CO2_TONS_PER_MWH, 2)

    logging.info("Updating 2,080 building solar & financial KPIs in PostGIS...")
    update_rows = []
    for i, bid in enumerate(bldg_ids):
        update_rows.append(
            {
                "bid": int(bid),
                "slope": round(float(avg_slope[bid]), 2),
                "aspect": round(float(avg_aspect[bid]), 2),
                "usable": float(usable_area[i]),
                "irr": float(annual_irr[i]),
                "shade": float(shading_loss_pct[i]),
                "kwp": float(capacity_kwp[i]),
                "mwh": float(annual_gen_mwh[i]),
                "capex": float(capex_usd[i]),
                "savings": float(annual_savings_usd[i]),
                "payback": float(payback_years[i]),
                "co2": float(co2_tons_yr[i]),
            }
        )

    update_sql = text("""
        UPDATE phoenix_buildings SET
            roof_slope_deg = :slope,
            roof_aspect_deg = :aspect,
            usable_solar_area_sqm = :usable,
            annual_irradiance_kwh_m2 = :irr,
            shading_loss_pct = :shade,
            system_capacity_kwp = :kwp,
            annual_generation_mwh = :mwh,
            estimated_capex_usd = :capex,
            annual_savings_usd = :savings,
            payback_years = :payback,
            co2_offset_tons_yr = :co2
        WHERE building_id = :bid;
    """)

    with engine.begin() as conn:
        conn.execute(update_sql, update_rows)

    logging.info(f"Bulk-inserting {len(hourly_records)} hourly solar records into 'building_hourly_solar'...")
    raw_conn = engine.raw_connection()
    try:
        with raw_conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE building_hourly_solar RESTART IDENTITY;")
            args_str = ",".join(
                cur.mogrify("(%s,%s,%s,%s,%s)", rec).decode("utf-8") for rec in hourly_records
            )
            cur.execute(
                "INSERT INTO building_hourly_solar (building_id, hour_local, sunlit_fraction, irradiance_wm2, generation_kw) VALUES "
                + args_str
            )
        raw_conn.commit()
    finally:
        raw_conn.close()

    if shadow_poly_records:
        gdf_shadows = gpd.GeoDataFrame(shadow_poly_records, geometry="geom", crs="EPSG:4326")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE hourly_shadow_polygons RESTART IDENTITY;"))
        gdf_shadows.to_postgis("hourly_shadow_polygons", engine, if_exists="append", index=False)
        logging.info(f"Ingested {len(gdf_shadows)} hourly street-shadow polygon layers into 'hourly_shadow_polygons'.")

    total_mwp = capacity_kwp.sum() / 1000.0
    total_gwh = annual_gen_mwh.sum() / 1000.0
    total_savings_m = annual_savings_usd.sum() / 1_000_000.0
    total_co2 = co2_tons_yr.sum()
    logging.info(
        f"Phase 3 completed! District Solar Potential: {total_mwp:.2f} MWp | "
        f"{total_gwh:.2f} GWh/yr | ${total_savings_m:.2f}M/yr savings | {total_co2:,.0f} tons CO2/yr offset."
    )


if __name__ == "__main__":
    engine = create_engine(DB_URL)
    run_solar_4d_simulation(engine)