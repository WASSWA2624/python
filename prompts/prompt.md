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

**Before implementation**, review the GSB Uganda terms of service and `robots.txt`. If automated collection is disallowed, the fixture list must be supplied manually (see §2.4) rather than scraped. Document whichever path is taken.

All output is a statistical estimate. The program must never present a selection as a guaranteed outcome, and every report must carry a short disclaimer to that effect.

---

## 1. Definitions

| Term | Definition |
| --- | --- |
| **Match day** | The calendar day in **Africa/Kampala (EAT, UTC+3)**, the sportsbook's local timezone. "Today's matches" means fixtures kicking off between 00:00:00 and 23:59:59 EAT. |
| **Over 1.5** | The match finishes with a total of 2 or more goals, counting full time (90 minutes plus stoppage) only — no extra time, no penalties. |
| **Match key** | A stable identifier for a fixture: `sha1(normalised_league + "|" + normalised_home + "|" + normalised_away + "|" + kickoff_utc_iso)`. Used for deduplication and for updating existing Excel rows. |
| **Upcoming** | Kick-off is strictly in the future at the moment of collection. Live and completed matches are excluded. |

Store every timestamp internally in **UTC** and render it in EAT for display. This avoids off-by-one-day errors around midnight.

---

## 2. Collect upcoming matches

### 2.1 Retrieval

The page loads fixtures dynamically via JavaScript, so plain `requests` is insufficient. Use a browser-automation framework — **Playwright** (preferred) or Selenium — and wait for the fixture list to finish rendering before parsing. Expand any lazy-loaded or paginated sections so that **all** available soccer fixtures for the day are captured, not a partial first page.

### 2.2 Fields to capture

For every match:

- League / competition
- Match date (EAT and UTC)
- Kick-off time (EAT and UTC)
- Home team
- Away team
- Over 1.5 goals odds (decimal)
- Under 1.5 goals odds (decimal) — required for margin removal in §5
- Any other available markets worth recording (1X2, Over/Under 2.5, BTTS)
- `collected_at` timestamp

### 2.3 Politeness and resilience

- Set a descriptive User-Agent and a request rate that does not burden the site.
- Cache the raw page response so that re-runs during development do not re-fetch.
- Apply explicit timeouts and bounded retries with exponential backoff.
- Exclude any fixture that has already kicked off or finished.

### 2.4 Manual fallback

Support `--fixtures <path.csv>` to load fixtures from a CSV using the same columns as §2.2. This keeps the analysis pipeline usable if the site is unreachable, its markup changes, or automated collection is not permitted.

---

## 3. Gather external statistics

For every fixture, collect supporting statistics from reliable, publicly available football-data sources. Record the source and retrieval time for each statistic.

### 3.1 Head-to-head

- Number of previous meetings considered
- Percentage ending Over 1.5
- Average total goals
- Count of meetings with 2+ goals
- Most recent results

Weight head-to-head meaningfully but never as the sole input. Discount meetings older than roughly three years and those played under materially different circumstances (different division, heavily changed squads).

### 3.2 Recent form

Over the last 5–10 matches for each team:

- Over 1.5 percentage
- Average goals scored
- Average goals conceded
- Average total goals
- Count of matches with 2+ goals

### 3.3 Home and away splits

The home team's recent **home** matches and the away team's recent **away** matches, each with its own Over 1.5 percentage and average total goals.

### 3.4 League context

- League average goals per match
- League Over 1.5 percentage
- Recent scoring trend within the league

League baselines matter most when team-level samples are small, and should carry more weight as sample size falls (§4.3).

### 3.5 Supporting context

Where reliable data exists: injuries and suspensions, expected lineups, attacking and defensive strength ratings, notable recent changes in performance, match importance, and competition stage.

Speculative or unsourced news must not materially move the estimate.

---

## 4. Estimate the Over 1.5 probability

### 4.1 Method

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

### 4.2 Blending the evidence

Derive `λ_home` and `λ_away` from a weighted blend rather than any single statistic. Suggested starting weights, all configurable:

| Input | Weight |
| --- | --- |
| Recent form (goals scored / conceded) | 30% |
| Home / away split performance | 20% |
| Recent Over 1.5 rate | 20% |
| Head-to-head | 15% |
| League baseline | 10% |
| Supporting context adjustments | 5% |

### 4.3 Shrinkage

With small samples, pull the estimate toward the league baseline in proportion to how little data is available. A team with three recorded matches must not be treated with the same confidence as one with twenty.

### 4.4 Output

Report the probability as a percentage with one decimal (e.g. `84.2%`), accompanied by:

- `lambda_total` — the expected total goals
- `confidence` — High / Medium / Low, driven by sample size and source agreement
- `data_completeness` — the share of §3 inputs that were actually available

---

## 5. Compare against bookmaker odds

### 5.1 Implied probability

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

### 5.2 Value metrics

```
Value = model_probability - implied_fair
EV    = (model_probability * decimal_odds) - 1
```

`Value` is the probability edge; `EV` is the expected return per unit staked and is the more comparable figure across different odds levels. Report both.

An optional Kelly fraction may be shown for information only; the program recommends no stake sizes.

The goal is neither the highest odds nor the highest probability in isolation, but fixtures combining a strong probability, a reasonable price, and a positive edge.

---

## 6. Ranking

Rank on a **combined score**, never on odds alone and never on probability alone:

```
score = (W_PROBABILITY * normalised_probability) + (W_VALUE * normalised_value)
```

Defaults: `W_PROBABILITY = 0.60`, `W_VALUE = 0.40`, both configurable and required to sum to 1.0.

Normalise both components to a 0–1 range across the analysed set before weighting, so that neither dominates through scale alone. Break ties by higher model probability, then by higher confidence.

The scoring methodology must be documented in the code and restated in the README.

---

## 7. Selection categories

After ranking, group the qualifying matches:

- **TOP PICKS** — very high probability, strong supporting statistics, positive value where available
- **GOOD PICKS** — high probability with reasonable statistical support
- **HIGH-ODDS VALUE PICKS** — lower probability than the top group, but an unusually attractive price and a clear positive edge

Category boundaries are configurable. **Never pad a category.** If only three matches meet the criteria, report three. If none do, report none and say so plainly.

---

## 8. Qualification thresholds

Exclude a match from the recommendations when any of the following holds:

- Insufficient historical data (below the configured minimum sample)
- Recent statistics materially contradict the selection
- The estimated probability falls below the minimum
- Available data is unreliable or self-contradictory
- The odds sit outside the configured range, or the edge is negative

All thresholds are configurable:

```python
MIN_PROBABILITY  = 70.0   # percent
MIN_ODDS         = 1.20
MAX_ODDS         = 5.00
MIN_VALUE        = 0.00   # model probability minus fair implied
MIN_H2H_MATCHES  = 3
MIN_FORM_MATCHES = 5
```

Excluded matches still appear in the full results sheet (§9.4) with the exclusion reason recorded — they are filtered from the recommendations, not from the record.

---

## 9. Excel output

Every run persists its results to an Excel workbook. This is a primary requirement, not an optional export.

### 9.1 Location and naming

- Results are written to a dedicated folder, default `results/`, configurable via `OUTPUT_DIR`, and created automatically if absent.
- The filename is the **match date in ISO format**: `YYYY-MM-DD.xlsx` — for example `results/2026-09-07.xlsx`.
- ISO ordering is required because it sorts chronologically by filename and is unambiguous and filesystem-safe on Windows.

### 9.2 One file per date — update, never duplicate

**For a given date there must be exactly one workbook.**

- If no file exists for the date, create it.
- If a file already exists, **open it and update it in place.**
- Never create `2026-09-07 (1).xlsx`, `2026-09-07_v2.xlsx`, `2026-09-07_1430.xlsx`, or any other suffixed or timestamped variant.

Rows are **upserted on `match_key`** (§1):

| Situation | Behaviour |
| --- | --- |
| Key already in the workbook | Overwrite that row with the newly computed values |
| Key is new | Append it |
| Key in the workbook but absent from this run | **Retain the row**, set `Status` to `Not in latest run`, and leave `Last Updated` untouched |

Rows are never silently deleted — a fixture that disappears from the sportsbook (postponed, market pulled) stays visible with its history intact.

Each row carries `First Seen (UTC)` and `Last Updated (UTC)`. After every upsert, **recompute the ranking across the full set** so the ordering reflects all known fixtures for that date, not only the latest run. Sheets and formatting the program does not own must be preserved.

### 9.3 Write safety

- Write atomically: build to a temporary file in the same directory, flush, then `os.replace()` onto the target. A crash mid-write must never leave a corrupt workbook.
- Read and write with **openpyxl** so existing content survives; use pandas for computation only.
- If the target file is locked because it is open in Excel — a common case on Windows — retry with backoff, then **fail with a clear, actionable message** ("close `results/2026-09-07.xlsx` and re-run"). Do **not** fall back to a differently named file: that would violate the no-duplicates rule. Cached results make the re-run inexpensive.

### 9.4 Workbook structure

**`Summary`** — run metadata: analysis date, run timestamps (first and latest), matches found, matches analysed, matches qualified, count per category, the configuration values used, and the estimate disclaimer.

**`Ranked Selections`** — qualifying matches, best to weakest:

| Rank | Match | League | Kick-off (EAT) | O1.5 Odds | Model Prob | Implied (Fair) | Value | EV | H2H O1.5 % | Recent O1.5 % | Confidence | Category | Score | Status | First Seen | Last Updated |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

**`All Matches`** — every fixture analysed, including those excluded, with an `Exclusion Reason` column and the full set of §3 statistics.

**`Run Log`** — one row per execution: timestamp, duration, fixtures found, rows added, rows updated, warnings, errors.

**`Data Sources`** — per-statistic provenance: source name, endpoint or URL, retrieval time, and any missing-data notes.

### 9.5 Presentation

Frozen header row, auto-filter enabled, sensible column widths, percentages formatted as percentages, odds to two decimals, and a conditional colour scale across the probability and value columns. The workbook should be readable without reformatting.

---

## 10. Console report

Alongside the workbook, print a concise summary:

```
DATE: 7 September 2026
MATCHES FOUND: 127
MATCHES ANALYSED: 118
QUALIFIED (OVER 1.5): 24

TOP 10 SELECTIONS
Rank | Match | Odds | Model Prob | Value | Confidence
...

Saved to: results/2026-09-07.xlsx (18 rows updated, 6 added)
```

For each top selection, give a one- or two-sentence rationale grounded in the actual figures — for example:

> Over 1.5 probability 84.1% (expected total goals 3.30). Both sides have produced 2+ goals in 9 of their last 10; their last 10 meetings went Over 1.5 eight times. Fair implied probability 78.1%, giving an edge of +6.0 points.

Close with the estimate disclaimer.

---

## 11. Data quality and error handling

- **Never fabricate a statistic.** If a source cannot supply a figure, mark it unavailable and let it lower `data_completeness` and `confidence`.
- Attribute every statistic to its source.
- Normalise team names across sources using an alias map plus fuzzy matching (e.g. `rapidfuzz`). Below a configurable similarity threshold, flag the match for review rather than assuming a link.
- Deduplicate fixtures on `match_key`.
- Handle gracefully: missing data, duplicate listings, name mismatches, API errors, timeouts, rate limits, and dynamically loaded content.
- A failure on one match must be logged and skipped without aborting the run. Log in a structured form to both console and file, at a configurable level.
- Exit codes: `0` success, `1` completed with recoverable errors, `2` fatal.

---

## 12. Technical requirements

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

Machine-learning libraries are unnecessary for the Poisson model; introduce them only if a genuinely better-performing approach is implemented and evaluated.

### Project structure

```
project/
    main.py                 CLI entry point and orchestration
    config.py               configuration and thresholds
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
python main.py                          analyse today (EAT)
python main.py --date 2026-09-08        analyse a specific date
python main.py --fixtures data.csv      use a manual fixture list
python main.py --output-dir results     override the output folder
python main.py --dry-run                analyse without writing Excel
python main.py --min-probability 75     override a threshold
```

### Testing

Unit tests covering, at minimum: the Poisson probability calculation against known values, margin removal, the ranking score, team-name normalisation, and — importantly — the **Excel upsert path**: creating a new file, updating an existing one, retaining rows absent from a later run, and confirming that no duplicate file is ever produced for a date.

The README must document installation (including `playwright install`), configuration, usage, and the scoring methodology.

---

## 13. Acceptance criteria

The implementation is complete when:

1. All available upcoming soccer fixtures for the target day are collected, not a subset.
2. Each fixture receives a documented Over 1.5 probability, or is excluded with a stated reason.
3. Bookmaker margin is removed before any value calculation.
4. Results are ranked by the combined probability-and-value score.
5. Results are written to `results/YYYY-MM-DD.xlsx`.
6. **Re-running for the same date updates that one workbook and produces no second file.**
7. Rows from earlier runs that are absent from a later one are retained and marked, not deleted.
8. Missing statistics are marked unavailable and never invented.
9. A single match failure does not abort the run.
10. No bet is ever placed, and no authenticated action is ever taken.

---

## Primary objective

**Identify the day's upcoming soccer matches with the highest statistically estimated probability of finishing Over 1.5 goals, highlight those offering attractive odds and genuine value, present them ranked from strongest to weakest, and persist them to a single Excel workbook per date that is updated in place on every run.**

This is an analysis and recommendation tool. It places no bets, and its probabilities are estimates rather than predictions of certain outcomes.
