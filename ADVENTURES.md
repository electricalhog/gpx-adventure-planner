# Adventure planning workflow

Personal workflow notes for planning bike/run adventures with this MCP server.

## Prompt recipe
Describe: start, intensity (easy / endurance / hard), duration or distance, stops wanted
("artisan coffee every ~40 km", "bag a peak"), and whether ferries/border crossings are OK.

Typical tool sequence: `geocode` -> `route` (profile `trekking` / `gravel` / `fastbike-lowtraffic`)
-> `find_pois_along_route` (cafe, peak, bakery, viewpoint) -> add as waypoints and re-`route`
-> `estimate_duration` -> `save_gpx` (writes `data/routes/<name>.gpx`).
`hazards` in `route` output flags ferries and barriers; check borders/passports manually.

## Road-rider mode: `avoid_unpaved`
`route(..., avoid_unpaved=True)` re-routes (up to 3 rounds) around gravel, tracks, paths and long
service-way shortcuts (cemetery / parking-lot cut-throughs) by dropping nogo circles on them.
The result's `avoid_unpaved` block reports distance and busy-road share before/after, because
removing gravel often moves you onto busier roads. Prefer `fastbike-lowtraffic` with this flag over `gravel`.

## Upload to Coros
Use the Coros MCP tool `uploadRouteFile` with the GPX text, `fileName` ending in `.gpx`,
and `routeType`: 1 = bike, 2 = on foot. Large tracks should be simplified (~600 points) first.
Processing is async; the same file can't be re-imported until the existing route is deleted in the app.

## Climb analysis from a ride (FIT file)
Download the ride's FIT file from your watch service, then:

```sh
uv sync --extra fit
uv run gpx-climb-map ride.fit --title "My climb" --serve            # highest point of the ride
uv run gpx-climb-map ride.fit --summit 44.21,-72.93 --window-km 16  # pick the summit, widen the approach
```

It writes `<ride>.climb.html`: a map of the climb colored by heart rate, power, speed, climb rate,
gradient, cadence or elevation, linked to an elevation-profile chart, plus summary stats (gain, time,
VAM, avg/max HR and power). `--serve` opens it at `http://localhost:8765/...`; use that, not a
`file://` URL, because OpenStreetMap's tile servers block requests that carry no referrer.
`--window-km` is how far back from the summit to look for the climb's lowest point (default 8).
