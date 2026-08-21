# AGENTS.md

Freqtrade — crypto trading bot (fork, branch `tony`). Two installable packages in one repo:
- `freqtrade/` — the bot (`freqtrade.main:main` is the CLI entrypoint)
- `ft_client/` — `freqtrade-client`, a **separate PyPI package** and a dependency of freqtrade. Keep its version in sync with `freqtrade_client/__version__` (CI runs `python build_helpers/freqtrade_client_version_align.py`).

## Answering doc questions: where to look

Docs live in `docs/*.md` (nav in `mkdocs.yml`; serve locally with `mkdocs serve` after `pip install -r docs/requirements-docs.txt`). This branch is a fork — trust local docs, not freqtrade.io (they drift).

| Question | Read |
|---|---|
| CLI command/flags | `docs/commands/<cmd>.md` — **generated** from argparse, authoritative for flags |
| Strategy development | `strategy-101.md`, `strategy-customization.md`, `strategy-callbacks.md`, `stoploss.md`, `plugins.md`, `strategy-advanced.md` |
| Backtesting / analysis of results | `backtesting.md`, `advanced-backtesting.md`, `data-analysis.md` (notebooks), `strategy_analysis_example.md` |
| Hyperopt | `hyperopt.md`, `advanced-hyperopt.md` |
| FreqAI | `freqai.md`, `freqai-configuration.md`, `freqai-parameter-table.md`, `freqai-feature-engineering.md`, `freqai-running.md`, `freqai-reinforcement-learning.md`, `freqai-developers.md` |
| Configuration / settings | `configuration.md`, plus `config_examples/*.json` (binance, kraken, freqai, full) |
| Live / dry-run operation | `bot-usage.md`, `bot-basics.md`, `telegram-usage.md`, `rest-api.md`, `webhook-config.md`, `freq-ui.md` |
| Data download / conversion / listing | `data-download.md`, `utils.md` |
| Plotting, lookahead/recursive analysis | `plotting.md`, `lookahead-analysis.md`, `recursive-analysis.md` |
| Exchange-specific behavior | `exchanges.md`, `leverage.md`, `advanced-orderflow.md` |
| Anything else | `faq.md`, `trade-object.md`, `sql_cheatsheet.md`, `deprecated.md`, `updating.md`, `producer-consumer.md` |

### Verify doc claims against code

Docs are prose and can be stale. When a doc says something concrete, confirm in the code:
- strategies → `freqtrade/strategy/` (interface, callbacks, hyperoptable `Parameter`), templates in `freqtrade/templates/sample_strategy.py`
- CLI behavior → `freqtrade/commands/*.py` + `freqtrade/commands/arguments.py`
- config options → `freqtrade/config_schema.py` (single source of truth; `build_helpers/schema.json` is generated)
- backtest/hyperopt semantics → `freqtrade/optimize/`
- freqai → `freqtrade/freqai/`
- telegram/API → `freqtrade/rpc/`
- exchange quirks → `freqtrade/exchange/`
- runtime loading of user strategies/losses/models → `freqtrade/resolvers/*`

## Doc conventions (when editing docs)

- **Never edit `docs/commands/*.md` by hand** — regenerated from CLI help by `python build_helpers/create_command_partials.py`; change the argparse code instead (CI fails on drift).
- Shared fragments live in `docs/includes/*.md` (pairlists, protections, pricing, exchange-features…) and are pulled in via `--8<-- "includes/x.md"` (pymdownx snippets). Update the include, not the copies.
- Use mkdocs-material admonitions (`!!! note "..."`); images go in `docs/images/`; generated command docs are excluded from the site build (`exclude_docs` in `mkdocs.yml`).
- CI checks: `tests/test_docs.sh` (markup sanity), `mkdocs build`, codespell. Run `pre-commit run -a`.

## EPUB build (`build_helpers/build_epub.py`)

Converts the mkdocs docs to an EPUB (default `~/Documents/freqtrade-docs.epub`):

```bash
python build_helpers/build_epub.py            # build + update Calibre library
python build_helpers/build_epub.py --no-calibre   # file-only build
```

- Zero-arg run also updates the "Freqtrade" book in the Calibre library (path read from `~/.config/calibre/global.py.json`): replaces the format and refreshes metadata from the epub OPF. If the `calibre-server` systemd service is active it is stopped first and restarted after (`sudo`, prompts for password). Halts with a warning if the Calibre GUI or ebook viewer is open (they lock the library).
- **Highlight preservation**: Calibre stores annotations keyed to (book, internal file, CFI). The build is deterministic (fixed zip timestamps/order, constant book UUID); chapters whose rendered XHTML is byte-identical keep their old filename (content-hash match, covers renamed pages) so highlights/notes survive. Changed chapters orphan their old highlights; removed pages drop their file. Verify with `tests/test_build_epub.py`.
- Metadata: title/description/language from `mkdocs.yml`; creator/publisher/rights/tags via flags; published date = latest release (git tag, else newest `bump version to X.Y` commit, else docs commit).
- Dependencies: `beautifulsoup4` (in `requirements-tony.txt`), mkdocs-material (theme markup), pygments. Known limits: MathJax stays as raw LaTeX; a changed-and-renamed page breaks cross-links from unchanged pages.

## Quick reference for hands-on runs

- `user_data/` is the live userdir (gitignored): `data/binance/futures/*.feather` candles, empty `strategies/`, `backtest_results/`, `hyperopt_results/`, `logs/`. DBs `tradesv3.sqlite` (live) / `tradesv3.dryrun.sqlite` (dry-run).
- Test fixture config + feather data in `tests/testdata/` (`config.tests.json`) — run backtests/hyperopt offline with `--datadir tests/testdata` (what CI does).
- `freqtrade new-strategy -s MyStrategy` scaffolds into `user_data/strategies/`.
- Live vs dry-run: config `dry_run: true` (or `--dry-run`); `freqtrade trade -c <config>`.

## FreqAI model quirk & FreqAI hyperopt

- **Model training parameters live in the config, not the strategy**: `freqai.model_training_parameters` (e.g. `learning_rate`, `n_estimators`, `num_leaves`) in the config file (docs: `freqai-running.md` "Controlling the model learning process"). They are **not** hyperoptable — strategy `Parameter`s cannot touch them. Tuning them = edit the config + rerun the backtest.
- **Trained models and predictions are cached per `identifier`** (verified in `freqai_interface.py` backtesting loop: `if not self.model_exists(dk): train else: load`). A rerun with the same config/identifier/timerange loads models from `user_data/models/<identifier>/` and reuses saved predictions instead of retraining. **Changing `learning_rate` (or any model/feature/target setting) with the same `identifier` silently reuses stale models — always bump `identifier` to force retraining.** `save_backtest_models: false` skips saving new models (metadata only) but still loads any that already exist.
- **Canonical backtesting command for this branch** (from zsh history — the run that produced the latest results in `user_data/backtest_results/`): `freqtrade backtesting --strategy FreqaiExampleHybridStrategy --strategy-path freqtrade/templates --config config_examples/config_freqai.example.json --freqaimodel LightGBMClassifier --timerange 20210101-20260101`
- **FreqAI hyperopt** (docs: `freqai-running.md` "Hyperopt"): same command as regular hyperopt plus `--freqaimodel`. Restrictions: `--analyze-per-epoch` is incompatible; indicators in `feature_engineering_*()` / `set_freqai_targets()` and model parameters cannot be hyperopted. Only hyperopt entry/exit thresholds and criteria — parameters that do not change predictions — because hyperopt runs on the cached predictions, not on retrained models.

## Dev workflow

- Env: Nix flake + direnv (`use flake`); `.venv` (Python 3.13, editable install, TA-Lib present). Personal extras in `requirements-tony.txt`. Install: `pip install -r requirements-dev.txt && pip install -e ft_client/ && pip install -e .`
- Tests: `pytest tests/optimize/test_backtesting.py::test_foo` (addopts force `--dist loadscope`). CI: `pytest --random-order -n auto`; online tests need `--longrun` + `CI_WEB_PROXY` env. Never commit tradesv3*.sqlite or user_data content.
- Lint: `ruff check` + `ruff format --check`; typecheck: `mypy freqtrade scripts tests` (tests/templates lenient per pyproject overrides).
- Key dirs: `freqtrade/optimize/` (backtesting, hyperopt), `freqtrade/freqai/`, `freqtrade/exchange/` (ccxt wrappers), `freqtrade/rpc/` (API server, telegram), `freqtrade/commands/` (CLI subcommands).
- Changing CLI help, config schema, or templates? Run both build_helpers scripts — CI's repository-cleanliness check rejects drift.
