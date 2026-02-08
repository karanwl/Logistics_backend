import requests
from config import settings

coords_str = "-79.393209,43.646057;-79.639268,43.590729"
url = f"https://api.mapbox.com/directions-matrix/v1/mapbox/driving/{coords_str}"
params = {
  "access_token": settings.MAPBOX_TOKEN,
  "annotations": "distance,duration"
}

r = requests.get(url, params=params, timeout=20)
print(r.status_code)
print(r.text)
