# Phoenix 4D LiDAR Solar & Shadow Digital Twin

An end-to-end **4D Spatial Intelligence Platform (3D + Time)** engineered to quantify rooftop photovoltaic potential, dynamic urban skyscraper shading losses, and asset-level financial ROI across Downtown Phoenix, Arizona.

Built with a decoupled client-server architecture combining **USGS 3DEP 1m elevation models**, **`pvlib` solar physics**, **3D ray-traced hourly shadows**, **PostgreSQL/PostGIS**, **FastAPI**, and **Deck.gl + MapLibre GL JS**.

---

## Executive Summary & District Simulation Metrics (Downtown Phoenix)

Across a 1.2 km radius in Phoenix's Financial & Civic Core (**2,080 buildings** up to **129.2 m** structural height):

* **Total Installable PV Capacity:** `204.05 MWp`
* **Net Annual Clean Energy Yield:** `376.13 GWh/yr` *(accounting for urban skyscraper shading & desert thermal derating)*
* **Commercial Electricity Savings:** `$54.54M USD/yr` *(at APS blended commercial tariff)*
* **Annual Carbon Offset:** `144,811 metric tons CO₂/yr` *(EPA eGRID WECC Southwest factor)*
* **Microgrid Routing:** Real-time PostGIS KNN spatial pairing (`<->`) linking top solar rooftops to **21 EV charging hubs, grid substations, and civic anchors**.

---

## System Architecture

```text
[USGS 3DEP 1m DEM] + [OpenStreetMap 3D Buildings & EV Hubs]
                           │
                           ▼
        [Python Solar Physics & 3D Ray-Tracing Engine]
           (Rasterio · NumPy · pvlib Ineichen Model)
                           │
                           ▼
        [PostgreSQL 16 + PostGIS 3.4 Spatial Database]
      (2,080 Buildings · 29,120 Hourly Records · Shadows)
                           │
                           ▼
               [FastAPI Live Spatial Backend]
       (/api/summary · /api/buildings/solar · /api/shadows)
                           │
                           ▼
        [4D WebGL Digital Twin (Deck.gl + MapLibre GL)]
   (06:00–19:00 Time-Slider · Dynamic Shadows · ROI Inspector)
```

### Core Engineering Components

1. **Automated Multi-Source Ingestion (`scripts/ingest_phoenix_data.py`):**
   * Streams official **USGS 3DEP 1-meter bare-earth DEM** tiles via The National Map (TNM) API, aligns them in `EPSG:26912` (NAD83 / UTM Zone 12N), and constructs aligned 1m `DTM`, `nDSM`, and `DSM` grids (`2401 × 2401 px`).
   * Extracts 2,080 building footprints and 21 EV/grid demand nodes via fault-tolerant Overpass API mirrors.

2. **Solar Physics & 3D Shadow Ray-Tracing (`scripts/simulate_solar_4d.py`):**
   * Computes exact solar elevation, azimuth, and clear-sky Global Horizontal Irradiance (`GHI`) from `06:00` to `19:00` using **`pvlib`**.
   * Runs vectorized 3D ray-marching across the urban DSM to quantify hourly sunlit fractions per rooftop (`29,120` records) and polygonizes street-level cast shadows (`hourly_shadow_polygons`).
   * Computes asset-level roof slope/aspect, net CAPEX (post-30% Federal ITC), annual savings, simple payback period, and $\text{CO}_2$ abatement.

3. **Live Spatial API & 4D WebGL Interface (`api/main.py` & `frontend/index.html`):**
   * **FastAPI** serves dynamic GeoJSON and spatial KNN routing queries directly from PostGIS with GiST indexing.
   * **Deck.gl + MapLibre GL JS** renders 3D extruded buildings colored by live hourly irradiance ($\text{W/m}^2$) or financial payback years, animated 4D cast shadows, 3D microgrid arcs (`ArcLayer`), and an interactive asset audit panel.

---

## Local Deployment (Docker)

```bash
# 1. Build and start PostgreSQL/PostGIS and FastAPI services
docker compose up -d --build

# 2. Ingest Phoenix 3D buildings, EV hubs, and 1m USGS elevation rasters
docker compose run --rm solar_api python scripts/ingest_phoenix_data.py

# 3. Run pvlib solar physics, 3D shadow ray-tracing, and financial ROI calculations
docker compose run --rm solar_api python scripts/simulate_solar_4d.py

# 4. Access the 4D Digital Twin in your browser
# http://localhost:8000
```