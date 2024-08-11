import argparse
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

if args.country:
    country_search_results = list(filter(lambda c: c['name'] == args.country, countries))
    if len(country_search_results) < 1:
        sys.exit(f"Country \"{args.country}\" not found.\n\nTry any of these: {readable_country_list}")

    selected_country = country_search_results[0]
    print(f"Downloading {selected_country}...")
else:
    for country in countries:
        print(f"Downloading {country['name']}...")
