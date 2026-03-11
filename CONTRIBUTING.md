# Contributing to Oncofiles

Thanks for your interest in contributing!

## Development setup

```bash
git clone https://github.com/instarea-sk/oncofiles.git
cd oncofiles
uv sync --extra dev
```

## Running tests

```bash
uv run pytest
uv run ruff check
uv run ruff format --check
```

## Code style

- Python 3.12+, async-first
- Ruff for linting and formatting
- Pydantic for data models
- Type hints on all public functions

## Pull requests

1. Fork the repo and create a branch from `main`
2. Add tests for new functionality
3. Ensure all tests pass and linting is clean
4. Submit a pull request with a clear description

## Reporting issues

Open an issue on [GitHub](https://github.com/instarea-sk/oncofiles/issues) with:
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
