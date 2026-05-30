# scrub-portco fixtures

These files deliberately contain hits that exercise the scrub-portco patterns.
They are excluded from the catalog scan via `exclude_paths` in
`bin/scrub-portco-patterns.yml`. Tests in `bin/scrub_portco_test.py` point
the scrubber at this directory to verify each pattern fires.

When adding a pattern to the catalog, add a sample hit here (one file per
category) and extend the test that points at that file.
