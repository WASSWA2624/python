# python

A Python starter project using a `src/` layout, `pyproject.toml`, pytest, and ruff.

## Requirements

- Python 3.11 or newer

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -e ".[dev]"
```

## Usage

```bash
app                  # Hello, world!
app Wilson           # Hello, Wilson!
python -m app Wilson # same, via the module
```

## Development

```bash
pytest               # run tests
ruff check .         # lint
ruff format .        # format
```

## Layout

```
src/app/       package source
  core.py      application logic
  cli.py       argparse entry point
tests/         pytest suite
```

## License

MIT — see [LICENSE](LICENSE).
