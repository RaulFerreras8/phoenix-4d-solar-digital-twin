CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- Edificios con métricas LiDAR y potencial solar anual
CREATE TABLE IF NOT EXISTS phoenix_buildings (
    building_id SERIAL PRIMARY KEY,
    osm_id BIGINT UNIQUE,
    building_type VARCHAR(60),
    footprint_area_sqm NUMERIC(12, 2),
    lidar_height_m NUMERIC(6, 2),
    roof_slope_deg NUMERIC(5, 2),
    roof_aspect_deg NUMERIC(5, 2),
    usable_solar_area_sqm NUMERIC(12, 2),
    annual_irradiance_kwh_m2 NUMERIC(8, 2),
    shading_loss_pct NUMERIC(5, 2),
    system_capacity_kwp NUMERIC(10, 2),
    annual_generation_mwh NUMERIC(10, 2),
    estimated_capex_usd NUMERIC(12, 2),
    annual_savings_usd NUMERIC(12, 2),
    payback_years NUMERIC(5, 2),
    co2_offset_tons_yr NUMERIC(8, 2),
    geom GEOMETRY(Polygon, 4326)
);

CREATE INDEX idx_phoenix_buildings_geom ON phoenix_buildings USING GIST (geom);

-- Irradiación y sombra dinámica por edificio y hora del día (06:00 a 19:00)
CREATE TABLE IF NOT EXISTS building_hourly_solar (
    id SERIAL PRIMARY KEY,
    building_id INTEGER REFERENCES phoenix_buildings(building_id) ON DELETE CASCADE,
    hour_local INTEGER NOT NULL CHECK (hour_local BETWEEN 0 AND 23),
    sunlit_fraction NUMERIC(5, 4),
    irradiance_wm2 NUMERIC(8, 2),
    generation_kw NUMERIC(10, 2),
    UNIQUE(building_id, hour_local)
);

CREATE INDEX idx_hourly_solar_hour ON building_hourly_solar (hour_local);

-- Estaciones de carga EV / Nodos de demanda energética (para ArcLayer 3D)
CREATE TABLE IF NOT EXISTS energy_demand_hubs (
    hub_id SERIAL PRIMARY KEY,
    osm_id BIGINT UNIQUE,
    hub_name VARCHAR(120),
    hub_type VARCHAR(50),
    peak_demand_kw NUMERIC(10, 2),
    geom GEOMETRY(Point, 4326)
);

CREATE INDEX idx_energy_hubs_geom ON energy_demand_hubs USING GIST (geom);