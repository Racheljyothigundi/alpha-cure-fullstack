"""controllers/hospital_controller.py"""

import math
import os
import requests
from flask import request, jsonify
from utils.jwt_utils import token_required

PLACES_NEARBY_URL = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
OVERPASS_URLS = (
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
)


def _haversine_km(lat1, lng1, lat2, lng2):
    r = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _address_from_osm_tags(tags):
    """Build a readable address from common OpenStreetMap address tags."""
    full = tags.get('addr:full')
    if full:
        return full

    parts = [
        tags.get('addr:housenumber'),
        tags.get('addr:street'),
        tags.get('addr:suburb'),
        tags.get('addr:city') or tags.get('addr:town') or tags.get('addr:village'),
    ]
    return ', '.join(part for part in parts if part) or 'Address unavailable'


def _openstreetmap_places(user_lat, user_lng, query_type):
    """Find nearby facilities without requiring a Google billing account."""
    if query_type == 'hospital':
        selector = f'nwr(around:10000,{user_lat},{user_lng})[amenity=hospital];'
    else:
        selector = (
            f'nwr(around:10000,{user_lat},{user_lng})[healthcare=vaccination];'
            f'nwr(around:10000,{user_lat},{user_lng})[amenity=clinic];'
        )

    query = f'[out:json][timeout:15];({selector});out center tags;'
    last_error = None

    for endpoint in OVERPASS_URLS:
        try:
            response = requests.post(
                endpoint,
                data={'data': query},
                headers={'User-Agent': 'Alpha-Cure/1.0 (nearby-facilities)'},
                timeout=20,
            )
            response.raise_for_status()
            elements = response.json().get('elements', [])
            places = []

            for element in elements:
                center = element.get('center', {})
                place_lat = element.get('lat', center.get('lat'))
                place_lng = element.get('lon', center.get('lon'))
                if place_lat is None or place_lng is None:
                    continue

                tags = element.get('tags', {})
                places.append({
                    'name': tags.get('name') or tags.get('official_name') or 'Healthcare facility',
                    'address': _address_from_osm_tags(tags),
                    'rating': None,
                    'lat': place_lat,
                    'lng': place_lng,
                    'place_id': f"osm:{element.get('type')}:{element.get('id')}",
                    'open_now': None,
                    'distance_km': round(_haversine_km(user_lat, user_lng, place_lat, place_lng), 1),
                })

            places.sort(key=lambda place: place['distance_km'])
            return places[:10]
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f'OpenStreetMap search failed: {last_error}')


@token_required
def get_nearby_hospitals():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    query_type = request.args.get('type', 'hospital')  # hospital or vaccination

    if not lat or not lng:
        return jsonify({'error': 'lat and lng parameters required'}), 400

    try:
        user_lat = float(lat)
        user_lng = float(lng)
    except ValueError:
        return jsonify({'error': 'lat and lng must be valid numbers'}), 400

    api_key = os.getenv('GOOGLE_MAPS_API_KEY')
    if not api_key or api_key == 'your_google_maps_key_here':
        try:
            return jsonify({'places': _openstreetmap_places(user_lat, user_lng, query_type), 'source': 'openstreetmap'}), 200
        except Exception as exc:
            print(f'[Nearby] OpenStreetMap fallback failed: {exc}')
            return jsonify({
                'error': 'Nearby search is temporarily unavailable. Please try again shortly.',
                'code': 'MAPS_PROVIDER_UNAVAILABLE',
            }), 503

    keyword = (
        'cancer hospital oncology'
        if query_type == 'hospital'
        else 'vaccination center immunization'
    )
    params = {
        'location': f'{lat},{lng}',
        'radius': 10000,
        'keyword': keyword,
        'key': api_key,
    }
    if query_type == 'hospital':
        params['type'] = 'hospital'

    try:
        resp = requests.get(PLACES_NEARBY_URL, params=params, timeout=10)
        data = resp.json()
        status = data.get('status')
        if status != 'OK':
            try:
                return jsonify({'places': _openstreetmap_places(user_lat, user_lng, query_type), 'source': 'openstreetmap'}), 200
            except Exception as exc:
                print(f'[Nearby] Google status {status}; OpenStreetMap fallback failed: {exc}')
                return jsonify({
                    'error': 'Nearby search is temporarily unavailable. Please try again shortly.',
                    'code': 'MAPS_PROVIDER_UNAVAILABLE',
                }), 503

        places = []
        for p in data.get('results', []):
            place_lat = p['geometry']['location']['lat']
            place_lng = p['geometry']['location']['lng']
            places.append({
                'name': p.get('name'),
                'address': p.get('vicinity'),
                'rating': p.get('rating'),
                'lat': place_lat,
                'lng': place_lng,
                'place_id': p.get('place_id'),
                'open_now': p.get('opening_hours', {}).get('open_now'),
                'distance_km': round(_haversine_km(user_lat, user_lng, place_lat, place_lng), 1),
            })

        places.sort(key=lambda x: x['distance_km'])
        return jsonify({'places': places[:10], 'source': 'google'}), 200
    except Exception as exc:
        try:
            return jsonify({'places': _openstreetmap_places(user_lat, user_lng, query_type), 'source': 'openstreetmap'}), 200
        except Exception as fallback_exc:
            print(f'[Nearby] Google request failed: {exc}; OpenStreetMap fallback failed: {fallback_exc}')
            return jsonify({
                'error': 'Nearby search is temporarily unavailable. Please try again shortly.',
                'code': 'MAPS_PROVIDER_UNAVAILABLE',
            }), 503
