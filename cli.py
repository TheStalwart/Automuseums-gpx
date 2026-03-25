import argparse
import copy
import datetime
import json
import math
import re
import sys
import time
from html import unescape
from itertools import chain
from pathlib import Path

import gpxpy
import gpxpy.gpx
import humanize
import sentry_sdk
import yaml
from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterSession
from rich import print as rprint
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
    Load and parse the Automuseums.info homepage
    to build a list of countries and their cache metadata.

    Returns:
        List[dict]: A list of country metadata dictionaries with keys:

        - name (str): Country display name.
        - relative_url (str): Relative URL to the first page
          of the country's museum list.
        - absolute_url (str): Absolute URL to the first page
          of the country's museum list.
        - cache_path (str): Path to a directory
          used to store cached pages for that country.
        - cache_index_path (Path): Museum list cache JSON file path
        - cache_index_timestamp (float): Modification timestamp
          of the museum list cache JSON file,
          or 0 if no cache file is present.
    """
    cache_file_path = Path(CACHE_ROOT) / "homepage.html"

    def download_homepage():
        rprint("Downloading country list...")
        r = requests.get(f"{WEBSITE_ROOT_URL}")
        homepage_contents = r.text

        with cache_file_path.open("w", encoding="utf-8") as f:
            f.write(homepage_contents)

        return homepage_contents

    html_contents = ""
    if not cache_file_path.is_file():
        html_contents = download_homepage()
    else:
        cache_file_modification_timestamp = cache_file_path.stat().st_mtime
        current_timestamp = time.time()
        cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
        cache_file_age_minutes = math.floor(cache_file_age_seconds / 60)
        rprint(
            "Country cache file is"
            f" {cache_file_age_minutes}/{args.cache_ttl_countrylist} minutes old",
        )

        if cache_file_age_minutes < args.cache_ttl_countrylist:
            rprint("Loading cached country list...")
            with cache_file_path.open("r", encoding="utf-8") as f:
                html_contents = f.read()
        else:
            html_contents = download_homepage()

    # Parse homepage HTML
    soup = BeautifulSoup(html_contents, "html.parser")
    countries = soup.find(id="filter-country").find_all(
        "option",
        value=re.compile(r".+"),
    )  # https://beautiful-soup-4.readthedocs.io/en/latest/#navigating-the-tree

    def define_country_properties(a_tag):
        name = a_tag["value"].strip()

        relative_url = f"/museums/?country={name}"

        cache_path = Path(CACHE_COUNTRY_ROOT) / name
        cache_index_path = Path(cache_path) / "index.json"

        # countries with missing cache will keep 0
        # and be first in queue to update in lowprofile mode
        cache_index_timestamp = 0

        if cache_index_path.is_file():
            cache_index_timestamp = cache_index_path.stat().st_mtime

        return {
            "name": name,
            "relative_url": relative_url,
            "absolute_url": f"{WEBSITE_ROOT_URL}{relative_url}",
            "cache_path": cache_path,
            "cache_index_path": cache_index_path,
            "cache_index_timestamp": cache_index_timestamp,
        }

    return list(map(define_country_properties, countries))


def load_country_museum_list(selected_country):
    """
    Load and parse all museum list pages for a given country
    and return array of links to museum pages.

    Args:
        selected_country (dict): Country metadata dictionary \
        returned by load_country_list(), with keys:
            - name (str)
            - relative_url (str)
            - absolute_url (str)
            - cache_path (str)
            - cache_index_path (Path)
            - cache_index_timestamp (float)

    Returns:
        dict: A dictionary with keys:
        - 'country' (dict): The original selected_country argument.
        - 'museums' (List[dict]): A list of museum metadata dictionaries, each having:
            - 'id' (float)
            - 'title' (str)
            - 'permalink' (str)
            - 'excerpt' (str)
            - 'featured_image' (str)
            - 'country' (str)
            - 'city' (str)
            - 'vehicle_types' (List[str])
            - 'latitude' (str)
            - 'longitude' (str)
            - 'is_fiva_member' (bool)
    """
    if not selected_country["cache_path"].is_dir():
        selected_country["cache_path"].mkdir()

    def format_return_value(museum_list):
        """
        Deduplicate and return a list of museums

        Museum list pages on the old Drupal website would display duplicates
        when a particular museum info page contained multiple locations.
        We deduplicated entries when building an index of museums,
        then produced multiple waypoints when building GPX files.

        On January 30th, 2026, the website was migrated from Drupal to Wordpress,
        and now the museums with multiple locations are not duplicated,
        but only display a single geolocation.

        This issue was found on March 18th, 2026,
        during codebase migration to support the new Wordpress engine.

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

        # Extract "Human readable" country name
        selected_country["title"] = museum_list[0]["country"]

        return {"country": selected_country, "museums": museum_list}

    def download_index():
        rprint(f"Downloading [yellow]{selected_country['name']}[/yellow]...")
        full_museum_list = []

        # Delete old cache
        rprint(
            "Deleting old museum index cache file:"
            f" {selected_country['cache_index_path']}",
        )
        if selected_country["cache_index_path"].is_file():
            selected_country["cache_index_path"].unlink()

        # Redownload country's index of museums
        museum_list_url = f"{WEBSITE_ROOT_URL}/wp-json/automuseums/v1/museums/filter"
        for page_index in range(
            1,
            100,  # make sure we never get stuck in infinite loop
        ):
            # On new WordPress-based website,
            # orderby parameter has "date" option now.
            # Could i request museums ordered by date
            # to avoid rescraping data that didn't change?
            # Next time i report an issue, e.g. bad geo coordinates,
            # check if museum order has changed in response.

            r = requests.get(
                museum_list_url,
                params={
                    "country": selected_country["name"],
                    "orderby": "name",
                    "page": page_index,
                    "per_page": 100,  # maximum supported by WordPress
                },
            )
            rprint(f"Downloaded {r.url}")
            json_contents = r.json()

            museum_array = json_contents["museums"]

            # As of March 2026, some museums had no geographic coordinates,
            # e.g. https://automuseums.info/museum/beijing-classic-car-museum/
            # and https://automuseums.info/museum/nemes-motor-museum/ .
            # They had empty strings for latitude and longitude in index
            # and no "geo" key in page's JSON.
            # These issues were reported to the website admin,
            # and Beijing was fixed by adding geolocation data,
            # but Nemes is likely to stay without precise geolocation
            # due to a privacy request from museum owner.
            # So this failsafe filter should stay indefinitely.
            def has_geolocation(museum):
                if not (len(museum["latitude"]) and len(museum["longitude"])):
                    rprint(
                        f"[red]Warning:[/red] {museum['title']} ({museum['id']})"
                        " excluded for missing geolocation",
                    )
                    return False

                return True

            museums_with_geolocation = filter(has_geolocation, museum_array)

            full_museum_list.extend(museums_with_geolocation)

            if json_contents["current_page"] == json_contents["pages"]:
                rprint("Next page not found, bailing out")
                break

        with selected_country["cache_index_path"].open(
            "w",
            encoding="utf-8",
        ) as json_output_file:
            json.dump({"museums": full_museum_list}, json_output_file, indent=2)

        return full_museum_list

    if not selected_country["cache_index_path"].is_file():
        return format_return_value(download_index())

    current_timestamp = time.time()
    cache_file_age_seconds = (
        current_timestamp - selected_country["cache_index_timestamp"]
    )
    cache_file_age_hours = math.floor(cache_file_age_seconds / 60 / 60)
    rprint(
        f"[yellow]{selected_country['name']}[/yellow] index cache"
        f" is {cache_file_age_hours}/{args.cache_ttl_museumlist} hours old",
    )

    if cache_file_age_hours < args.cache_ttl_museumlist:
        rprint("Loading cached index...")
        full_museum_list = []

        with selected_country["cache_index_path"].open("r") as f:
            data = json.load(f)
            full_museum_list.extend(data["museums"])

        return format_return_value(full_museum_list)

    return format_return_value(download_index())


def load_museum_page(country, museums, museum_properties):
    cache_museum_root_path = Path(country["cache_path"]) / "museums"
    if not cache_museum_root_path.is_dir():
        cache_museum_root_path.mkdir()

    cache_file_path = Path(cache_museum_root_path) / f"{museum_properties['id']}.html"

    def download_page():
        r = requests.get(museum_properties["permalink"])
        rprint(
            f"Downloaded {museums.index(museum_properties) + 1}/{len(museums)} {r.url}",
        )
        page_contents = r.text

        with cache_file_path.open("w", encoding="utf-8") as f:
            f.write(page_contents)

        return BeautifulSoup(page_contents, "html.parser")

    if not cache_file_path.is_file():
        return download_page(), cache_file_path

    cache_file_modification_timestamp = cache_file_path.stat().st_mtime
    current_timestamp = time.time()
    cache_file_age_seconds = current_timestamp - cache_file_modification_timestamp
    cache_file_age_hours = math.floor(cache_file_age_seconds / 60 / 60)

    if cache_file_age_hours < args.cache_ttl_museumpage:
        rprint(
            f"Loading {cache_file_age_hours}/{args.cache_ttl_museumpage} hours old"
            " cached museum page"
            f" for [yellow]{museum_properties['title']}[/yellow]...",
        )
        with cache_file_path.open("r", encoding="utf-8") as f:
            html_contents = f.read()
            return BeautifulSoup(html_contents, "html.parser"), cache_file_path
    else:
        return download_page(), cache_file_path


def parse_museum_page(page, museum_properties):
    # New (since February 2026) Wordpress frontend
    # exposes most of the useful values
    # as a convenient JSON embedded in HTML <script> tag.
    # But some values like description and links
    # are clipped in JSON,
    # so we still need to parse HTML like savages.
    museum_json_tag = page.find(type="application/ld+json")
    museum_json = json.loads(museum_json_tag.text)

    # Fallback description from JSON.
    # It's trimmed, but it's better than no description,
    # if we fail to parse HTML
    museum_description = museum_json["description"]

    # Links are extracted from multiplace places on the page,
    # then filtered before returning the value back to index.
    links = []

    # Since migration to Wordpress, there are two layouts:
    # one regular, e.g. https://automuseums.info/museum/the-royal-automobile-museum/
    # and another for FIVA-certified museums, e.g. https://automuseums.info/museum/grom-motorcycle-museum/
    description_div = page.find(
        class_="museum-description-content",
    ) or page.find(
        class_="fiva-description",
    )

    if description_div:
        # For some museums, description is wrapped in extra <p> tag
        #
        # https://automuseums.info/museum/egeskov-castle/ (denmark)
        #       has multiple <p> tags
        #
        # https://automuseums.info/museum/the-royal-automobile-museum/ (jordan)
        #       field--name-body value is enclosed in double-quotes
        #
        # Most popular apps with GPX import feature do not support HTML tags,
        # so do a simple conversion to plain text
        museum_description = (
            "".join(list(description_div.text)).replace("\n", "\n\n").strip().strip('"')
        )

        # Some pages contain extra links in description,
        # e.g. https://automuseums.info/museum/lithuanian-road-museum/
        # Since we strip description to plain text,
        # capture those extra links to avoid losing them.
        # Also, avoid random "<a id="search" name="search"></a>"
        # in https://automuseums.info/museum/walker-transportation-collection/
        # by only capturing links with text with `string=True` parameter
        # https://pytutorial.com/beautifulsoup-find-by-text/
        # https://beautiful-soup-4.readthedocs.io/en/latest/#id12
        a_tags_in_description = description_div.find_all("a", string=True)
        if a_tags_in_description:
            links_in_description = [
                {"url": a["href"], "title": a.text.strip()}
                for a in a_tags_in_description
            ]
            links.extend(links_in_description)

    original_name = None
    abbreviation_div = page.find(
        class_="museum-hero-native-name",
    ) or page.find(
        class_="fiva-museum-native-name",
    )
    if abbreviation_div and abbreviation_div.text:
        # if field--name-abbreviation value is different from main name
        # it's usually the original museum name in country's official language
        title_in_index = unescape(museum_properties["title"])
        original_name_candidate = abbreviation_div.text.strip()
        if original_name_candidate != title_in_index:
            original_name = original_name_candidate

    info = None
    info_div = page.find(
        class_="museum-additional-info-content",
    ) or page.find(
        class_="fiva-additional-info-content",
    )
    if info_div and info_div.text:
        # this field usually contains extra properties
        # e.g. opening times or "Open by appointment" string
        info = "".join(list(info_div.text)).strip().strip('"')

    address = None
    leaflet_div = page.find(
        id="single-museum-map",
    ) or page.find(
        id="fiva-single-museum-map",
    )
    if leaflet_div:
        # In the old Drupal version, almost all multi-coordinates museums
        # were also a multi-address museums
        # with address index matching coordinates index.
        # When generating GPX waypoints from multi-location museum
        # we only picked one address and one coordinate pair per waypoint.
        # In the new Wordpress version, as of March 2026,
        # multi-location museums are not implemented.
        address_string = leaflet_div["data-address"]

        # FIVA Certified Members have separate fields for city and country.
        # Concatenate components to match non-FIVA format
        city = leaflet_div.get("data-city")
        if city:
            address_string += f", {city}"
        country = leaflet_div.get("data-country")
        if country:
            address_string += f", {country}"

        address = [address_string]

    email = None
    phone = None

    contact_point = museum_json.get("contactPoint")
    if contact_point:
        contact_point_email = contact_point.get("email")
        if contact_point_email:
            email = [contact_point_email]

        contact_point_telephone = contact_point.get("telephone")
        if contact_point_telephone:
            # Grom Motorcycle Museum
            # has too many spaces in phone number value.
            # Simplify for better format recognition by apps.
            # https://automuseums.info/museum/grom-motorcycle-museum/
            phone = [contact_point_telephone.replace(" ", "")]

    # Proper links from HTML.
    # Some pages have more links in HTML, than in JSON,
    # e.g. https://automuseums.info/museum/ikaho-toy-doll-and-car-museum/
    # Select only one copy of contact-information-card,
    # because there are multiple copies for different layouts
    contact_info_card_div = page.find(class_="contact-information-card")
    if contact_info_card_div:

        def strip_link_title(a_tag):
            a_tag_copy = copy.deepcopy(a_tag)

            # Social media links
            # have undesirable emoji prefix
            for emoji_container in a_tag_copy.find_all(class_="social-icon"):
                emoji_container.decompose()

            return a_tag_copy.text.strip()

        links.extend(
            [
                {"url": a["href"], "title": strip_link_title(a)}
                for a in contact_info_card_div.find_all("a")
            ],
        )

    # Filter out phone and email links
    # because we have a separate property for that
    links = list(filter(lambda link: not link["url"].startswith("tel:"), links))
    links = list(filter(lambda link: not link["url"].startswith("mailto:"), links))

    # Fallback links from JSON.
    # Only one main link usually.
    # Sometimes no links and sameAs key is absent,
    # e.g. https://automuseums.info/museum/museum-of-antique-technique-sodeliskiu/
    if museum_json.get("sameAs"):
        # Only add links from this source
        # if the URL is not already found elsewhere
        urls_present_in_links = {entry["url"] for entry in links}
        links.extend(
            [
                {"url": url, "title": "Website"}
                for url in museum_json["sameAs"]
                if url not in urls_present_in_links
            ],
        )

    # Since migration from Drupal to Wordpress,
    # multi-location museums only list one coordinate set.
    # I reported that to the website admin in March 2026,
    # and he replied that it's a known issue that will be fixed in the future.
    # GPX build can get these values from museum index,
    # but i'm gonna keep a copy in this structure
    # until we know how upstream will handle multi-location museums.
    coordinates = [
        {"lat": museum_properties["latitude"], "lon": museum_properties["longitude"]},
    ]

    return {
        "description": museum_description,
        "original_name": original_name,
        "info": info,
        "address": address,
        "email": email,
        "phone": phone,
        "links": links,
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
except (OSError, sentry_sdk.utils.BadDsn):
    pass

# Attempt to load Better Stack heartbeat token
betterstack_heartbeat_url = None
try:
    heartbeat_file_path = Path(PROJECT_ROOT) / "heartbeat.url"
    betterstack_heartbeat_url = heartbeat_file_path.read_text().strip()
except OSError:
    pass


def report_failure_and_exit():
    if betterstack_heartbeat_url:
        rprint(f"Reporting heartbeat to {betterstack_heartbeat_url}/fail")
        response = requests.get(f"{betterstack_heartbeat_url}/fail")
        if not response.ok:
            rprint("Failed!")
        rprint(f"Response: [{response.status_code}]")
    sys.exit(1)


start_datetime = datetime.datetime.now(datetime.timezone.utc)

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
    default=168,  # 7 days
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
# with rate limiter and retry config
# to avoid crashing on monthly DNS resolution failures
# https://stackoverflow.com/questions/23013220/max-retries-exceeded-with-url-in-requests
requests = LimiterSession(per_second=(60 / args.request_delay) / 60)
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
        rprint("[red]Deleting stale lock file[/red]")
        lock_file_path.unlink()
    elif (
        sys.gettrace() or "debugpy" in sys.modules
    ):  # https://stackoverflow.com/a/72977762/5337349
        rprint("[red]Lock file ignored due to debugging[/red]")
    else:
        rprint("[red]Another instance of the script is running, exiting[/red]")
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

    # Calls to stop_profiler are optional,
    # If you don't stop the profiler, it will keep profiling
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
            (country["name"] for country in country_list),
        )
        sys.exit(
            f'Country "{args.country}" not found.\n\n'
            f"Try any of these: {readable_country_list}",
        )

    selected_country = country_search_results[0]
    country_indexes.append(load_country_museum_list(selected_country))
elif args.lowprofile:
    rprint("Keeping low profile, updating 1 country with oldest cache...")
    selected_country = sorted(country_list, key=lambda c: c["cache_index_timestamp"])[0]
    country_indexes.append(load_country_museum_list(selected_country))
else:
    rprint("Updating all country indexes...")
    country_indexes.extend(
        load_country_museum_list(selected_country) for selected_country in country_list
    )

for country in country_indexes:
    rprint(
        f"Loading {len(country['museums'])} museums"
        f" of [yellow]{country['country']['name']}[/yellow]...",
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
        rprint(
            f"Parsed [yellow]{country['country']['name']}[/yellow]:"
            f" {len(country['museums'])} museums",
        )

    if not OUTPUT_ROOT_JSON.is_dir():
        OUTPUT_ROOT_JSON.mkdir()
    json_output_file_name = f"{country['country']['name']}.json"
    json_output_file_path = Path(OUTPUT_ROOT_JSON) / json_output_file_name

    class CountryEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, Path):
                return str(obj)
            return super().default(obj)

    with json_output_file_path.open("w", encoding="utf-8") as json_output_file:
        json.dump(country, json_output_file, indent=2, cls=CountryEncoder)

if args.verbose:
    rprint(country_indexes)

# Generate per-country GPX files
# https://github.com/tkrajina/gpxpy/blob/dev/examples/waypoints_example.py
for country in country_indexes:
    gpx = gpxpy.gpx.GPX()
    gpx.creator = GPX_CREATOR
    gpx.name = f"Automuseums.info: {country['country']['title']}"
    gpx.description = f"Generated using {gpx.creator}"
    gpx.link = country["country"]["absolute_url"]

    if not args.omit_time:
        gpx.time = datetime.datetime.now(datetime.timezone.utc)

    def create_gpx_waypoint(museum, location_index):
        gpx_wps = gpxpy.gpx.GPXWaypoint()
        gpx_wps.latitude = museum["coordinates"][location_index]["lat"]
        gpx_wps.longitude = museum["coordinates"][location_index]["lon"]
        gpx_wps.symbol = "Museum"

        gpx_wps.name = museum["title"]
        if location_index > 0:
            gpx_wps.name = f"{gpx_wps.name} ({location_index + 1})"

        gpx_wps.description = museum["description"]

        # Prepend description with museum's original name in native language,
        # if available
        if museum["original_name"]:
            gpx_wps.description = f"{museum['original_name']}\n\n{gpx_wps.description}"

        # Append "Vehicle types" values,
        # what kinds of vehicles are exhibited
        if museum["vehicle_types"]:
            vehicle_types_item_list_formatted = "\n".join(
                [f"- {vt}" for vt in museum["vehicle_types"]],
            )
            vehicle_types_item_list_formatted = (
                f"Vehicle types:\n{vehicle_types_item_list_formatted}"
            )
            gpx_wps.description += f"\n\n{vehicle_types_item_list_formatted}"

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
                gpx_wps.description += f"\n\n{address_section_formatted}"
            else:
                # there is a museum in Iran
                # that has two coordinates but only one address
                # https://automuseums.info/iran/abadan-gasoline-house-museum
                address_item_list_formatted = "\n\n".join(museum["address"])
                address_section_formatted = f"Address:\n{address_item_list_formatted}"
                gpx_wps.description += f"\n\n{address_section_formatted}"

        # Append "E-mail" section if available
        if museum["email"]:
            if len(museum["email"]) > 1:
                email_item_list_formatted = "\n".join(
                    [f"{em}" for em in museum["email"]],
                )
                email_section_formatted = f"E-mail:\n{email_item_list_formatted}"
                gpx_wps.description += f"\n\n{email_section_formatted}"
            else:
                # most museums have only one email listed,
                # so collapse the entry into a single line
                gpx_wps.description += f"\n\nE-mail: {museum['email'][0]}"

        # Append "Phone" section if available
        if museum["phone"]:
            if len(museum["phone"]) > 1:
                phone_item_list_formatted = "\n".join(
                    [f"{ph}" for ph in museum["phone"]],
                )
                phone_section_formatted = f"Phone:\n{phone_item_list_formatted}"
                gpx_wps.description += f"\n\n{phone_section_formatted}"
            else:
                # most museums have only one phone number listed,
                # so collapse the entry into a single line
                gpx_wps.description += f"\n\nPhone: {museum['phone'][0]}"

        # GPX 1.1 Schema supports multiple links per waypoint,
        # https://www.topografix.com/gpx.asp
        # https://www.topografix.com/GPX/1/1/gpx.xsd
        # but gpxpy library assumes there can be only one link tag
        # https://github.com/tkrajina/gpxpy/issues/138
        gpx_wps.link = museum["permalink"]

        # Besides this gpxpy issue,
        # Google My Maps ignores <link> tags in Waypoints when importing,
        # so add all the links at the end of <desc> tag
        links = museum["links"].copy()
        links.append({"url": museum["permalink"], "title": "Automuseums.info"})
        links_section_plaintext = "\n".join(
            [f"{link['title']}: {link['url']}" for link in links],
        )
        gpx_wps.description += f"\n\n{links_section_plaintext}"

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
        rprint(f"Generated [cyan]{output_file_name}[/cyan]")
    else:
        rprint(
            f"Not generating [red]{output_file_name}[/red]"
            f" due to {len(gpx.waypoints)} museums"
            f" in [yellow]{country['country']['name']}[/yellow]",
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
            rprint(f"Loaded {CONFIG_GROUP_FILENAME}:")
            pprint(groups)
        except yaml.YAMLError as exc:
            rprint(exc)

    # Extend groups definition with "All Countries"
    groups["All countries"] = [country["name"] for country in country_list]

    # Load all generated per-country GPX files we need
    # for groups defined in YAML config file
    required_countries = list(set(chain.from_iterable(groups.values())))

    def load_country_gpx_data(country_name):
        country_file_name = f"{OUTPUT_FILENAME_PREFIX}{country_name}.gpx"
        file_path = Path(OUTPUT_ROOT_PER_COUNTRY) / country_file_name

        if not file_path.is_file():
            rprint(f"[red]Warning:[/red] missing [red]{country_file_name}[/red]")
            return None

        with file_path.open("r", encoding="utf-8") as gpx_file:
            return gpxpy.parse(gpx_file)

    per_country_data = {
        k: v
        for (k, v) in zip(
            required_countries,
            map(load_country_gpx_data, required_countries),
            strict=True,
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
            rprint(f"Generated [magenta]{group_output_file_name}[/magenta]")
        else:
            rprint(
                f"Not generating [red]{group_output_file_name}[/red]"
                f" due to {len(gpx.waypoints)} museums in {group_name}",
            )

humanized_execution_duration = humanize.precisedelta(
    datetime.datetime.now(datetime.timezone.utc) - start_datetime,
    minimum_unit="seconds",
    format="%.0f",
)
rprint(f"Completed in {humanized_execution_duration}")

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
        rprint(f"Reporting heartbeat to {betterstack_heartbeat_url}")
        response = requests.get(betterstack_heartbeat_url)
        if not response.ok:
            rprint("Failed!")
        rprint(f"Response: [{response.status_code}]")
