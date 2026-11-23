"""Build a static demo from a running, populated Phoenix API.

Run from the repository root:
    python scripts/export_static_demo.py --base-url http://localhost:8000
Preview: python -m http.server 8080 --directory docs
Open http://localhost:8080. Review before committing docs/ or publishing.
The exported results are snapshots, not a live simulation or financial advice.
"""
import argparse
import json
from pathlib import Path
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

SHIM = r'''<script>
(() => {
  const originalFetch = window.fetch.bind(window);
  const cache = new Map();
  window.fetch = async (input, options) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (!url.pathname.startsWith('/api/')) return originalFetch(input, options);
    const hour = Number(url.searchParams.get('hour') || 12);
    let file;
    let payback = null;
    if (url.pathname === '/api/summary') file = 'summary.json';
    else if (url.pathname === '/api/demand-hubs') file = 'demand-hubs.json';
    else if (url.pathname === '/api/buildings/solar') {
      file = `buildings-${hour}.json`;
      payback = Number(url.searchParams.get('max_payback') || 25);
    } else if (/^\/api\/shadows\/\d+$/.test(url.pathname)) {
      file = `shadows-${Number(url.pathname.split('/').pop())}.json`;
    } else if (url.pathname === '/api/energy-arcs') file = `arcs-${hour}.json`;
    else throw new Error(`Unsupported snapshot route: ${url.pathname}`);
    if (!cache.has(file)) {
      cache.set(file, originalFetch(new URL(`data/${file}`, location.href)).then(async response => {
        if (!response.ok) throw new Error(`Missing snapshot: ${file} (${response.status})`);
        return response.json();
      }).catch(error => { cache.delete(file); throw error; }));
    }
    let data = await cache.get(file);
    if (payback !== null) data = { ...data, features: data.features.filter(f =>
      f.properties.payback_years !== null && Number(f.properties.payback_years) <= payback
    ) };
    return new Response(JSON.stringify(data), {headers: {'Content-Type': 'application/json'}});
  };
  window.addEventListener('unhandledrejection', event => {
    let box = document.getElementById('snapshot-error');
    if (!box) {
      box = document.createElement('div');
      box.id = 'snapshot-error';
      box.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:100;background:#7f1d1d;color:white;padding:12px';
      document.body.appendChild(box);
    }
    box.textContent = 'Demo data could not load. Check exported files and serve this page over HTTP.';
    console.error(event.reason);
  });
})();
</script>'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://localhost:8000')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    source = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
    marker = '<script>'
    if source.count(marker) != 1:
        raise RuntimeError('Expected one inline viewer script; review frontend changes first.')
    routes = {
        'summary.json': '/api/summary',
        'demand-hubs.json': '/api/demand-hubs',
    }
    for hour in range(6, 20):
        routes[f'buildings-{hour}.json'] = f'/api/buildings/solar?hour={hour}&max_payback=100'
        routes[f'shadows-{hour}.json'] = f'/api/shadows/{hour}'
        routes[f'arcs-{hour}.json'] = f'/api/energy-arcs?hour={hour}'
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp)
        for name, route in routes.items():
            with urlopen(base + route, timeout=120) as response:
                data = json.load(response)
            if name.startswith('buildings-'):
                if not isinstance(data, dict) or not data.get('features'):
                    raise RuntimeError(f'No buildings in {route}; populate the pipeline first.')
            (staging / name).write_text(json.dumps(data, separators=(',', ':'), allow_nan=False), encoding='utf-8')
            print(f'Exported {name}')
        page = source.replace(marker, SHIM + '\n' + marker, 1)
        page = page.replace('Live PostGIS Spatial Engine', 'Precomputed PostGIS Demo')
        page = page.replace('Live Hourly Irradiance', 'Precomputed Hourly Irradiance')
        page = page.replace('Live Rooftop Solar Irradiance', 'Precomputed Rooftop Solar Irradiance')
        page = page.replace('Live Sunlit Fraction', 'Snapshot Sunlit Fraction')
        page = page.replace('Live Irradiance / Output', 'Snapshot Irradiance / Output')
        page = page.replace('</body>', '<div style="position:fixed;bottom:2px;left:8px;z-index:15;font-size:10px;background:#060913;color:#94a3b8">Precomputed results · Illustrative financial estimates · Not a live API</div>\n</body>')
        out = ROOT / 'docs'
        (out / 'data').mkdir(parents=True, exist_ok=True)
        for name in routes:
            (out / 'data' / name).write_bytes((staging / name).read_bytes())
        (out / 'index.html').write_text(page, encoding='utf-8')
        (out / '.nojekyll').touch()
    print('Built docs/. Preview over HTTP and test all controls before publishing.')

if __name__ == '__main__':
    main()
