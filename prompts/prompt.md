# Over 1.5 Goals — Match Analysis Tool

**Specification for a Python application**

Build a Python program that analyses **upcoming soccer matches** listed on the GSB Uganda sportsbook and identifies those with the strongest statistical likelihood of finishing with **Over 1.5 total goals** (two or more goals).

Source page: `https://gsb.ug/sportsbook/upcoming`

---

## 0. Scope and non-goals

**In scope**

- Collecting upcoming fixtures and odds for the current day
- Gathering supporting historical statistics from public football-data sources
- Estimating an Over 1.5 probability per match with a documented model
- Comparing that estimate against bookmaker odds to identify value
- Ranking, categorising, and persisting the results to Excel

**Explicitly out of scope**

- **The program must never place, stage, or authorise a bet.** It performs no authenticated actions on the sportsbook and submits no forms. It is an analysis and recommendation tool only.
- No account creation, login, or credential handling of any kind.
- No circumvention of bot detection, CAPTCHAs, or access controls.

**Before implementation**, review the GSB Uganda terms of service and `robots.txt`. If automated collection is disallowed, the fixture list must be supplied manually (see §3.4) rather than scraped. Document whichever path is taken.

All output is a statistical estimate. The program must never present a selection as a guaranteed outcome, and every report must carry a short disclaimer to that effect.

---

## 1. Definitions

| Term | Definition |
| --- | --- |
| **Match day** | The calendar day in **Africa/Kampala (EAT, UTC+3)**, the sportsbook's local timezone. "Today's matches" means fixtures kicking off between 00:00:00 and 23:59:59 EAT. |
| **Analysis window** | The kick-off time range actually analysed within the match day. Defaults to the whole day; narrowed at startup (§2). A fixture qualifies when its kick-off falls inside the window. |
| **Over 1.5** | The match finishes with a total of 2 or more goals, counting full time (90 minutes plus stoppage) only — no extra time, no penalties. |
| **Match key** | A stable identifier for a fixture: `sha1(normalised_league + "\|" + normalised_home + "\|" + normalised_away + "\|" + kickoff_utc_iso)`. Used for deduplication and for updating existing Excel rows. |
| **Upcoming** | Kick-off is strictly in the future at the moment of collection. Live and completed matches are excluded. |

Store every timestamp internally in **UTC** and render it in EAT for display. This avoids off-by-one-day errors around midnight.

---

## 2. Runtime configuration and interactive setup

The program is usable with no arguments at all. On an interactive run it **optionally prompts** for three settings; pressing Enter at any prompt accepts the default, so a user who just wants sensible behaviour can press Enter three times.

### 2.1 Promptable settings

| Setting | Default | Meaning |
| --- | --- | --- |
| **Analysis window** (time frame) | Full match day, `00:00`–`23:59` EAT | Only fixtures kicking off inside this window are analysed |
| **Minimum probability** | `70.0` percent | A match must reach this model probability to be recommended |
| **Minimum odds** | `1.20` decimal | A match must be priced at or above this to be recommended |

These are the only prompted values. Every other threshold in §9 stays in configuration.

### 2.2 Precedence

Each setting resolves from the first source that supplies it:

1. An explicit command-line flag — highest priority
2. The interactive answer
3. An environment variable or `.env` entry
4. The built-in default in `config.py` — lowest priority

**A setting supplied by flag is not prompted for.** Running with all three flags is therefore fully non-interactive without needing `--no-prompt`.

### 2.3 The prompt

When the run is interactive and prompting is not disabled, ask for each unresolved setting, showing its default in brackets. Then echo the resolved configuration and ask for a single confirmation before work begins.

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

Analysing 2026-09-07 | window 14:30-22:30 EAT | min probability 75.0% | min odds 1.35
Proceed? [Y/n]:
```

Option `[2]` measures from the current time, so its window end is clamped to the end of the match day. Option `[3]` accepts a 24-hour `HH:MM-HH:MM` range in EAT.

### 2.4 Validation

Reject bad input at the prompt rather than failing later:

- **Minimum probability** — must be greater than 0 and at most 100. Warn above 95, where almost nothing will qualify.
- **Minimum odds** — must be at least 1.01 and below `MAX_ODDS`. Reject with an explanatory message if the two would cross, leaving no admissible range.
- **Analysis window** — the end must follow the start, hours-ahead must be positive, and the window must intersect the match day. Warn if the window has already passed, since no fixture can match.

Invalid input re-prompts with the reason; it must never crash or silently coerce. After three consecutive invalid attempts for one setting, fall back to its default with a warning and continue. `Ctrl+C` at any prompt exits cleanly with code `0`, writing nothing.

### 2.5 Non-interactive execution

**The program must never block waiting for input in an unattended run.** Skip all prompts and use the resolved defaults when any of the following is true:

- `--no-prompt` is passed
- stdin is not a TTY — piped input, output redirection, cron, CI, a scheduled task
- the `CI` environment variable is set

In that case print the effective configuration to the log before starting, so an unattended run is still self-documenting.

### 2.6 Recording the configuration

Record the effective value of every setting **and the source it resolved from** (flag, prompt, environment, default) in the `Summary` sheet and in the `Run Log` row for that run (§10.4). A workbook must always explain the thresholds that produced it.

Because thresholds can differ between runs against the same date, `Ranked Selections` is **rebuilt on every run** from the full retained match set using the current thresholds. A fixture that qualified under a looser earlier threshold and no longer qualifies moves back to `All Matches` with its exclusion reason; nothing is lost, and the sheet always reflects one coherent set of criteria.

---

## 3. Collect upcoming matches

### 3.1 Retrieval

The page loads fixtures dynamically via JavaScript, so plain `requests` is insufficient. Use a browser-automation framework — **Playwright** (preferred) or Selenium — and wait for the fixture list to finish rendering before parsing. Expand any lazy-loaded or paginated sections so that **all** available soccer fixtures for the day are captured, not a partial first page.

Collect the full day's fixtures first, then apply the analysis window (§2.1) as a filter. Fixtures falling outside the window are retained in `All Matches` with the status `Outside analysis window` rather than being discarded — narrowing the window must not erase what an earlier, wider run already found.

### 3.2 Fields to capture

For every match:

- League / competition
- Match date (EAT and UTC)
- Kick-off time (EAT and UTC)
- Home team
- Away team
- Over 1.5 goals odds (decimal)
- Under 1.5 goals odds (decimal) — required for margin removal in §6
- Any other available markets worth recording (1X2, Over/Under 2.5, BTTS)
- `collected_at` timestamp

### 3.3 Politeness and resilience

- Set a descriptive User-Agent and a request rate that does not burden the site.
- Cache the raw page response so that re-runs during development do not re-fetch.
- Apply explicit timeouts and bounded retries with exponential backoff.
- Exclude any fixture that has already kicked off or finished.

### 3.4 Manual fallback

Support `--fixtures <path.csv>` to load fixtures from a CSV using the same columns as §3.2. This keeps the analysis pipeline usable if the site is unreachable, its markup changes, or automated collection is not permitted.

---

## 4. Gather external statistics

For every fixture, collect supporting statistics from reliable, publicly available football-data sources. Record the source and retrieval time for each statistic.

### 4.1 Head-to-head

- Number of previous meetings considered
- Percentage ending Over 1.5
- Average total goals
- Count of meetings with 2+ goals
- Most recent results

Weight head-to-head meaningfully but never as the sole input. Discount meetings older than roughly three years and those played under materially different circumstances (different division, heavily changed squads).

### 4.2 Recent form

Over the last 5–10 matches for each team:

- Over 1.5 percentage
- Average goals scored
- Average goals conceded
- Average total goals
- Count of matches with 2+ goals

### 4.3 Home and away splits

The home team's recent **home** matches and the away team's recent **away** matches, each with its own Over 1.5 percentage and average total goals.

### 4.4 League context

- League average goals per match
- League Over 1.5 percentage
- Recent scoring trend within the league

League baselines matter most when team-level samples are small, and should carry more weight as sample size falls (§5.3).

### 4.5 Supporting context

Where reliable data exists: injuries and suspensions, expected lineups, attacking and defensive strength ratings, notable recent changes in performance, match importance, and competition stage.

Speculative or unsourced news must not materially move the estimate.

---

## 5. Estimate the Over 1.5 probability

### 5.1 Method

Use a **Poisson goals model**, the standard approach for total-goals markets, which yields a defensible probability rather than an arbitrary score.

1. Estimate expected goals for each side — `λ_home` and `λ_away` — from attacking strength, opposing defensive strength, home advantage, and the league baseline.
2. Let `λ = λ_home + λ_away` be the expected total goals.
3. Then:

```
P(0 goals)  = exp(-λ)
P(1 goal)   = λ * exp(-λ)
P(Over 1.5) = 1 - exp(-λ) * (1 + λ)
```

For example, `λ = 2.7` gives an Over 1.5 probability of about 75%; `λ = 3.0` gives about 80%.

A Dixon-Coles low-score correction may be applied, and a bivariate or negative-binomial variant may be substituted, provided the choice is documented.

### 5.2 Blending the evidence

Derive `λ_home` and `λ_away` from a weighted blend rather than any single statistic. Suggested starting weights, all configurable:

| Input | Weight |
| --- | --- |
| Recent form (goals scored / conceded) | 30% |
| Home / away split performance | 20% |
| Recent Over 1.5 rate | 20% |
| Head-to-head | 15% |
| League baseline | 10% |
| Supporting context adjustments | 5% |

### 5.3 Shrinkage

With small samples, pull the estimate toward the league baseline in proportion to how little data is available. A team with three recorded matches must not be treated with the same confidence as one with twenty.

### 5.4 Output

Report the probability as a percentage with one decimal (e.g. `84.2%`), accompanied by:

- `lambda_total` — the expected total goals
- `confidence` — High / Medium / Low, driven by sample size and source agreement
- `data_completeness` — the share of §4 inputs that were actually available

---

## 6. Compare against bookmaker odds

### 6.1 Implied probability

The raw implied probability is:

```
implied_raw = 1 / decimal_odds
```

This figure **includes the bookmaker's margin** and therefore overstates the true implied probability. Comparing a model estimate directly against it biases every result toward "no value". Remove the margin using the Over/Under 1.5 pair:

```
overround    = (1 / odds_over) + (1 / odds_under)
implied_fair = (1 / odds_over) / overround
```

Report `implied_raw`, `implied_fair`, and `overround`. Use **`implied_fair`** for all value calculations. Where the Under 1.5 price is unavailable, fall back to `implied_raw` and flag the row as margin-unadjusted.

### 6.2 Value metrics

```
Value = model_probability - implied_fair
EV    = (model_probability * decimal_odds) - 1
```

`Value` is the probability edge; `EV` is the expected return per unit staked and is the more comparable figure across different odds levels. Report both.

An optional Kelly fraction may be shown for information only; the program recommends no stake sizes.

The goal is neither the highest odds nor the highest probability in isolation, but fixtures combining a strong probability, a reasonable price, and a positive edge.

---

## 7. Ranking

Rank on a **combined score**, never on odds alone and never on probability alone:

```
score = (W_PROBABILITY * normalised_probability) + (W_VALUE * normalised_value)
```

Defaults: `W_PROBABILITY = 0.60`, `W_VALUE = 0.40`, both configurable and required to sum to 1.0.

Normalise both components to a 0–1 range across the analysed set before weighting, so that neither dominates through scale alone. Break ties by higher model probability, then by higher confidence.

The scoring methodology must be documented in the code and restated in the README.

---

## 8. Selection categories

After ranking, group the qualifying matches:

- **TOP PICKS** — very high probability, strong supporting statistics, positive value where available
- **GOOD PICKS** — high probability with reasonable statistical support
- **HIGH-ODDS VALUE PICKS** — lower probability than the top group, but an unusually attractive price and a clear positive edge

Category boundaries are configurable. **Never pad a category.** If only three matches meet the criteria, report three. If none do, report none and say so plainly.

---

## 9. Qualification thresholds

Exclude a match from the recommendations when any of the following holds:

- Its kick-off falls outside the analysis window (§2.1)
- Insufficient historical data (below the configured minimum sample)
- Recent statistics materially contradict the selection
- The estimated probability falls below the minimum
- Available data is unreliable or self-contradictory
- The odds sit outside the configured range, or the edge is negative

All thresholds are configurable, and the first two are also promptable at startup (§2.1):

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

Excluded matches still appear in the full results sheet (§10.4) with the exclusion reason recorded — they are filtered from the recommendations, not from the record.

---

## 10. Excel output

Every run persists its results to an Excel workbook. This is a primary requirement, not an optional export.

### 10.1 Location and naming

- Results are written to a dedicated folder, default `results/`, configurable via `OUTPUT_DIR`, and created automatically if absent.
- The filename is the **match date in ISO format**: `YYYY-MM-DD.xlsx` — for example `results/2026-09-07.xlsx`.
- ISO ordering is required because it sorts chronologically by filename and is unambiguous and filesystem-safe on Windows.
- The filename depends on the match date only. **The analysis window, thresholds, and run time never affect it** — a narrower window or a higher minimum probability still writes to the same file for that date.

### 10.2 One file per date — update, never duplicate

**For a given date there must be exactly one workbook.**

- If no file exists for the date, create it.
- If a file already exists, **open it and update it in place.**
- Never create `2026-09-07 (1).xlsx`, `2026-09-07_v2.xlsx`, `2026-09-07_1430.xlsx`, or any other suffixed or timestamped variant.

Rows are **upserted on `match_key`** (§1):

| Situation | Behaviour |
| --- | --- |
| Key already in the workbook | Overwrite that row with the newly computed values |
| Key is new | Append it |
| Key present but kick-off outside this run's window | **Retain the row**, set `Status` to `Outside analysis window` |
| Key in the workbook but absent from this run entirely | **Retain the row**, set `Status` to `Not in latest run`, and leave `Last Updated` untouched |

Rows are never silently deleted — a fixture that disappears from the sportsbook (postponed, market pulled) or falls outside a narrowed window stays visible with its history intact.

Each row carries `First Seen (UTC)` and `Last Updated (UTC)`. After every upsert, **recompute the ranking across the full set** so the ordering reflects all known fixtures for that date, not only the latest run. Sheets and formatting the program does not own must be preserved.

### 10.3 Write safety

- Write atomically: build to a temporary file in the same directory, flush, then `os.replace()` onto the target. A crash mid-write must never leave a corrupt workbook.
- Read and write with **openpyxl** so existing content survives; use pandas for computation only.
- If the target file is locked because it is open in Excel — a common case on Windows — retry with backoff, then **fail with a clear, actionable message** ("close `results/2026-09-07.xlsx` and re-run"). Do **not** fall back to a differently named file: that would violate the no-duplicates rule. Cached results make the re-run inexpensive.

### 10.4 Workbook structure

**`Summary`** — run metadata: analysis date, the analysis window, run timestamps (first and latest), matches found, matches analysed, matches qualified, count per category, **the effective configuration with the source of each value** (§2.6), and the estimate disclaimer.

**`Ranked Selections`** — qualifying matches, best to weakest, rebuilt each run under the current thresholds (§2.6):

| Rank | Match | League | Kick-off (EAT) | O1.5 Odds | Model Prob | Implied (Fair) | Value | EV | H2H O1.5 % | Recent O1.5 % | Confidence | Category | Score | Status | First Seen | Last Updated |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

**`All Matches`** — every fixture analysed, including those excluded, with an `Exclusion Reason` column and the full set of §4 statistics.

**`Run Log`** — one row per execution: timestamp, duration, window, minimum probability, minimum odds, fixtures found, rows added, rows updated, warnings, errors.

**`Data Sources`** — per-statistic provenance: source name, endpoint or URL, retrieval time, and any missing-data notes.

### 10.5 Presentation

Frozen header row, auto-filter enabled, sensible column widths, percentages formatted as percentages, odds to two decimals, and a conditional colour scale across the probability and value columns. The workbook should be readable without reformatting.

---

## 11. Console report

Alongside the workbook, print a concise summary that restates the settings actually used:

```
DATE: 7 September 2026
WINDOW: 14:30 - 22:30 EAT
THRESHOLDS: min probability 75.0% | min odds 1.35

MATCHES FOUND: 127
IN WINDOW: 63
MATCHES ANALYSED: 58
QUALIFIED (OVER 1.5): 11

TOP 10 SELECTIONS
Rank | Match | Odds | Model Prob | Value | Confidence
...

Saved to: results/2026-09-07.xlsx (18 rows updated, 6 added)
```

For each top selection, give a one- or two-sentence rationale grounded in the actual figures — for example:

> Over 1.5 probability 84.1% (expected total goals 3.30). Both sides have produced 2+ goals in 9 of their last 10; their last 10 meetings went Over 1.5 eight times. Fair implied probability 78.1%, giving an edge of +6.0 points.

If nothing qualifies, say so plainly and name the binding constraint — for example, "No matches met the 75.0% minimum within 14:30-22:30 EAT; the best was 71.4%." A user who over-tightened a threshold should be able to see that immediately.

Close with the estimate disclaimer.

---

## 12. Data quality and error handling

- **Never fabricate a statistic.** If a source cannot supply a figure, mark it unavailable and let it lower `data_completeness` and `confidence`.
- Attribute every statistic to its source.
- Normalise team names across sources using an alias map plus fuzzy matching (e.g. `rapidfuzz`). Below a configurable similarity threshold, flag the match for review rather than assuming a link.
- Deduplicate fixtures on `match_key`.
- Handle gracefully: missing data, duplicate listings, name mismatches, API errors, timeouts, rate limits, and dynamically loaded content.
- A failure on one match must be logged and skipped without aborting the run. Log in a structured form to both console and file, at a configurable level.
- Exit codes: `0` success, `1` completed with recoverable errors, `2` fatal.

---

## 13. Technical requirements

Python 3.11 or newer, running cleanly on Windows.

| Purpose | Library |
| --- | --- |
| Browser automation | Playwright |
| HTML parsing | BeautifulSoup (where appropriate) |
| Data processing | pandas |
| Numerics | NumPy, SciPy |
| Excel read/write | openpyxl |
| Name matching | rapidfuzz |
| Configuration | pydantic-settings or python-dotenv |
| Testing | pytest |

Interactive prompts need no third-party library; the standard `input()` is sufficient. A prompt toolkit such as `questionary` or `rich` may be used for presentation, but the program must still run correctly when stdin is not a TTY (§2.5).

Machine-learning libraries are unnecessary for the Poisson model; introduce them only if a genuinely better-performing approach is implemented and evaluated.

### Project structure

```
project/
    main.py                 CLI entry point and orchestration
    config.py               configuration, defaults, thresholds
    prompts.py              interactive setup and precedence resolution
    gsb_scraper.py          fixture and odds collection
    statistics_provider.py  external statistics, with caching
    probability_model.py    Poisson model and blending
    odds.py                 margin removal, value, EV
    ranking.py              combined score, categories
    excel_writer.py         workbook create / upsert / format
    reporting.py            console report
    utils/
        naming.py           team-name normalisation
        logging.py          structured logging
    tests/
    requirements.txt
    README.md
    .env.example
```

### Command line

```
python main.py                             prompt for settings, then analyse today (EAT)
python main.py --no-prompt                 skip all prompts, use defaults

python main.py --window 14:00-22:00        explicit kick-off window (EAT)
python main.py --next-hours 6              kick-off within the next 6 hours
python main.py --min-probability 75        minimum model probability (percent)
python main.py --min-odds 1.35             minimum Over 1.5 decimal odds

python main.py --date 2026-09-08           analyse a specific date
python main.py --fixtures data.csv         use a manual fixture list
python main.py --output-dir results        override the output folder
python main.py --dry-run                   analyse without writing Excel
```

`--window` and `--next-hours` are mutually exclusive; supplying both is an argument error. Any setting given as a flag is not prompted for (§2.2).

### Testing

Unit tests covering, at minimum:

- The Poisson probability calculation against known values
- Margin removal and the value / EV metrics
- The ranking score and category assignment
- Team-name normalisation
- **Precedence resolution** — flag beats prompt beats environment beats default
- **Non-interactive fallback** — with stdin not a TTY the program uses defaults and never blocks
- **Prompt validation** — out-of-range probability, odds crossing `MAX_ODDS`, and inverted windows all re-prompt rather than crashing
- **Window filtering** — fixtures outside the window are excluded from recommendations but retained in `All Matches`
- **The Excel upsert path** — creating a new file, updating an existing one, retaining rows absent from a later run, rebuilding `Ranked Selections` when thresholds change between runs, and confirming that no duplicate file is ever produced for a date

The README must document installation (including `playwright install`), configuration, the interactive prompts and their defaults, usage, and the scoring methodology.

---

## 14. Acceptance criteria

The implementation is complete when:

1. Running with no arguments prompts for the analysis window, minimum probability, and minimum odds, and accepting every default with Enter produces a full analysis of the current day.
2. A setting supplied as a command-line flag is not prompted for, and flag values override prompted, environment, and default values in that order.
3. An unattended run — `--no-prompt`, or stdin not a TTY — never blocks for input and logs the effective configuration.
4. Invalid prompt input re-prompts with an explanation instead of crashing.
5. All available upcoming soccer fixtures for the target day are collected, not a subset, before the window filter is applied.
6. Each fixture within the window receives a documented Over 1.5 probability, or is excluded with a stated reason.
7. Bookmaker margin is removed before any value calculation.
8. Results are ranked by the combined probability-and-value score.
9. Results are written to `results/YYYY-MM-DD.xlsx`, named by match date alone.
10. **Re-running for the same date updates that one workbook and produces no second file**, including when the window or thresholds differ between runs.
11. Rows from earlier runs that are absent from a later one, or that fall outside a narrowed window, are retained and marked, not deleted.
12. Missing statistics are marked unavailable and never invented.
13. A single match failure does not abort the run.
14. No bet is ever placed, and no authenticated action is ever taken.

---

## Primary objective

**Identify the day's upcoming soccer matches with the highest statistically estimated probability of finishing Over 1.5 goals, highlight those offering attractive odds and genuine value, present them ranked from strongest to weakest, and persist them to a single Excel workbook per date that is updated in place on every run.**

The user may optionally narrow the analysis window and tighten the minimum probability and minimum odds at startup; left alone, the program runs on its documented defaults.

This is an analysis and recommendation tool. It places no bets, and its probabilities are estimates rather than predictions of certain outcomes.
