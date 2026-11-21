# Adventure planning workflow

Personal workflow notes for planning bike/run adventures with this MCP server.

## Prompt recipe
Describe: start, intensity (easy / endurance / hard), duration or distance, stops wanted
("artisan coffee every ~40 km", "bag a peak"), and whether ferries/border crossings are OK.

Typical tool sequence: `geocode` -> `route` (profile `trekking` / `gravel` / `fastbike-lowtraffic`)
-> `find_pois_along_route` (cafe, peak, bakery, viewpoint) -> add as waypoints and re-`route`
-> `estimate_duration` -> `save_gpx` (writes `data/routes/<name>.gpx`).
`hazards` in `route` output flags ferries and barriers; check borders/passports manually.

## Upload to Coros
Use the Coros MCP tool `uploadRouteFile` with the GPX text, `fileName` ending in `.gpx`,
and `routeType`: 1 = bike, 2 = on foot. Large tracks should be simplified (~600 points) first.
Processing is async; the same file can't be re-imported until the existing route is deleted in the app.
