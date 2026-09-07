Create a Python program that analyzes **upcoming soccer matches** from the GSB Uganda sportsbook:

https://gsb.ug/sportsbook/upcoming

### Main objective

The program should find **all available upcoming soccer matches for the current day** and identify the matches with the strongest statistical likelihood of ending with **Over 1.5 total goals** (2 or more goals).

The program must **not place bets automatically**. It should only collect data, analyze the matches, calculate probabilities, and present the best selections.

### 1. Collect upcoming matches

Open the GSB Uganda upcoming sportsbook page and retrieve all available upcoming soccer matches for the current day.

Because the website may load matches dynamically using JavaScript, use an appropriate browser-automation framework such as **Playwright or Selenium** rather than relying only on `requests`.

For every match, collect:

* League/competition
* Match date
* Kick-off time
* Home team
* Away team
* GSB Over 1.5 goals odds
* Any other relevant available betting odds

Do not include matches that have already started or finished.

### 2. Gather external statistics

For every upcoming match, obtain historical statistics from reliable publicly available football-data sources on the Internet.

The analysis should prioritize:

#### A. Head-to-head statistics

Analyze previous meetings between the two teams, including:

* Number of previous H2H matches
* Percentage ending Over 1.5 goals
* Average total goals
* Number of matches with 2+ goals
* Most recent H2H results

Give H2H statistics significant weight, but do not rely on H2H alone.

#### B. Recent team form

Analyze the most recent matches of both teams, preferably the last 5–10 matches.

Calculate:

* Over 1.5 percentage
* Average goals scored
* Average goals conceded
* Average total goals
* Number of matches with 2+ goals
* Goals scored in the last 5–10 matches
* Goals conceded in the last 5–10 matches

#### C. Home and away performance

Analyze:

* Home team's recent home matches
* Away team's recent away matches

Calculate the Over 1.5 percentage and average total goals separately for these situations.

#### D. League statistics

Consider the overall goal-scoring characteristics of the league:

* Average goals per match
* Over 1.5 percentage
* Recent league goal trends

#### E. Other relevant information

Where reliable data is available, consider:

* Injuries and suspensions
* Expected lineups
* Attacking and defensive strength
* Recent changes in team performance
* Motivation/importance of the match
* Competition stage
* Any other statistically relevant information

Do not give excessive weight to speculative news.

### 3. Calculate Over 1.5 probability

Create a statistical scoring/modeling system that estimates the probability that each match will finish with **at least 2 total goals**.

The model should combine the available evidence rather than simply using one statistic.

Give greater importance to:

1. Recent goal-scoring statistics
2. Recent Over 1.5 performance
3. Home/away Over 1.5 performance
4. H2H Over 1.5 performance
5. Average goals scored and conceded
6. League goal statistics
7. Other reliable supporting information

Produce an estimated probability such as:

* 85%
* 78%
* 73%
* 68%

The probability must be a model estimate, not a claim that the result is guaranteed.

### 4. Compare probability with bookmaker odds

For each match, calculate the implied probability from the GSB Over 1.5 odds:

Implied probability = 1 / decimal odds

Then compare the bookmaker's implied probability with the model's estimated probability.

Calculate a value indicator such as:

**Value = Model Probability − Implied Probability**

This is important because the objective is not simply to find matches with high odds or high probability individually.

The ideal selections should have:

* High probability of Over 1.5
* Reasonably attractive odds
* Positive statistical value

### 5. Ranking system

Rank all analyzed matches using a combined ranking system that considers both:

**A. Probability of Over 1.5 goals**

and

**B. Attractive bookmaker odds/value**

The ranking should prioritize matches where there is a strong statistical probability while still offering relatively good odds.

Do NOT simply sort by odds alone.

Do NOT simply sort by probability alone.

Create a combined score, for example:

* 60% weight: estimated Over 1.5 probability
* 40% weight: value/odds attractiveness

Explain the scoring methodology in the program.

### 6. Output

Display the results in a clear table sorted from the **best selection to the weakest selection**.

The table should contain:

| Rank | Match | League | Time | O1.5 Odds | Model Probability | Implied Probability | Value | H2H O1.5 % | Recent O1.5 % | Rating |
| ---- | ----- | ------ | ---- | --------- | ----------------- | ------------------- | ----- | ---------- | ------------- | ------ |

For example:

1. Team A vs Team B — 1.65 odds — 86% probability
2. Team C vs Team D — 1.80 odds — 82% probability
3. Team E vs Team F — 2.05 odds — 76% probability

### 7. Selection categories

After ranking all matches, divide the results into categories:

**🔥 TOP PICKS**

* Very high probability
* Strong supporting statistics
* Positive value where possible

**⭐ GOOD PICKS**

* High probability
* Reasonable statistical support

**⚠️ HIGH-ODDS VALUE PICKS**

* Lower probability than the top picks
* But unusually attractive odds/value

Do not artificially create selections if the statistics do not support them.

If only 3 matches meet the criteria, show only 3.

### 8. Minimum statistical requirements

Avoid recommending a match when:

* There is insufficient historical data
* Recent statistics strongly contradict the selection
* The estimated probability is too low
* The available data is unreliable
* The bookmaker odds provide poor value

Allow the minimum probability threshold to be configured in the Python script, for example:

MIN_PROBABILITY = 70

Also allow the user to configure:

MIN_ODDS = 1.20
MAX_ODDS = 5.00

### 9. Data quality

The program should clearly identify where each statistic came from.

Do not fabricate missing statistics.

If a data source cannot provide information for a particular match, mark that statistic as unavailable rather than guessing.

Handle:

* Missing data
* Duplicate matches
* Team-name differences between websites
* API errors
* Website timeouts
* Rate limits
* Dynamically loaded pages

The program should log errors without stopping the entire analysis.

### 10. Final report

At the end, display a concise report such as:

DATE: 7 September 2026

MATCHES ANALYZED: 127

QUALIFIED OVER 1.5 MATCHES: 24

TOP 10 SELECTIONS:

Rank | Match | Odds | Probability | Value | Rating

The program should also explain briefly **why each top selection was ranked highly**, for example:

"Over 1.5 probability: 84%. Both teams have produced 2+ goals in 9 of their last 10 matches, while their H2H meetings have produced Over 1.5 in 8 of the last 10."

### 11. Technical requirements

Use Python 3.

Prefer:

* Playwright for dynamically loading the GSB website
* Pandas for data processing
* NumPy/scikit-learn where useful for statistical modeling
* BeautifulSoup only where appropriate
* A reliable football statistics API or publicly accessible football-data source for historical statistics

Structure the project cleanly:

/project
main.py
gsb_scraper.py
statistics.py
probability_model.py
ranking.py
config.py
requirements.txt
README.md

The program should be easy to run on Windows.

Provide installation instructions and all required dependencies.

### Most important requirement

The program's primary objective is:

**Find today's upcoming soccer matches with the highest statistically estimated probability of finishing Over 1.5 goals, while also identifying matches offering attractive odds/value, and present them in a ranked list from strongest to weakest.**

The program must analyze **all available upcoming soccer matches**, not just a small predefined selection.

It must be an **analysis and recommendation tool only**, with no automatic bet placement.
