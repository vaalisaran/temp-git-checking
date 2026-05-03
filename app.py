from flask import Flask, render_template, request, jsonify, Response
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import json

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────
#  KSO Image Fetcher
#  URL format:
#   https://kso.iiap.res.in/new/static/images/rawimg/
#     {FILTER}/{YEAR}/{MM}/{FILTER}_{YYYYMMDD}T{HHMMSS}_Q{Q}L{L}a128px.jpg
# ──────────────────────────────────────────────────────────────

class KSOImageFetcher:

    BASE_URL   = "https://kso.iiap.res.in/new/static/images/rawimg"
    FILTER_MAP = {'whitelight': 'WL', 'cak': 'CAK', 'halpha': 'HA'}

    # Every 5 min from 05:00 to 13:55 IST
    TIME_SLOTS = [
        f"{h:02d}{m:02d}00"
        for h in range(5, 14)
        for m in range(0, 60, 5)
    ]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Referer':        'https://kso.iiap.res.in/',
            'Accept':         'image/webp,image/jpeg,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })

    # ── build URL ──────────────────────────────────────────────
    def construct_url(self, date, filter_code, time, quality='1', level='0'):
        y, m = date.strftime("%Y"), date.strftime("%m")
        ds   = date.strftime("%Y%m%d")
        fn   = f"{filter_code}_{ds}T{time}_Q{quality}L{level}a128px.jpg"
        return f"{self.BASE_URL}/{filter_code}/{y}/{m}/{fn}", fn

    # ── probe single URL ───────────────────────────────────────
    def _exists(self, url):
        try:
            r = self.session.get(url, headers={'Range': 'bytes=0-511'},
                                 timeout=9, stream=True)
            if r.status_code not in (200, 206):
                return False
            ct = r.headers.get('Content-Type', '')
            if 'image' in ct or 'jpeg' in ct:
                return True
            return int(r.headers.get('Content-Length', 0)) > 2000
        except Exception:
            return False

    # ── single worker ──────────────────────────────────────────
    def _check(self, date, fc, time_slot, quality, level):
        url, fn = self.construct_url(date, fc, time_slot, quality, level)
        if self._exists(url):
            hh, mm, ss = time_slot[:2], time_slot[2:4], time_slot[4:6]
            label = f"{hh}:{mm}:{ss} IST" + (f" (Q{quality})" if quality != '1' else '')
            return {
                'url':       url,
                'filename':  fn,
                'time':      label,
                'time_sort': time_slot,
                'proxy_url': f'/proxy?url={url}',
            }
        return None

    # ── all images for one (date, filter) ─────────────────────
    def get_images(self, date_str, filter_type, level='0'):
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return []

        fc   = self.FILTER_MAP.get(filter_type, 'WL')
        work = [(t, '1') for t in self.TIME_SLOTS]
        for t in self.TIME_SLOTS[:15]:           # extra qualities early day
            work += [(t, '2'), (t, '3')]

        found = []
        with ThreadPoolExecutor(max_workers=30) as pool:
            futures = {pool.submit(self._check, date, fc, t, q, level): (t, q)
                       for t, q in work}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    found.append(r)

        found.sort(key=lambda x: x['time_sort'])
        return found

    def download(self, url):
        try:
            r = self.session.get(url, timeout=30)
            if r.status_code == 200:
                return r.content
        except Exception as e:
            app.logger.error(f"Download error: {e}")
        return None


fetcher = KSOImageFetcher()


# ══════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


# ── SSE streaming endpoint ─────────────────────────────────
@app.route('/search_stream')
def search_stream():
    """
    Server-Sent Events. For each (date, filter) combination emits:
      data: {"date":..., "filter":..., "images":[...], "count":N}
    Final event:
      data: {"done": true, "total": N}
    """
    start_date = request.args.get('start_date', '')
    end_date   = request.args.get('end_date', '') or start_date
    level      = request.args.get('level', '0')
    filters    = request.args.getlist('filters') or ['whitelight', 'cak', 'halpha']

    def error_stream(msg):
        yield f"data: {json.dumps({'error': msg})}\n\n"

    if not start_date:
        return Response(error_stream("Date required"), mimetype='text/event-stream')

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt   = datetime.strptime(end_date,   "%Y-%m-%d")
    except ValueError:
        return Response(error_stream("Invalid date format"), mimetype='text/event-stream')

    if end_dt < start_dt:
        end_dt = start_dt

    num_days = (end_dt - start_dt).days + 1
    dates = [
        (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(num_days)
    ]

    def generate():
        total = 0
        for d in dates:
            for ft in filters:
                images = fetcher.get_images(d, ft, level)
                total += len(images)
                payload = {
                    'date':   d,
                    'filter': ft,
                    'images': images,
                    'count':  len(images),
                }
                yield f"data: {json.dumps(payload)}\n\n"
        yield f"data: {json.dumps({'done': True, 'total': total})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ── Proxy ──────────────────────────────────────────────────
@app.route('/proxy')
def proxy_image():
    url = request.args.get('url', '')
    if not url.startswith('https://kso.iiap.res.in'):
        return "Invalid URL", 400
    data = fetcher.download(url)
    if data:
        return Response(data, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    return "Image not found", 404


# ── Download ───────────────────────────────────────────────
@app.route('/download')
def download_image():
    url      = request.args.get('url', '')
    filename = request.args.get('filename', 'solar_image.jpg')
    if not url.startswith('https://kso.iiap.res.in'):
        return "Invalid URL", 400
    data = fetcher.download(url)
    if data:
        return Response(data, mimetype='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename={filename}'})
    return "Image not found", 404


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)