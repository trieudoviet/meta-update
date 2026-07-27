# Meta Update

Source project for **Update Checker**.

## Project layout

```text
bin/       Project-local launcher
src/       Python 3.12 core
config/    TOML software inventory
systemd/   User service and timer
tests/     Unit tests
docs/      Vietnamese usage guide and implementation summary
```

## Run from this checkout

```bash
./bin/check-all-updates --quick
./bin/check-all-updates --quick --json
./bin/check-all-updates --quick --dry-run
```

## Test

```bash
python3 -m unittest discover -s tests -v
```

See [docs/Hướng-dẫn-check-all-updates.md](docs/Hướng-dẫn-check-all-updates.md)
for full usage and operational notes.
