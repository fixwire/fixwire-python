# Contributing

Thanks for helping. Issues and pull requests are welcome here.

The SDK is developed together with the Fixwire server, which keeps the SDKs
in step with the protocol and with each other; this repository is updated
from there, with its full history. We apply an accepted pull request there,
with you as its author, and it comes back here with the next update.

1. **Open an issue first** for anything larger than a fix, so we can agree
   on the approach before you spend time on it.
2. **Keep pull requests small**, one change each, with tests.
3. **Run the checks** before you push. With [uv](https://docs.astral.sh/uv/),
   one command sets up everything from `uv.lock`: Python 3.14 (from
   `.python-version`), the SDK, the frameworks the tests drive and the
   checkers CI runs.

   ```sh
   uv sync --all-extras
   uv run ruff format --check . && uv run ruff check . && uv run pytest -q
   uv run mypy && uv run pyright
   ```

   The tests run on every supported Python, for example
   `uv run --python 3.10 --all-extras pytest -q`. The type checks need 3.11
   or newer. A dependency you add goes in `pyproject.toml`; then run
   `uv lock` and commit `uv.lock` with it.

4. **Redaction stays exact.** Secrets and personal data never leave a device
   unmasked, and the SDK masks exactly what the server masks. Changes there
   keep the redaction tests passing.

Commit messages say what changed and why, in plain words.
