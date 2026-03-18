import argparse
import datetime
import json
import math
import sys
import time
from functools import reduce
from itertools import chain
from pathlib import Path
from urllib.parse import quote

import gpxpy
import gpxpy.gpx
import humanize
import sentry_sdk
import yaml
from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter
from rich import print
from rich.pretty import pprint
from sentry_sdk.crons import capture_checkin
from sentry_sdk.crons.consts import MonitorStatus
from urllib3.util.retry import Retry

# Define source URL
WEBSITE_ROOT_URL = "https://automuseums.info"

# Define file paths
PROJECT_ROOT = Path(__file__).parent.resolve()
CONFIG_GROUP_FILENAME = "regions.yaml"

# Define cache properties
CACHE_ROOT = Path(PROJECT_ROOT) / "cache"
CACHE_COUNTRY_ROOT = Path(CACHE_ROOT) / "countries"

# Define output properties
OUTPUT_ROOT = Path(PROJECT_ROOT) / "output"
OUTPUT_ROOT_PER_COUNTRY = Path(OUTPUT_ROOT) / "per-country"
OUTPUT_ROOT_GROUPED = Path(OUTPUT_ROOT) / "grouped-by-region"
OUTPUT_ROOT_JSON = Path(OUTPUT_ROOT) / "json"
OUTPUT_FILENAME_PREFIX = "Automuseums.info - "

GPX_CREATOR = "https://github.com/TheStalwart/Automuseums-gpx"


def load_country_list():
    """
    Load and parse the Automuseums.info homepage to build a list of countries and their cache metadata.

    Returns:
      List[dict]: A list of country metadata dictionaries with keys:
        - name (str): Country display name.
        - relative_url (str): relative URL to the first page of museum list of the country.
        - absolute_url (str): absolute URL to the first page of museum list of the country.
        - cache_path (str): Path to a directory used to store cached pages for that country.
        - cache_timestamp (float): Modification timestamp of the cache file of the first page of museum list, or 0 if no cache file is present.
    """
    cache_file_path = Path(CACHE_ROOT) / "homepage.html"

    def download_homepage():
        print("Downloading country list...")
        r = requests.get(f"{WEBSITE_ROOT_URL}/homepage")
        homepage_contents = r.text

        with cache_file_path.open("w", encoding="utf-8") as f:
            f.write(homepage_contents)

        if args.request_delay > 0:
            time.sleep(args.request_delay)

        return homepage_contents

    html_contents = ""
    if not cache_file_path.is_file():
        html_contents = download_homepage()
    else:
        cache_file_modification_timestamp = cache_file_path.stat().st_mtime
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
        cache_file_age_minutes = math.floor(cache_file_age_seconds / 60)
        print(
            f"Country cache file is {cache_file_age_minutes}/{args.cache_ttl_countrylist} minutes old",
        )

        if cache_file_age_minutes < args.cache_ttl_countrylist:
            print("Loading cached country list...")
            with cache_file_path.open("r", encoding="utf-8") as f:
                html_contents = f.read()
        else:
            html_contents = download_homepage()

    # Parse homepage HTML
    soup = BeautifulSoup(html_contents, "html.parser")
    countries = soup.find(id="block-searchmuseumsin").find_all(
        "a",
    )  # https://beautiful-soup-4.readthedocs.io/en/latest/#navigating-the-tree

    def define_country_properties(a_tag):
        name = a_tag.contents[0].strip()

        relative_url = a_tag["href"]
        # A link to Bosnia on the main page contains invalid (non-urlencoded) href value.
        # It's one specific invalid value, all other country links e.g. "New Zealand" are urlencoded.
        if "&Herze" in relative_url:
            relative_url = quote(relative_url)

        cache_path = Path(CACHE_COUNTRY_ROOT) / name
        cache_file_path = Path(cache_path) / "00.html"
        cache_timestamp = 0  # countries with missing cache will keep 0 and be first in queue to update in lowprofile mode
        if cache_file_path.is_file():
            cache_timestamp = cache_file_path.stat().st_mtime

        return {
            "name": name,
            "relative_url": relative_url,
            "absolute_url": f"{WEBSITE_ROOT_URL}{relative_url}",
            "cache_path": cache_path,
            "cache_timestamp": cache_timestamp,
        }

    property_list = list(map(define_country_properties, countries))

    return property_list


def load_country_museum_list(selected_country):
    """
    Load and parse all museum list pages for a given country and return array of links to museum pages.

    Args:
      selected_country (dict): Country metadata dictionary returned by load_country_list(), with keys:
          - name (str)
          - relative_url (str)
          - absolute_url (str)
          - cache_path (str)
          - cache_timestamp (float)

    Returns:
      dict: A dictionary with keys:
        - 'country' (dict): The original selected_country argument.
        - 'museums' (List[dict]): A list of museum metadata dictionaries, each having:
            - 'name' (str)
            - 'relative_url' (str)
            - 'absolute_url' (str)
    """
    if not selected_country["cache_path"].is_dir():
        selected_country["cache_path"].mkdir()

    def format_return_value(museum_list):
        """
        Deduplicate and return a list of museums

        Museum list pages will display duplicates
        when a particular museum info page contains multiple locations.
        We deduplicate entries when building an index of museums,
        then produce multiple waypoints when building GPX files.

        Museum pages listing multiple locations, as of January 2025:
        - https://automuseums.info/czech-republic/museum-historical-motorcycles
        - https://automuseums.info/germany/fire-museum-schw%C3%A4bisch-hall
        - https://automuseums.info/australia/sir-henry-royce-foundation
        - https://automuseums.info/canada/western-development-museum
        - https://automuseums.info/russia/museum-vintage-motorcycles-and-antiques
        - https://automuseums.info/index.php/slovakia/skoda-classic-cars-museum
        - https://automuseums.info/switzerland/saurer-museum
        - https://automuseums.info/uruguay/eduardo-iglesias-automobile-museum
        - https://automuseums.info/iran/abadan-gasoline-house-museum (only one address)
        """

        deduplicated_museum_list = reduce(
            lambda l, x: l.append(x) or l if x not in l else l,
            museum_list,
            [],
        )  # https://stackoverflow.com/a/37163210
        return {"country": selected_country, "museums": deduplicated_museum_list}

    def parse_museum_list_page(soup):
        """
        Read museum list html parsed with BeautifulSoup and return names and URLs of found museums
        """
        museum_blocks = soup.find_all(class_="node-readmore")

        def define_museum_properties(li_tag):
            a_tag = li_tag.find("a")
            name = a_tag["title"].strip()
            return {
                "name": name,
                "relative_url": a_tag["href"],
                "absolute_url": f"{WEBSITE_ROOT_URL}{a_tag['href']}",
            }

        return list(map(define_museum_properties, museum_blocks))

    def download_index():
        print(f"Downloading [yellow]{selected_country['name']}[/yellow]...")
        full_museum_list = []

        # Delete old cache
        for old_cache_file in sorted(
            Path(selected_country["cache_path"]).glob("[0-9]*.html"),
        ):
            print(f"Deleting old cache file: {old_cache_file}")
            old_cache_file.unlink()

        # Redownload country's index of museums
        museum_list_url = selected_country["absolute_url"]
        for page_index in range(100):  # make sure we never get stuck in infinite loop
            cached_file_name = f"{page_index}.html".rjust(
                7,
                "0",
            )  # make all page numbers double-digits for easier sorting when loading cache
            cached_page_path = Path(selected_country["cache_path"]) / cached_file_name

            r = requests.get(museum_list_url, params={"page": page_index})
            print(f"Downloaded {r.url}")
            html_contents = r.text

            with cached_page_path.open("w", encoding="utf-8") as f:
                f.write(html_contents)

            soup = BeautifulSoup(html_contents, "html.parser")
            full_museum_list.extend(parse_museum_list_page(soup))

            if not soup.find(title="Go to next page"):
                print("Link to next page not found, bailing out")
                break
            else:
                if args.request_delay > 0:
                    time.sleep(args.request_delay)

        return full_museum_list

    cache_file_path = Path(selected_country["cache_path"]) / "00.html"
    if not cache_file_path.is_file():
        return format_return_value(download_index())
    else:
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - selected_country["cache_timestamp"]
        cache_file_age_hours = math.floor(cache_file_age_seconds / 60 / 60)
        print(
            f"[yellow]{selected_country['name']}[/yellow] index cache is {cache_file_age_hours}/{args.cache_ttl_museumlist} hours old",
        )

        if cache_file_age_hours < args.cache_ttl_museumlist:
            print("Loading cached index...")
            full_museum_list = []

            sorted_cache_file_path_array = sorted(
                Path(selected_country["cache_path"]).glob("[0-9]*.html"),
            )
            for cache_file_path in sorted_cache_file_path_array:
                print(f"Loading cache from {cache_file_path}...")
                with cache_file_path.open("r", encoding="utf-8") as f:
                    html_contents = f.read()
                    soup = BeautifulSoup(html_contents, "html.parser")
                    full_museum_list.extend(parse_museum_list_page(soup))

            return format_return_value(full_museum_list)
        else:
            return format_return_value(download_index())


def load_museum_page(country, museums, museum_properties):
    cache_museum_root_path = Path(country["cache_path"]) / "museums"
    if not cache_museum_root_path.is_dir():
        cache_museum_root_path.mkdir()

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

    name_slug = museum_properties["relative_url"].split("/")[
        -1
    ]  # always use last slug because there could be "/index.php/" in the middle
    sanitized_file_basename = "".join(
        [x if x.isalnum() else "_" for x in name_slug],
    )  # sanitize https://stackoverflow.com/a/295152
    cache_file_path = Path(cache_museum_root_path) / f"{sanitized_file_basename}.html"

    def download_page():
        r = requests.get(f"{WEBSITE_ROOT_URL}{museum_properties['relative_url']}")
        print(
            f"Downloaded {museums.index(museum_properties) + 1}/{len(museums)} {r.url}",
        )
        page_contents = r.text

        with cache_file_path.open("w", encoding="utf-8") as f:
            f.write(page_contents)

        if args.request_delay > 0:
            time.sleep(args.request_delay)

        return BeautifulSoup(page_contents, "html.parser")

    if not cache_file_path.is_file():
        return download_page(), cache_file_path
    else:
        cache_file_modification_timestamp = cache_file_path.stat().st_mtime
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
        cache_file_age_hours = math.floor(cache_file_age_seconds / 60 / 60)

        if cache_file_age_hours < args.cache_ttl_museumpage:
            print(
                f"Loading {cache_file_age_hours}/{args.cache_ttl_museumpage} hours old cached museum page for [yellow]{museum_properties['name']}[/yellow]...",
            )
            with cache_file_path.open("r", encoding="utf-8") as f:
                html_contents = f.read()
                return BeautifulSoup(html_contents, "html.parser"), cache_file_path
        else:
            return download_page(), cache_file_path


def parse_museum_page(page, museum_properties):
    museum_description = ""

    content_div = page.find(class_="node-content")

    links = []
    links_div = content_div.find(class_="field--name-link")
    if links_div:
        links = list(
            map(
                lambda a: {"url": a["href"], "title": a.text.strip()},
                links_div.find_all("a"),
            ),
        )

    body_div = content_div.find(class_="field--name-body")
    if body_div:
        # for some museums, description is wrapped in extra <p> tag
        # https://automuseums.info/denmark/egeskov-castle - has multiple <p> tags
        # https://automuseums.info/jordan/royal-automobile-museum - field--name-body value is enclosed in double-quotes

        # Most popular apps with GPX import feature do not support HTML tags,
        # so do a simple conversion to plain text
        museum_description = (
            "".join(list(body_div.text)).replace("\n", "\n\n").strip().strip('"')
        )

        # Some pages contain extra links in description,
        # e.g. https://automuseums.info/lithuania/lithuanian-road-museum
        # Since we strip description to plain text,
        # capture those extra links to avoid losing them.
        # Also, avoid random "<a id="search" name="search"></a>"
        # in https://automuseums.info/united-states/walker-transportation-collection
        # by only capturing links with text
        # https://pytutorial.com/beautifulsoup-find-by-text/
        # https://beautiful-soup-4.readthedocs.io/en/latest/#id12
        links_in_description = body_div.find_all("a", string=True)
        if links_in_description:
            links.extend(
                list(
                    map(
                        lambda a: {"url": a["href"], "title": a.text.strip()},
                        links_in_description,
                    ),
                ),
            )

    original_name = None
    abbreviation_div = content_div.find(class_="field--name-abbreviation")
    if (
        abbreviation_div
        and abbreviation_div.contents[0]
        and (abbreviation_div.contents[0] != museum_properties["name"])
    ):
        # if field--name-abbreviation value is different from main name
        # it's usually the original museum name in country's official language
        original_name = abbreviation_div.contents[0]

    display = None
    display_div = content_div.find(class_="field--name-display")
    if display_div and display_div.contents[0]:
        # "Display" section on museum page usually lists
        # what kinds of vehicles are exhibited
        display = list(
            map(
                lambda item_tag: item_tag.text,
                display_div.find_all(class_="field-item"),
            ),
        )

    info = None
    info_div = content_div.find(class_="field--name-info")
    if (
        info_div
        and info_div.find(class_="field-item")
        and info_div.find(class_="field-item").contents[0]
    ):
        # this field usually contains extra properties
        # e.g. opening times or "Open by appointment" string
        info = "".join(list(info_div.find(class_="field-item").text)).strip().strip('"')

    address = None
    address_div = content_div.find(class_="field--name-address")
    if address_div and address_div.contents[0]:
        # "Address" section is structured as a list of field-item tags
        # each one containing a structure of spans
        # for every part of the address.
        # Do our best flattening those structures
        # to return an array of multiline strings.
        # As of January 2025, almost all multi-coordinates museums
        # are also a multi-address museums
        # with address index matching coordinates index.
        # When generating GPX waypoints from multi-location museum
        # we only pick one address and one coordinate pair per waypoint.
        address = list(
            map(
                lambda address_item_tag: address_item_tag.text.strip(),
                address_div.find_all(class_="field-item"),
            ),
        )

    email = None
    email_div = content_div.find(class_="field--name-e-mail")
    if email_div and email_div.contents[0]:
        # "E-mail" section is a list of items,
        # much like "Display" section
        email = list(
            map(
                lambda item_tag: item_tag.text.strip(),
                email_div.find_all(class_="field-item"),
            ),
        )

    phone = None
    phone_div = content_div.find(class_="field--name-phone")
    if phone_div and phone_div.contents[0]:
        # "Phone" section is a list of items,
        # much like "Display" section
        phone = list(
            map(
                lambda item_tag: item_tag.text.strip(),
                phone_div.find_all(class_="field-item"),
            ),
        )

    drupal_node_id = page.find("article")["data-history-node-id"]

    data_json = page.find(
        attrs={"data-drupal-selector": "drupal-settings-json"},
    ).contents[0]
    data = json.loads(data_json)
    leaflet_features = data["leaflet"][
        f"leaflet-map-node-museum-{drupal_node_id}-coordinates"
    ]["features"]
    leaflet_points = list(filter(lambda f: f["type"] == "point", leaflet_features))
    coordinates = list(
        map(lambda p: {"lat": p["lat"], "lon": p["lon"]}, leaflet_points),
    )

    return {
        "description": museum_description,
        "original_name": original_name,
        "display": display,
        "info": info,
        "address": address,
        "email": email,
        "phone": phone,
        "links": links,
        "drupal_node_id": drupal_node_id,
        "coordinates": coordinates,
    }


# Init Sentry before doing anything that might raise exception
try:
    dsn_file_path = Path(PROJECT_ROOT) / "sentry.dsn"
    sentry_sdk.init(
        dsn=dsn_file_path.read_text(),
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for tracing.
        traces_sample_rate=1.0,
    )
except:
    pass

# Attempt to load Better Stack heartbeat token
betterstack_heartbeat_url = None
try:
    heartbeat_file_path = Path(PROJECT_ROOT) / "heartbeat.url"
    betterstack_heartbeat_url = heartbeat_file_path.read_text().strip()
except:
    pass


def report_failure_and_exit():
    if betterstack_heartbeat_url:
        print(f"Reporting heartbeat to {betterstack_heartbeat_url}/fail")
        response = requests.get(f"{betterstack_heartbeat_url}/fail")
        if not response.ok:
            print("Failed!")
        print(f"Response: [{response.status_code}]")
    sys.exit(1)


start_datetime = datetime.datetime.now()

# Ensure cache folders exist
if not CACHE_ROOT.is_dir():
    CACHE_ROOT.mkdir()
if not CACHE_COUNTRY_ROOT.is_dir():
    CACHE_COUNTRY_ROOT.mkdir()

# Build ArgumentParser https://docs.python.org/3/library/argparse.html
arg_parser = argparse.ArgumentParser()
arg_parser.add_argument("--country", help="Limit scrape to one country")
arg_parser.add_argument(
    "--cache-ttl-countrylist",
    type=int,
    default=55,
    help="Override country list cache time-to-live in minutes (default: %(default)s)",
)
arg_parser.add_argument(
    "--cache-ttl-museumlist",
    type=int,
    default=24,
    help="Override museum list cache time-to-live in hours (default: %(default)s)",
)
arg_parser.add_argument(
    "--cache-ttl-museumpage",
    type=int,
    default=48,
    help="Override museum page cache time-to-live in hours (default: %(default)s)",
)
arg_parser.add_argument(
    "--request-delay",
    type=int,
    default=15,
    help="Delay after every HTTPS request in seconds (default: %(default)s)",
)
arg_parser.add_argument(
    "--lowprofile",
    action="store_true",
    help="Update 1 country with oldest cache",
)
arg_parser.add_argument(
    "--group",
    action="store_true",
    help="Generate files grouped by region",
)
arg_parser.add_argument(
    "--verbose",
    action="store_true",
    help="Print data used to generate GPX files",
)

# During development i often diff GPX output
# and <time> tag makes output noisy
arg_parser.add_argument(
    "--omit-time",
    action="store_true",
    help="Omit <time> tag from generated GPX files",
)

args = arg_parser.parse_args()

# Set up a customized instance of Requests library
# to avoid crashing on monthly DNS resolution failures
# https://stackoverflow.com/questions/23013220/max-retries-exceeded-with-url-in-requests
requests = Session()
request_retry_config = Retry(total=5, backoff_factor=args.request_delay)
http_adapter = HTTPAdapter(max_retries=request_retry_config)
requests.mount("http://", http_adapter)
requests.mount("https://", http_adapter)

# Make sure we don't run more than one instance
# on the same set of cache/output folders
lock_file_path = Path(PROJECT_ROOT) / "cli.lock"
if lock_file_path.is_file():
    # if script is launched in lowprofile mode,
    # but lockfile is older than 24h -
    # assume previous execution has failed,
    # e.g. due to host machine power failure,
    # recreate the lock and carry on
    if args.lowprofile and lock_file_path.stat().st_mtime < time.time() - 60 * 60 * 24:
        print("[red]Deleting stale lock file[/red]")
        lock_file_path.unlink()
    else:
        if (
            sys.gettrace() or "debugpy" in sys.modules
        ):  # https://stackoverflow.com/a/72977762/5337349
            print("[red]Lock file ignored due to debugging[/red]")
        else:
            print("[red]Another instance of the script is running, exiting[/red]")
            report_failure_and_exit()
lock_file_path.open("w").close()

# Check-in with Sentry cron monitoring
sentry_lowprofile_slug = "lowprofile"
sentry_check_in_id = ""
if args.lowprofile:
    sentry_check_in_id = capture_checkin(
        monitor_slug=sentry_lowprofile_slug,
        status=MonitorStatus.IN_PROGRESS,
    )

    # Calls to stop_profiler are optional - if you don't stop the profiler, it will keep profiling
    # your application until the process exits or stop_profiler is called.
    sentry_sdk.profiler.start_profiler()

# Refresh country list
country_list = load_country_list()
country_indexes = []

if args.country:
    country_search_results = list(
        filter(lambda c: c["name"] == args.country, country_list),
    )
    if len(country_search_results) < 1:
        # technically, a clean exit
        # even though no useful work has been done
        lock_file_path.unlink()

        readable_country_list = ", ".join(
            map(lambda country: country["name"], country_list),
        )
        sys.exit(
            f'Country "{args.country}" not found.\n\nTry any of these: {readable_country_list}',
        )

    selected_country = country_search_results[0]
    country_indexes.append(load_country_museum_list(selected_country))
else:
    if args.lowprofile:
        print("Keeping low profile, updating 1 country with oldest cache...")
        selected_country = sorted(country_list, key=lambda c: c["cache_timestamp"])[0]
        country_indexes.append(load_country_museum_list(selected_country))
    else:
        print("Updating all country indexes...")
        for selected_country in country_list:
            country_indexes.append(load_country_museum_list(selected_country))

for country in country_indexes:
    print(
        f"Loading {len(country['museums'])} museums of [yellow]{country['country']['name']}[/yellow]...",
    )
    for museum_properties in country["museums"]:
        page, cache_file_path = load_museum_page(
            country["country"],
            country["museums"],
            museum_properties,
        )
        museum_properties["cache_file_path"] = cache_file_path
        museum_properties.update(parse_museum_page(page, museum_properties))
    if not args.verbose:
        print(
            f"Parsed [yellow]{country['country']['name']}[/yellow]: {len(country['museums'])} museums",
        )

    if not OUTPUT_ROOT_JSON.is_dir():
        OUTPUT_ROOT_JSON.mkdir()
    json_output_file_name = f"{country['country']['name']}.json"
    json_output_file_path = Path(OUTPUT_ROOT_JSON) / json_output_file_name
    with json_output_file_path.open("w", encoding="utf-8") as json_output_file:
        json.dump(country, json_output_file, indent=2)

if args.verbose:
    print(country_indexes)

# Generate per-country GPX files
# https://github.com/tkrajina/gpxpy/blob/dev/examples/waypoints_example.py
for country in country_indexes:
    gpx = gpxpy.gpx.GPX()
    gpx.creator = GPX_CREATOR
    gpx.name = f"Automuseums.info: {country['country']['name']}"
    gpx.description = f"Generated using {gpx.creator}"
    gpx.link = country["country"]["absolute_url"]

    if not args.omit_time:
        gpx.time = datetime.datetime.now(datetime.timezone.utc)

    def create_gpx_waypoint(museum, location_index):
        gpx_wps = gpxpy.gpx.GPXWaypoint()
        gpx_wps.latitude = museum["coordinates"][location_index]["lat"]
        gpx_wps.longitude = museum["coordinates"][location_index]["lon"]
        gpx_wps.symbol = "Museum"

        gpx_wps.name = museum["name"]
        if location_index > 0:
            gpx_wps.name = f"{gpx_wps.name} ({location_index + 1})"

        gpx_wps.description = museum["description"]

        # Prepend description with museum's original name in native language, if available
        if museum["original_name"]:
            gpx_wps.description = f"{museum['original_name']}\n\n{gpx_wps.description}"

        # Append "Display" section listing
        # what kinds of vehicles are exhibited
        if museum["display"]:
            display_item_list_formatted = "\n".join(
                list(map(lambda di: f"- {di}", museum["display"])),
            )
            display_section_formatted = f"Display:\n{display_item_list_formatted}"
            gpx_wps.description = (
                f"{gpx_wps.description}\n\n{display_section_formatted}"
            )

        # Append "Info" section
        # usually containing opening times
        if museum["info"]:
            gpx_wps.description = f"{gpx_wps.description}\n\n{museum['info']}"

        # Append "Address" section
        if museum["address"]:
            if len(museum["coordinates"]) == len(museum["address"]):
                # if address count matches coordinates count,
                # assume their indexes match,
                # as that is the case for museums i tested as of January 2025.
                address_section_formatted = (
                    f"Address:\n{museum['address'][location_index]}"
                )
                gpx_wps.description = (
                    f"{gpx_wps.description}\n\n{address_section_formatted}"
                )
            else:
                # there is a museum in Iran
                # that has two coordinates but only one address
                # https://automuseums.info/iran/abadan-gasoline-house-museum
                address_item_list_formatted = "\n\n".join(
                    list(map(lambda ai: f"{ai}", museum["address"])),
                )
                address_section_formatted = f"Address:\n{address_item_list_formatted}"
                gpx_wps.description = (
                    f"{gpx_wps.description}\n\n{address_section_formatted}"
                )

        # Append "E-mail" section if available
        if museum["email"]:
            if len(museum["email"]) > 1:
                email_item_list_formatted = "\n".join(
                    list(map(lambda pi: f"{pi}", museum["email"])),
                )
                email_section_formatted = f"E-mail:\n{email_item_list_formatted}"
                gpx_wps.description = (
                    f"{gpx_wps.description}\n\n{email_section_formatted}"
                )
            else:
                # most museums have only one email listed,
                # so collapse the entry into a single line
                gpx_wps.description = (
                    f"{gpx_wps.description}\n\nE-mail: {museum['email'][0]}"
                )

        # Append "Phone" section if available
        if museum["phone"]:
            if len(museum["phone"]) > 1:
                phone_item_list_formatted = "\n".join(
                    list(map(lambda pi: f"{pi}", museum["phone"])),
                )
                phone_section_formatted = f"Phone:\n{phone_item_list_formatted}"
                gpx_wps.description = (
                    f"{gpx_wps.description}\n\n{phone_section_formatted}"
                )
            else:
                # most museums have only one phone number listed,
                # so collapse the entry into a single line
                gpx_wps.description = (
                    f"{gpx_wps.description}\n\nPhone: {museum['phone'][0]}"
                )

        # GPX 1.1 Schema supports multiple links per waypoint,
        # https://www.topografix.com/gpx.asp
        # https://www.topografix.com/GPX/1/1/gpx.xsd
        # but gpxpy library assumes there can be only one link tag
        # https://github.com/tkrajina/gpxpy/issues/138
        gpx_wps.link = museum["absolute_url"]

        # Besides this gpxpy issue,
        # Google My Maps ignores <link> tags in Waypoints when importing,
        # so add all the links at the end of <desc> tag
        links = museum["links"].copy()
        links.append({"url": museum["absolute_url"], "title": "Automuseums.info"})
        links_section_plaintext = "\n".join(
            list(map(lambda l: f"{l['title']}: {l['url']}", links)),
        )
        gpx_wps.description = f"{gpx_wps.description}\n\n{links_section_plaintext}"

        return gpx_wps

    for museum in country["museums"]:
        for location_index in range(len(museum["coordinates"])):
            gpx.waypoints.append(create_gpx_waypoint(museum, location_index))

    if not OUTPUT_ROOT_PER_COUNTRY.is_dir():
        OUTPUT_ROOT_PER_COUNTRY.mkdir()

    output_file_name = f"{OUTPUT_FILENAME_PREFIX}{country['country']['name']}.gpx"
    output_file_path = Path(OUTPUT_ROOT_PER_COUNTRY) / output_file_name

    if len(gpx.waypoints) > 0:
        with output_file_path.open("w", encoding="utf-8") as f:
            f.write(gpx.to_xml())
        print(f"Generated [cyan]{output_file_name}[/cyan]")
    else:
        print(
            f"Not generating [red]{output_file_name}[/red] due to {len(gpx.waypoints)} museums in [yellow]{country['country']['name']}[/yellow]",
        )

# Regenerate GPX files grouped by region
if args.group:
    groups = {}

    # Load country groups from YAML config file
    # https://stackoverflow.com/a/1774043/5337349
    group_config_file_path = Path(PROJECT_ROOT) / CONFIG_GROUP_FILENAME
    with group_config_file_path.open() as stream:
        try:
            groups = yaml.safe_load(stream)
            print(f"Loaded {CONFIG_GROUP_FILENAME}:")
            pprint(groups)
        except yaml.YAMLError as exc:
            print(exc)

    # Extend groups definition with "All Countries"
    groups["All countries"] = list(map(lambda c: c["name"], country_list))

    # Load all generated per-country GPX files we need for groups defined in YAML config file
    required_countries = list(set(chain.from_iterable(groups.values())))

    def load_country_gpx_data(country_name):
        country_file_name = f"{OUTPUT_FILENAME_PREFIX}{country_name}.gpx"
        file_path = Path(OUTPUT_ROOT_PER_COUNTRY) / country_file_name

        if not file_path.is_file():
            print(f"Warning: missing [red]{country_file_name}[/red]")
            return None

        with file_path.open("r", encoding="utf-8") as gpx_file:
            return gpxpy.parse(gpx_file)

    per_country_data = {
        k: v
        for (k, v) in zip(
            required_countries,
            map(load_country_gpx_data, required_countries),
        )
    }

    if not OUTPUT_ROOT_GROUPED.is_dir():
        OUTPUT_ROOT_GROUPED.mkdir()

    # Generate GPX files grouped by region
    for group_name, group_countries in groups.items():
        group_output_file_name = f"{OUTPUT_FILENAME_PREFIX}{group_name}.gpx"
        group_output_file_path = Path(OUTPUT_ROOT_GROUPED) / group_output_file_name

        gpx = gpxpy.gpx.GPX()
        gpx.creator = GPX_CREATOR
        gpx.name = f"Automuseums.info: {group_name}"
        gpx.description = f"Generated using {gpx.creator}"
        gpx.link = WEBSITE_ROOT_URL

        if not args.omit_time:
            gpx.time = datetime.datetime.now(datetime.timezone.utc)

        for country_name in group_countries:
            if isinstance(per_country_data[country_name], gpxpy.gpx.GPX):
                gpx.waypoints.extend(per_country_data[country_name].waypoints)

        if len(gpx.waypoints) > 0:
            with group_output_file_path.open("w", encoding="utf-8") as f:
                f.write(gpx.to_xml())
            print(f"Generated [magenta]{group_output_file_name}[/magenta]")
        else:
            print(
                f"Not generating [red]{group_output_file_name}[/red] due to {len(gpx.waypoints)} museums in {group_name}",
            )

humanized_execution_duration = humanize.precisedelta(
    datetime.datetime.now() - start_datetime,
    minimum_unit="seconds",
    format="%.0f",
)
print(f"Completed in {humanized_execution_duration}")

# Clean exit
lock_file_path.unlink()

if args.lowprofile:
    capture_checkin(
        monitor_slug=sentry_lowprofile_slug,
        check_in_id=sentry_check_in_id,
        status=MonitorStatus.OK,
    )

    # Report success to Better Stack
    if betterstack_heartbeat_url:
        print(f"Reporting heartbeat to {betterstack_heartbeat_url}")
        response = requests.get(betterstack_heartbeat_url)
        if not response.ok:
            print("Failed!")
        print(f"Response: [{response.status_code}]")
