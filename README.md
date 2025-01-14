# Automuseums.info content as GPX files

I like planning travel routes around automotive/motorcycle museums,
and last winter i wasted too much time manually pinning museum locations in [Google My Maps](https://www.google.com/maps/d/).

This repo is an effort to have content of [Automuseums.info](https://automuseums.info) website as GPX files.

Download generated GPX files here: [https://automuseums.tldrtravel.info](https://automuseums.tldrtravel.info)

## Example use

Display help:
`.venv/bin/python3 cli.py --help`

Generate all possible output GPX files:
`.venv/bin/python3 cli.py --group`

Regenerate output files with current cache:
`.venv/bin/python3 cli.py --group '--cache-ttl-countrylist' '999' '--cache-ttl-museumlist' '999' '--cache-ttl-museumpage' '999'`

Run in lowprofile mode and regenerate grouped GPX:
`.venv/bin/python3 cli.py --lowprofile --group`

## Development environment

### venv-based

To create venv:
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
