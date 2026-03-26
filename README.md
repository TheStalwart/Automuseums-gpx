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

## Lowprofile mode

`--lowprofile` mode is designed to be executed periodically (e.g. via cron)
and keep output GPX files up to date with upstream website data.

It will pick a single country with oldest (or absent) output
and refresh data on its museums, including `grouped-by-region` output files
if `--group` option is enabled.

With default `--request-delay` execution can take more than 2 hours for countries like United States.

## Sentry.io SDK integration

To enable [Sentry.io SDK](https://docs.sentry.io/platforms/python/),
create `sentry.dsn` file with Client Key (DSN) in the root of the project.

## Better Stack heartbeat monitor

To enable [Better Stack heartbeat monitor](https://betterstack.com/docs/uptime/cron-and-heartbeat-monitor/),
create `heartbeat.url` file with heartbeat URL in the root of the project.

## Development environment

- Python >=3.10

### venv-based

To create venv:
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
