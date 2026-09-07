# Over 1.5 Goals — Match Analysis Tool

Analyses upcoming soccer fixtures, estimates the probability that each finishes
with **two or more goals**, compares that estimate against the bookmaker's price
to find genuine value, ranks the results, and writes them to one Excel workbook
per match date.

> **This is an analysis and recommendation tool.** It places no bets, performs no
> authenticated action on any sportsbook, and submits no forms. Its figures are
> statistical estimates, not predictions of certain outcomes.

---

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Fixture sources and the access policy](#fixture-sources-and-the-access-policy)
- [Usage](#usage)
- [The interactive prompts](#the-interactive-prompts)
- [Configuration](#configuration)
- [Methodology](#methodology)
- [The Excel workbook](#the-excel-workbook)
- [Data quality](#data-quality)
- [Exit codes](#exit-codes)
- [Development](#development)
- [Project layout](#project-layout)

---

## Requirements

Python 3.11 or newer. Developed and tested on Windows 11.

## Installation

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

Playwright needs its browser downloaded once. This is only required if you intend
to collect fixtures from the sportsbook directly — the manual CSV path below
needs no browser:

```bash
playwright install chromium
```

Then copy the configuration template and edit it if you want to change anything:

```bash
copy .env.example .env
```

Every dependency has a graceful fallback except `openpyxl`, which is genuinely
required. Without `rapidfuzz` the tool falls back to `difflib`; without
`python-dotenv` it uses a small built-in `.env` parser; without `pandas` it parses
season files with the standard library; without Playwright only the manual
fixture path works.

---

## Fixture sources and the access policy

Section 0 of the specification requires reviewing the sportsbook's terms and
`robots.txt` before implementing automated collection, and falling back to a
manually supplied fixture list if collection is not permitted.

**What the review found.** `https://gsb.ug/robots.txt` and
`https://gsb.ug/sportsbook/upcoming` both return **HTTP 403** to non-browser
clients. The site sits behind edge bot protection and its crawling policy cannot
be read, so permission to collect automatically **could not be established**.

**What this tool therefore does.**

- The **manual CSV path is the default and supported route** (`--fixtures`).
- The Playwright collector is fully implemented but **opt-in**, behind
  `--allow-scraping`, which you set only after satisfying yourself that the
  site's terms permit it.
- An explicit `Disallow` in `robots.txt` is **never** overridden, even with
  `--allow-scraping`.
- If a bot-protection challenge is served, collection **stops with a clear
  message**. The tool does not attempt to solve, evade, or work around such a
  control, and never performs an authenticated action.

### The fixtures CSV

Any of these column spellings are accepted, case-insensitively:

| Field | Accepted headers |
| --- | --- |
| League | `League`, `Competition`, `Tournament` |
| Home team | `Home Team`, `Home`, `HomeTeam` |
| Away team | `Away Team`, `Away`, `AwayTeam` |
| Kick-off | `Date` + `Time`, or `Kickoff EAT`, or `Kickoff UTC` |
| Over 1.5 | `Over 1.5`, `O1.5`, `odds_over_15` |
| Under 1.5 | `Under 1.5`, `U1.5`, `odds_under_15` |
| Optional | `Over 2.5`, `1`, `X`, `2`, `BTTS Yes`, `BTTS No` |

Times without an explicit zone are read as **EAT**. Only `League`, `Home Team`
and `Away Team` are required; a row with an unreadable kick-off is skipped with a
warning rather than aborting the run.

```csv
League,Home Team,Away Team,Date,Time,Over 1.5,Under 1.5
English Premier League,Man Utd,Arsenal,2026-09-08,18:00,1.30,3.40
```

**The Under 1.5 price matters.** It is what makes margin removal possible; without
it the row is flagged margin-unadjusted and its edge is overstated. Supply it
wherever you can.

---

## Usage

```bash
python main.py
```

Prompts for the three settings, then analyses today's fixtures in EAT. Pressing
Enter three times accepts the documented defaults.

```bash
python main.py --no-prompt --fixtures fixtures.csv
```

| Flag | Meaning |
| --- | --- |
| `--no-prompt` | Skip all prompts and use the resolved defaults |
| `--window HH:MM-HH:MM` | Explicit kick-off window, in EAT |
| `--next-hours N` | Kick-off within the next N hours |
| `--min-probability PCT` | Minimum model probability, in percent |
| `--min-odds ODDS` | Minimum Over 1.5 decimal odds |
| `--date YYYY-MM-DD` | Analyse a specific match date |
| `--fixtures PATH` | Use a manual fixture list (CSV) |
| `--output-dir DIR` | Override the output folder |
| `--dry-run` | Analyse without writing Excel |
| `--results PATH` | A local results CSV to compute statistics from |
| `--stats-providers LIST` | Comma-separated providers, or `none` |
| `--allow-scraping` | Opt in to collecting from the sportsbook |
| `--no-cache` | Ignore cached pages and feeds |
| `--dixon-coles [RHO]` | Apply the low-score correction (default rho 0.08) |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`--window` and `--next-hours` are mutually exclusive; supplying both is an
argument error. **Any setting given as a flag is not prompted for**, so supplying
all three promptable settings as flags is fully non-interactive without needing
`--no-prompt`.

### Examples

Tonight's fixtures only, tightened:

```bash
python main.py --fixtures fixtures.csv --window 19:00-23:00 --min-probability 80 --min-odds 1.35
```

The next six hours, unattended:

```bash
python main.py --fixtures fixtures.csv --next-hours 6 --no-prompt
```

Re-analyse a date already on disk under looser thresholds — the same workbook is
updated in place:

```bash
python main.py --fixtures fixtures.csv --date 2026-09-08 --min-probability 65 --no-prompt
```

---

## The interactive prompts

On an interactive run the tool asks for three settings and nothing else. Every
other threshold stays in configuration.

| Setting | Default |
| --- | --- |
| Analysis window | Full match day, `00:00`–`23:59` EAT |
| Minimum probability | `70.0` percent |
| Minimum odds | `1.20` decimal |

```
Over 1.5 Goals — Match Analysis Tool
Press Enter to accept the default shown in brackets.

Analysis window (EAT)
  [1] Full day, 00:00-23:59   (default)
  [2] Next N hours
  [3] Custom window, HH:MM-HH:MM
Choice [1]: 2
  Hours ahead [6]: 8
  -> Window: 14:30 - 22:30 EAT

Minimum probability %  [70.0]: 75
Minimum odds           [1.20]: 1.35

Analysing 2026-09-08 | window 14:30-22:30 EAT | min probability 75.0% | min odds 1.35
Proceed? [Y/n]:
```

Option `[2]` measures from the current time and clamps its end to the end of the
match day. Option `[3]` takes a 24-hour `HH:MM-HH:MM` range in EAT.

**Validation.** Bad input is rejected at the prompt with the reason, and
re-asked — it never crashes and never silently coerces. A minimum probability
above 95 is accepted with a warning. A minimum odds figure that would cross
`MAX_ODDS` is refused, because it would leave no admissible range. After three
consecutive invalid answers for one setting the default is used with a warning.
`Ctrl+C` exits cleanly with code `0`, writing nothing.

### Unattended runs

**The tool never blocks waiting for input when nobody is there.** Prompts are
skipped, and the resolved defaults used, when any of these holds:

- `--no-prompt` was passed
- stdin is not a TTY — piped input, redirected output, cron, CI, a scheduled task
- the `CI` environment variable is set

The effective configuration is printed and logged first, so an unattended run is
still self-documenting.

---

## Configuration

### Precedence

Each setting resolves from the first source that supplies it:

1. **A command-line flag** — highest priority
2. **The interactive answer**
3. **An environment variable or `.env` entry**
4. **The built-in default in `config.py`** — lowest priority

Pressing Enter at a prompt accepts the value that was shown without claiming to
have supplied it, so a figure that came from `.env` is still recorded as having
come from the environment.

The `Summary` sheet and the `Run Log` row record **the effective value of every
setting and the source it resolved from**, so a workbook always explains the
thresholds that produced it.

### Environment variables

`.env.example` documents every setting. Both the prefixed name
(`OVER15_MIN_ODDS`) and the bare name from the specification (`MIN_ODDS`) are
read; the prefixed form wins when both are present.

### Thresholds

```python
MIN_PROBABILITY  = 70.0   # percent          - promptable
MIN_ODDS         = 1.20   # decimal          - promptable
MAX_ODDS         = 5.00
MIN_VALUE        = 0.00   # model probability minus fair implied
MIN_H2H_MATCHES  = 3
MIN_FORM_MATCHES = 5

WINDOW_START     = "00:00"  # EAT            - promptable
WINDOW_END       = "23:59"  # EAT            - promptable
DEFAULT_NEXT_HOURS = 6      # used by prompt option [2]
```

A match is excluded from the recommendations when its kick-off falls outside the
window, its historical sample is too thin, the recent statistics materially
contradict the estimate, its probability is below the minimum, its data is
unreliable or self-contradictory, or its price is outside the range or its edge
negative. **Excluded matches still appear in `All Matches` with the reason** —
they are filtered from the recommendations, not from the record.

---

## Methodology

### 1. Expected goals

A **Poisson goals model**, the standard approach for total-goals markets.
Expected goals are estimated for each side, summed, and the tail is read off the
distribution:

```
P(0 goals)  = exp(-λ)
P(1 goal)   = λ * exp(-λ)
P(Over 1.5) = 1 - exp(-λ) * (1 + λ)
```

λ = 2.7 gives about 75%; λ = 3.0 gives about 80%. A **Dixon-Coles** low-score
correction is implemented and available via `--dixon-coles`; it is off by
default so the closed form above holds exactly.

### 2. Blending the evidence

Neither λ comes from a single statistic. Five components each propose an
expected-goals pair, combined with these weights:

| Input | Weight |
| --- | --- |
| Recent form (goals scored / conceded) | 30% |
| Home / away split performance | 20% |
| Recent Over 1.5 rate | 20% |
| Head-to-head | 15% |
| League baseline | 10% |
| Supporting context adjustments | 5% |

**A component with no data drops out and its weight is redistributed** across
those that do have data, so a missing input never behaves like a zero. Supporting
context is a multiplier applied at its own weight, which is what keeps
speculative information from materially moving the estimate.

A league baseline **alone** is never treated as evidence: at least one
team-specific component is required, or the fixture is excluded. A league average
dressed up as a match estimate would be a fabrication.

**Observed rates are Laplace-smoothed before they are inverted into expected
goals.** Ten Overs in ten matches is an observed rate of 1.0, which inverted
literally implies about nine expected goals — an unbounded extrapolation from a
bounded sample that would then dominate the blend. Smoothing turns 10/10 into
11/12, an expected total near 3.9, and the correction weakens as the sample
grows.

### 3. Shrinkage

Small samples are pulled toward the league baseline by `n / (n + k)`, where `n` is
the effective sample size and `k` is `shrinkage_strength` (default 5). A team with
three recorded matches is not treated with the confidence of one with twenty.

### 4. Removing the bookmaker's margin

`1 / decimal_odds` **includes the bookmaker's margin** and so overstates the true
implied probability. Comparing a model estimate directly against it biases every
result toward "no value", so the margin is removed first using the Over/Under 1.5
pair:

```
overround    = (1 / odds_over) + (1 / odds_under)
implied_fair = (1 / odds_over) / overround
```

All three figures are reported, and **`implied_fair` is what every value
calculation uses**. Where the Under 1.5 price is unavailable the raw figure is
used and the row is flagged margin-unadjusted.

### 5. Value and expected value

```
Value = model_probability - implied_fair
EV    = (model_probability * decimal_odds) - 1
```

`Value` is the probability edge in percentage points; `EV` is the expected return
per unit staked, and is the more comparable figure across different odds levels.
Both are reported. A Kelly fraction is shown for information only — **the tool
recommends no stake sizes**.

### 6. The ranking score

Ranking is **never on odds alone and never on probability alone**:

```
score = (W_PROBABILITY * normalised_probability) + (W_VALUE * normalised_value)
```

with `W_PROBABILITY = 0.60` and `W_VALUE = 0.40` by default, both configurable
and required to sum to 1.0.

Both components are min-max normalised to 0–1 **across the analysed set** before
weighting, so neither dominates through scale alone — raw value edges live in the
tenths while probabilities live near 1. Where a component has no spread across
the set it contributes a neutral 0.5 rather than an arbitrary 0 or 1. Ties break
on higher model probability, then on higher confidence.

The normalisation set is the **full retained set for the date**, not just the
current run's fixtures, so re-running with a narrower window does not silently
rescale everything.

### 7. Categories

| Category | Meaning |
| --- | --- |
| **TOP PICK** | ≥ 80% probability, non-negative value, good data completeness, at least Medium confidence |
| **HIGH-ODDS VALUE PICK** | Priced at 2.00 or above with an edge of at least 5 points |
| **GOOD PICK** | Everything else that qualifies |

Boundaries are configurable. **Categories are never padded.** If three matches
meet the criteria, three are reported. If none do, none are — and the console
says so, naming the binding constraint.

### 8. Confidence

`High`, `Medium` or `Low`, driven by the effective sample size, how many
independent components contributed, how far apart they were, and data
completeness.

Component disagreement is measured **relative to the mean expected total**, not
in goals. An absolute threshold would rate high-scoring fixtures Low for the same
relative agreement — a 0.9-goal spread is close agreement around an expected 4.0
and poor agreement around an expected 1.8 — and since TOP PICK requires at least
Medium confidence, that would quietly starve the top category in exactly the
leagues this tool is looking for.

---

## The Excel workbook

Results go to `results/YYYY-MM-DD.xlsx`, named by **the match date alone**. The
window, the thresholds and the run time never affect the filename.

**For a given date there is exactly one workbook.** If it exists it is opened and
updated in place. No `2026-09-08 (1).xlsx`, no `_v2`, no timestamped variant —
not even when the target is locked.

Rows are upserted on a **match key**, `sha1(league | home | away | kickoff_utc)`
computed from normalised names, so two sources spelling a club differently share
one row.

| Situation | Behaviour |
| --- | --- |
| Key already present | Overwritten with the newly computed values |
| Key is new | Appended |
| Kick-off outside this run's window | **Retained**, status `Outside analysis window` |
| Absent from this run entirely | **Retained**, status `Not in latest run`, `Last Updated` untouched |

**Rows are never silently deleted.** A postponed fixture, or one that falls
outside a narrowed window, stays visible with its history intact. Each row carries
`First Seen (UTC)` and `Last Updated (UTC)`, and the ranking is recomputed across
the full retained set after every upsert.

### Sheets

| Sheet | Contents |
| --- | --- |
| `Summary` | Run metadata, counts per category, the effective configuration with the source of each value, and the disclaimer |
| `Ranked Selections` | Qualifying matches, best to weakest — rebuilt every run under the current thresholds |
| `All Matches` | Every fixture, including excluded ones, with an `Exclusion Reason` and the full statistics |
| `Run Log` | One row per execution: timestamps, duration, window, thresholds, counts, warnings, errors |
| `Data Sources` | Per-statistic provenance: source, endpoint, retrieval time, and missing-data notes |

Headers are frozen, auto-filter is on, percentages are formatted as percentages,
odds to two decimals, and a colour scale runs across the probability and value
columns. **Sheets the tool does not own are preserved**, so notes you add by hand
survive a re-run.

### Write safety

Writes are atomic: the workbook is built beside the target and moved onto it with
`os.replace`, so a crash mid-write cannot leave a corrupt file. If the target is
open in Excel — common on Windows — the write retries with backoff and then fails
with an actionable message. It does **not** fall back to a differently named
file; that would break the one-file-per-date rule. Cached results make the re-run
inexpensive.

---

## Data quality

- **No statistic is ever fabricated.** A figure a source cannot supply is marked
  unavailable, which lowers `data_completeness` and `confidence` and can exclude
  the fixture outright.
- Every statistic is attributed to its source, with a retrieval time.
- Team names are normalised through an alias map (`data/team_aliases.json`) plus
  fuzzy matching. Below the similarity threshold **no link is assumed** — the
  fixture is flagged for review instead.
- **A first team is never linked to its reserve, youth, or women's side.** Names
  differing by a squad qualifier (`B`, `II`, `U21`, `Ladies`, `Castilla`, …) are
  refused outright, because blending "Barcelona" with "Barcelona B" would feed
  the wrong team's results into the model.
- A competition that names a country only maps to that country's data, so
  "Uganda Premier League" is never analysed on English results.
- Fixtures are deduplicated on the match key.
- A failure on one match is logged and skipped without aborting the run.
- Logging is structured: readable `key=value` lines on the console, JSON lines to
  `logs/over15.log`.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success — including a clean cancellation at the prompt |
| `1` | Completed, but with recoverable errors (see the log and `Run Log`) |
| `2` | Fatal — nothing useful was produced |

---

## Development

```bash
pytest
```

```bash
ruff check .
```

```bash
ruff format .
```

The suite covers the Poisson calculation against known values and against SciPy,
margin removal and the value/EV metrics, the ranking score and categories,
team-name normalisation, precedence resolution, the non-interactive fallback,
prompt validation, window filtering, and the full Excel upsert path — including
that no duplicate file is ever produced for a date.

No test touches the network.

## Project layout

```
main.py                 CLI entry point and orchestration
config.py               configuration, defaults, thresholds
prompts.py              interactive setup and precedence resolution
gsb_scraper.py          fixture and odds collection
statistics_provider.py  external statistics, with caching
probability_model.py    Poisson model and blending
odds.py                 margin removal, value, EV
ranking.py              combined score, categories, qualification
excel_writer.py         workbook create / upsert / format
reporting.py            console report
models.py               shared domain records
utils/
    naming.py           team-name normalisation and match keys
    logging.py          structured logging
data/
    team_aliases.json   alias map, extends the built-in one
tests/
requirements.txt
.env.example
```

## Licence

MIT — see [LICENSE](LICENSE).
