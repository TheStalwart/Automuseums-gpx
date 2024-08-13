import argparse
import datetime
from functools import reduce
import glob
import json
import math
import os
import pathlib
import random
import sys
import time
import requests
from bs4 import BeautifulSoup
import rich
import gpxpy
import gpxpy.gpx

# Define source URL
WEBSITE_ROOT_URL = 'https://automuseums.info'

# Define file paths
PROJECT_ROOT = pathlib.Path(__file__).parent.resolve()

# Define cache properties
CACHE_ROOT = os.path.join(PROJECT_ROOT, "cache")
COUNTRY_CACHE_MAX_AGE_MINUTES = 55
INDEX_CACHE_MAX_AGE_HOURS = 24
MUSEUM_CACHE_MAX_AGE_HOURS = 48

# Define output properties
OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "output")

def load_countries():
    cache_file_path = os.path.join(CACHE_ROOT, 'homepage.html')

    def download_homepage():
        print("Downloading country list...")
        r = requests.get(f"{WEBSITE_ROOT_URL}/homepage")
        homepage_contents = r.text

        with open(cache_file_path, "w") as f:
            f.write(homepage_contents)

        return homepage_contents
    
    html_contents = ''
    if not os.path.isfile(cache_file_path):
        html_contents = download_homepage()
    else:
        cache_file_modification_timestamp = os.path.getmtime(cache_file_path)
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
        cache_file_age_minutes = math.floor(cache_file_age_seconds / 60)
        print(f"Country cache file is {cache_file_age_minutes} minutes old")

        if cache_file_age_minutes < COUNTRY_CACHE_MAX_AGE_MINUTES:
            print("Loading cached country list...")
            with open(cache_file_path, 'r') as f:
                html_contents = f.read()
        else:
            html_contents = download_homepage()

    # Parse homepage HTML
    soup = BeautifulSoup(html_contents, 'html.parser')
    countries = soup.find(id='block-searchmuseumsin').find_all('a') # https://beautiful-soup-4.readthedocs.io/en/latest/#navigating-the-tree

    def define_country_properties(a_tag):
        name = a_tag.contents[0].strip()
        return { 'name': name, 'relative_url': a_tag['href'], 'absolute_url': f"{WEBSITE_ROOT_URL}{a_tag['href']}" }

    property_list = list(map(define_country_properties, countries))

    return property_list

def download_country_index(selected_country):
    cache_country_root_path = os.path.join(CACHE_ROOT, 'countries')
    if not os.path.isdir(cache_country_root_path):
        os.mkdir(cache_country_root_path)

    cache_country_path = os.path.join(cache_country_root_path, selected_country['name'])
    if not os.path.isdir(cache_country_path):
        os.mkdir(cache_country_path)

    selected_country['cache_path'] = cache_country_path

    def format_return_value(index):
        return { 'country': selected_country, 'museums': index }

    def download_index():
        print(f"Downloading {selected_country['name']}...")
        index_pages = []

        # Delete old cache
        for old_cache_file in glob.glob(os.path.join(cache_country_path, "[0-9]*.html")):
            print(f"Deleting old cache file: {old_cache_file}")
            os.remove(old_cache_file)

        # Redownload country's index of museums
        museum_list_url = f"{WEBSITE_ROOT_URL}{selected_country['relative_url']}"
        for page_index in range(100): # make sure we never get stuck in infinite loop
            cached_file_name = f"{page_index}.html".rjust(7, '0') # make all page numbers double-digits for easier sorting when loading cache
            cached_page_path = os.path.join(cache_country_path, cached_file_name)
            r = requests.get(museum_list_url, params={'page': page_index})
            print(f"Downloaded {r.url}")
            page_contents = r.text

            with open(cached_page_path, "w") as f:
                f.write(page_contents)

            soup = BeautifulSoup(page_contents, 'html.parser')

            index_pages.append(soup)

            if not soup.find(title='Go to next page'):
                print(f"Link to next page not found, bailing out")
                break

        return index_pages

    cache_file_path = os.path.join(cache_country_path, "00.html")
    if not os.path.isfile(cache_file_path):
        return format_return_value(parse_country_index(download_index()))
    else:
        cache_file_modification_timestamp = os.path.getmtime(cache_file_path)
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
        cache_file_age_hours = math.floor(cache_file_age_seconds / 60 / 60)
        print(f"{selected_country['name']} index cache is {cache_file_age_hours} hours old")

        if cache_file_age_hours < INDEX_CACHE_MAX_AGE_HOURS:
            print("Loading cached index...")
            index_pages = []
            
            sorted_cache_file_path_array = sorted(glob.glob(os.path.join(cache_country_path, "[0-9]*.html")))
            for cache_file_path in sorted_cache_file_path_array:
                print(f"Loading contents of {cache_file_path}...")
                with open(cache_file_path, 'r') as f:
                    html_contents = f.read()
                    soup = BeautifulSoup(html_contents, 'html.parser')

                    index_pages.append(soup)
            
            return format_return_value(parse_country_index(index_pages))
        else:
            return format_return_value(parse_country_index(download_index()))
        
def parse_country_index(pages):
    museums = []

    for page in pages:
        museum_blocks = page.find_all(class_='node-readmore')

        def define_museum_properties(li_tag):
            a_tag = li_tag.find('a')
            name = a_tag['title'].strip()
            return { 'name': name, 'relative_url': a_tag['href'], 'absolute_url': f"{WEBSITE_ROOT_URL}{a_tag['href']}" }

        museums.extend(list(map(define_museum_properties, museum_blocks)))

    # Deduplicate entries, 
    # because museum list pages return dupes of museums that have multiple locations.
    # e.g. the following museum https://automuseums.info/czech-republic/museum-historical-motorcycles
    # is listed 3x times on https://automuseums.info/museums/Czechia?page=4

    unique_museums = reduce(lambda l, x: l.append(x) or l if x not in l else l, museums, []) # https://stackoverflow.com/a/37163210

    return unique_museums

def load_museum_page(country, museum_properties):
    cache_museum_root_path = os.path.join(country['cache_path'], 'museums')
    if not os.path.isdir(cache_museum_root_path):
        os.mkdir(cache_museum_root_path)

    # Museum page URLs encountered during debugging:
    # https://automuseums.info/czechia/automoto-museum-lucany
    # https://automuseums.info/czech-republic/museum-eastern-bloc-vehicles-%C5%BEelezn%C3%BD-brod
    # https://automuseums.info/index.php/czechia/historic-car-museum-kuks
    # https://automuseums.info/index.php/czech-republic/fire-brigade-museum-p%C5%99ibyslav

    # Also, some entries are listed multiple times on country index page,
    # e.g. https://automuseums.info/czech-republic/museum-historical-motorcycles 
    # is listed 3x times on https://automuseums.info/museums/Czechia?page=4 as of Aug 11th 2024,
    # all 3x entries have the same page link, but that page lists 3x locations.
    # This needs to be exported as 3x different placemarks in GPX file.

    # A few days after that code was written,
    # i discovered every museum page has data-history-node-id, 
    # and museum pages can be loaded by /node/ID URLs, e.g. https://automuseums.info/node/1893

    name_slug = museum_properties['relative_url'].split('/')[-1] # always use last slug because there could be "/index.php/" in the middle
    sanitized_file_basename = "".join([x if x.isalnum() else "_" for x in name_slug]) # sanitize https://stackoverflow.com/a/295152
    cache_file_path = os.path.join(cache_museum_root_path, f"{sanitized_file_basename}.html")

    def download_page():
        r = requests.get(f"{WEBSITE_ROOT_URL}{museum_properties['relative_url']}")
        print(f"Downloaded {r.url}")
        page_contents = r.text

        with open(cache_file_path, "w") as f:
            f.write(page_contents)

        return BeautifulSoup(page_contents, 'html.parser')

    if not os.path.isfile(cache_file_path):
        return download_page(), cache_file_path
    else:
        cache_file_modification_timestamp = os.path.getmtime(cache_file_path)
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
        cache_file_age_hours = math.floor(cache_file_age_seconds / 60 / 60)

        if cache_file_age_hours < MUSEUM_CACHE_MAX_AGE_HOURS:
            print(f"Loading {cache_file_age_hours} hours old cached museum page for {museum_properties['name']}...")
            with open(cache_file_path, 'r') as f:
                html_contents = f.read()
                return BeautifulSoup(html_contents, 'html.parser'), cache_file_path
        else:
            return download_page(), cache_file_path
        
def parse_museum_page(page):
    museum_description = ''
    body_div = page.find(class_='node-content').find(class_='field--name-body')
    if body_div: # https://automuseums.info/estonia/estonian-museum-old-technology - no description <div>
        # for some museums, description is wrapped in extra <p> tag
        # https://automuseums.info/barbados/mallalieu-motor-collection - has two children <p> tags
        # https://automuseums.info/jordan/royal-automobile-museum - field--name-body value is enclosed in double-quotes
        museum_description = "\n".join(map(str, list(body_div.children)))

    drupal_node_id = page.find('article')['data-history-node-id']

    data_json = page.find(attrs={"data-drupal-selector": "drupal-settings-json"}).contents[0]
    data = json.loads(data_json)
    leaflet_features = data['leaflet'][f"leaflet-map-node-museum-{drupal_node_id}-coordinates"]['features']
    leaflet_points = list(filter(lambda f: f['type'] == 'point', leaflet_features))
    coordinates = list(map(lambda p: { 'lat': p['lat'], 'lon': p['lon'] }, leaflet_points))

    return { 'description': museum_description, 'drupal_node_id': drupal_node_id, 'coordinates': coordinates }

# Ensure CACHE_ROOT exists
if not os.path.isdir(CACHE_ROOT):
    os.mkdir(CACHE_ROOT)

# Refresh country list
countries = load_countries()
# rich.print(countries)

# Build ArgumentParser https://docs.python.org/3/library/argparse.html
readable_country_list = ', '.join(map(lambda country: country['name'], countries))
arg_parser = argparse.ArgumentParser(epilog=f"Available countries: {readable_country_list}")
arg_parser.add_argument('--country', help='Limit scrape to one country')
arg_parser.add_argument('--lowprofile', action='store_true')
args = arg_parser.parse_args()

country_indexes = []

if args.country:
    country_search_results = list(filter(lambda c: c['name'] == args.country, countries))
    if len(country_search_results) < 1:
        sys.exit(f"Country \"{args.country}\" not found.\n\nTry any of these: {readable_country_list}")

    selected_country = country_search_results[0]
    country_indexes.append(download_country_index(selected_country))
else:
    if args.lowprofile:
        print('Keeping low profile, updating one random country index...')
        selected_country = random.choice(countries)
        # In the future, i want this mode to update country index with oldest/absent cache
        country_indexes.append(download_country_index(selected_country))
    else:
        print('Updating all country indexes...')
        for selected_country in countries:
            country_indexes.append(download_country_index(selected_country))

for country in country_indexes:
    for museum_properties in country['museums']:
        page, cache_file_path = load_museum_page(country['country'], museum_properties)
        museum_properties['cache_file_path'] = cache_file_path
        museum_properties.update(parse_museum_page(page))

rich.print(country_indexes)

# Ensure OUTPUT_ROOT exists
if not os.path.isdir(OUTPUT_ROOT):
    os.mkdir(OUTPUT_ROOT)

# Generate per-country GPX files
# https://github.com/tkrajina/gpxpy/blob/dev/examples/waypoints_example.py
for country in country_indexes:
    gpx = gpxpy.gpx.GPX()
    gpx.creator = 'https://github.com/TheStalwart/Automuseums-gpx'
    gpx.name = f"Automuseums.info: {country['country']['name']}"
    gpx.description = f"Generated using {gpx.creator}"
    gpx.link = country['country']['absolute_url']
    gpx.time = datetime.datetime.now(datetime.timezone.utc)

    def create_gpx_waypoint(museum):
        gpx_wps = gpxpy.gpx.GPXWaypoint()
        gpx_wps.latitude = museum['coordinates'][0]['lat'] # WARNING: does not cover multi-location museums atm
        gpx_wps.longitude = museum['coordinates'][0]['lon'] # WARNING: does not cover multi-location museums atm
        gpx_wps.symbol = "Museum"
        gpx_wps.name = museum['name']
        gpx_wps.description = museum['description']
        return gpx_wps
    
    gpx.waypoints.extend(list(map(create_gpx_waypoint, country['museums'])))

    output_file_name = f"{selected_country['name']}.gpx"
    output_file_path = os.path.join(OUTPUT_ROOT, output_file_name)
    print(f"Generated {output_file_name}")
    with open(output_file_path, "w") as f:
        f.write(gpx.to_xml())
