import argparse
import glob
import json
import math
import os
import pathlib
import sys
import time
import requests
from bs4 import BeautifulSoup
import rich

# Define source URL
WEBSITE_ROOT_URL = 'https://automuseums.info'

# Define file paths
PROJECT_ROOT = pathlib.Path(__file__).parent.resolve()

# Define cache properties
CACHE_ROOT = os.path.join(PROJECT_ROOT, "cache")
COUNTRY_CACHE_MAX_AGE_MINUTES = 55
INDEX_CACHE_MAX_AGE_HOURS = 24

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
        return { 'name': name, 'relative_url': a_tag['href'] }

    property_list = list(map(define_country_properties, countries))

    return property_list

def download_country_index(country):
    print(f"Downloading {selected_country['name']}...")

    cache_country_root_path = os.path.join(CACHE_ROOT, 'countries')
    if not os.path.isdir(cache_country_root_path):
        os.mkdir(cache_country_root_path)

    cache_country_path = os.path.join(cache_country_root_path, selected_country['name'])
    if not os.path.isdir(cache_country_path):
        os.mkdir(cache_country_path)

    def download_index():
        index_pages = []

        # Delete old cache
        for old_cache_file in glob.glob(os.path.join(cache_country_path, "[0-9]*.html")):
            print(f"Deleting old cache file: {old_cache_file}")
            os.remove(old_cache_file)

        # Redownload country's index of museums
        museum_list_url = f"{WEBSITE_ROOT_URL}{selected_country['relative_url']}"
        for page_index in range(100): # make sure we never get stuck in infinite loop
            cached_file_name = f"{page_index}.html".rjust(7, '0')
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
        return download_index()
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
            
            return index_pages
        else:
            return download_index()

# Ensure cache_root exists
if not os.path.isdir(CACHE_ROOT):
    os.mkdir(CACHE_ROOT)

# Refresh country list
countries = load_countries()
# rich.print(countries)

# Build ArgumentParser https://docs.python.org/3/library/argparse.html
readable_country_list = ', '.join(map(lambda country: country['name'], countries))
arg_parser = argparse.ArgumentParser(epilog=f"Available countries: {readable_country_list}")
arg_parser.add_argument('--country', help='Limit scrape to one country')
args = arg_parser.parse_args()

country_indexes = []

if args.country:
    country_search_results = list(filter(lambda c: c['name'] == args.country, countries))
    if len(country_search_results) < 1:
        sys.exit(f"Country \"{args.country}\" not found.\n\nTry any of these: {readable_country_list}")

    selected_country = country_search_results[0]
    country_indexes.append(download_country_index(selected_country))
else:
    for selected_country in countries:
        country_indexes.append(download_country_index(selected_country))
