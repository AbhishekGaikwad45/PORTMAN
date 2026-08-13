"""WeatherAPI cache for the port location.

One JSONB row in weather_cache, refreshed every TTL_HOURS by a standalone
scheduler installed as a Windows service via NSSM:

    nssm install PortmanWeather "<python.exe>" "d:\\PMS\\DPPL\\PORTMAN\\weather_service.py"
    nssm set PortmanWeather AppDirectory d:\\PMS\\DPPL\\PORTMAN

One-off refresh / smoke test:  python weather_service.py --once

App code reads get_weather(), which returns None once the row is older than
the TTL (stale weather is worse than no weather on an ops screen).
"""
import json
import logging
import sys
import time
from datetime import datetime, timedelta

import requests

from database import get_db, get_cursor

API_KEY = '764a97d2988545ef9e593553251707'
LAT, LON = 18.705186, 73.028097
TTL_HOURS = 6
CACHE_KEY = 'port'
URL = 'https://api.weatherapi.com/v1/forecast.json'
RETRY_SECONDS = 300


def refresh():
    """Fetch from WeatherAPI and upsert the cache row. Returns the payload."""
    r = requests.get(URL, params={'key': API_KEY, 'q': f'{LAT},{LON}',
                                  'days': 3, 'aqi': 'yes', 'alerts': 'yes'},
                     timeout=30)
    r.raise_for_status()
    data = r.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""INSERT INTO weather_cache (cache_key, data, fetched_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (cache_key) DO UPDATE
                     SET data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at""",
                [CACHE_KEY, json.dumps(data), datetime.now()])
    conn.commit()
    conn.close()
    return data


def get_weather():
    """Cached WeatherAPI payload, or None if missing / older than TTL_HOURS."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT data, fetched_at FROM weather_cache WHERE cache_key = %s',
                [CACHE_KEY])
    row = cur.fetchone()
    conn.close()
    if not row or row['fetched_at'] < datetime.now() - timedelta(hours=TTL_HOURS):
        return None
    return row['data']


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(message)s')
    once = '--once' in sys.argv
    while True:
        try:
            data = refresh()
            logging.info('weather cache refreshed: %s',
                         data.get('current', {}).get('condition', {}).get('text'))
            if once:
                assert get_weather() is not None, 'cache row not readable after refresh'
                logging.info('read-back OK')
                return
            time.sleep(TTL_HOURS * 3600)
        except Exception:
            logging.exception('weather refresh failed')
            if once:
                sys.exit(1)
            time.sleep(RETRY_SECONDS)


if __name__ == '__main__':
    main()
