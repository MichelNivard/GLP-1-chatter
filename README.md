# GLP-1 Reddit User-Report Miner

Static GitHub Pages project for self-updating Reddit text mining of user reports involving retatrutide, tirzepatide, and semaglutide family weight-loss drugs.

This is observational social-media text mining. It is not medical advice, clinical evidence, or proof of causality. Reddit text can be ambiguous, edited, promotional, wrong, duplicated, or sarcastic.

## What It Mines

Drug families and default search terms:

- `reta`: `reta`, `retatrutide`, `retaglutide`
- `tirz`: `tirz`, `tirzepatide`, `mounjaro`, `zepbound`
- `sema`: `sema`, `semaglutide`, `ozempic`, `wegovy`, `rybelsus`

Default subreddits are configured in `config/sources.json`. The seed list includes:

- `Retatrutide`, `Peptides`, `Semaglutide`, `Ozempic`, `WegovyWeightLoss`, `Mounjaro`, `Zepbound`
- Additional relevant communities found at project setup: `RetatrutideTrial`, `SemaglutideFreeSpeech`, `OzempicForWeightLoss`, `WegovyUK`, `MounjaroMaintenance`, `MounjaroUK`, `tirzepatidecompound`, `compoundedtirzepatide`, `GLP1`, `GLP1_BeforeAfter`

Edit `config/sources.json` and `config/search_terms.json` to change sources or search terms.

## Local Setup

Requires Python 3.12 or compatible Python 3. No third-party Python package is required.

```bash
python scripts/init_db.py
python scripts/build_site.py
python -m http.server 8000 --directory site
```

Open `http://localhost:8000`.

## Local Crawl

Recent crawl, used by automation:

```bash
python scripts/crawl_reddit.py --source auto --since-days 7 --limit 100
```

One-time historical seed:

```bash
python scripts/crawl_reddit.py --source pullpush --seed-historical --pages 10 --limit 0
```

Useful flags:

- `--source pullpush|reddit_json|auto`
- `--since-days 7`
- `--seed-historical`
- `--limit 100`
- `--dry-run`
- `--subreddit Retatrutide --subreddit Zepbound`
- `--no-comments`

The crawler stores raw Reddit candidate posts/comments in `data/glp1_reports.sqlite3`, including full text and Reddit URL.

## Local Parse

Set an OpenAI API key:

```bash
export OPENAI_API_KEY="..."
```

Parse pending candidates:

```bash
python scripts/parse_reports.py --limit 25
```

Default models:

- First pass: `gpt-5.4-nano`
- Rescreen pass: `gpt-5.4-mini`

Useful flags:

- `--model gpt-5.4-nano`
- `--rescreen-model gpt-5.4-mini`
- `--limit 25`
- `--dry-run`
- `--retry-errors`
- `--prompt-cache-key glp1-reddit-extraction`
- `--prompt-cache-retention 24h`

The parser sends exactly one Reddit post/comment per OpenAI API call. It uses a stable prompt prefix plus a varying per-post user message, and sets `prompt_cache_key` so repeated calls can benefit from OpenAI prompt caching. Cached-token usage is stored in `parse_cache` when returned by the API.

## Processing Semantics

The raw Reddit item identity is authoritative:

- A row is uniquely identified by `source_type + reddit_id`.
- Once a post/comment is parsed or marked error, future crawler text changes do not reset it to `pending`.
- If text changes after processing, the DB sets `content_changed_after_processing = 1`.
- `processed_full_text` and `processed_content_hash` retain the exact text/hash used for extraction.
- `content_hash` remains stored for audit and cache metadata, but it is not allowed to trigger an expensive automatic reparse of an already processed post/comment.

Use `--retry-errors` only when you intentionally want to retry rows previously marked error.

## Rescreening

After computational unit conversion, a report is rescreened with `gpt-5.4-mini` if:

- extracted weight loss is greater than 25 kg, or
- extracted duration is greater than 365 days.

The rescreen prompt includes an explicit warning to check whether the large loss/duration is really attributable to the focal drug rather than prior GLP history, total journey, age, goal weight, dose, or another confound. Each processed content hash gets at most one nano pass and one mini rescreen pass unless you explicitly retry errors.

## Site Build

```bash
python scripts/build_site.py
```

Generated pages:

- `site/index.html`
- `site/reta/index.html`
- `site/tirz/index.html`
- `site/sema/index.html`
- `site/reta/side-effects.html`
- `site/tirz/side-effects.html`
- `site/sema/side-effects.html`
- JSON bundles in `site/data/`

The site is plain HTML/CSS/JS. Scatterplots are dependency-free SVG.

Plotting rules:

- x-axis: duration in weeks
- y-axis: weight change in kg
- weight loss is negative, so 10 kg lost plots as `-10`
- reports with duration under 21 days are omitted
- reports with `include_in_plots = false` are omitted
- fitted curves use Reddit reports only, never RCT overlays

## Trial Overlay CSVs

Optional CSVs can be added in `trial-data/`:

- `trial-data/trial-reta.csv`
- `trial-data/trial-tirz.csv`
- `trial-data/trial-sema.csv`

Required columns:

```csv
weeks,loss_kg,sd_loss_kg
12,8.2,4.1
24,15.4,6.0
```

Optional `dose` or `arm` columns create separate trial overlay curves:

```csv
dose,weeks,loss_kg,sd_loss_kg
5 mg,0,0,0
5 mg,72,16.46,10.01
10 mg,0,0,0
10 mg,72,22.65,10.35
```

`loss_kg` is interpreted as weight-loss magnitude. The site plots it as negative weight change: `-abs(loss_kg)`. The ribbon is mean plus/minus `1.96 * sd_loss_kg`. Extra audit columns such as `n`, `percent_change`, `se_percent`, `baseline_weight_kg`, `body_weight_kg`, `se_kg`, `pixel_y_mean`, `pixel_y_top`, `pixel_y_bottom`, `source`, `source_url`, `method`, and `n_assumption` are allowed. Trial overlays are external uploaded aggregate data and are not used in Reddit fitted curves.

Included retatrutide overlay data in `trial-data/trial-reta.csv` were digitized from the body-weight figure in Jastreboff et al., "Triple-Hormone-Receptor Agonist Retatrutide for Obesity - A Phase 2 Trial", New England Journal of Medicine, DOI [`10.1056/NEJMoa2301972`](https://www.nejm.org/doi/full/10.1056/NEJMoa2301972). Placebo is excluded. Because sample sizes were not visible in the figure, `n` uses an explicit assumption of 300 randomized participants allocated 2:1:1:1:1:2:2 across the six retatrutide arms and placebo.

## Side-Effect Normalization

Side effects are extracted as lowercase phrases by the LLM, then normalized in code using `config/side_effect_normalization.json`. Keep this mapping explicit and auditable.

## GitHub Actions

Workflows:

- `.github/workflows/crawl.yml`: scheduled daily at 03:18 UTC and manual dispatch. Crawls recent candidates, default last 7 days, and commits DB changes.
- `.github/workflows/parse.yml`: runs after crawl, on schedule, or manually. Uses `OPENAI_API_KEY` from GitHub Secrets. Parses one pending post/comment per API call and commits DB changes.
- `.github/workflows/pages.yml`: rebuilds the static site from SQLite and deploys to GitHub Pages.

Repository setup:

1. Add `OPENAI_API_KEY` in GitHub repository secrets.
2. In repository settings, enable GitHub Pages with source `GitHub Actions`.
3. Run `Crawl Reddit candidates` manually with `--seed-historical` locally for the initial historical seed, or temporarily edit the workflow command if you want GitHub Actions to do the initial seed.
4. After the first crawl, run `Parse Reddit candidates`.
5. Run `Build and publish Pages site`.

The workflow commit pattern follows the general GitHub Actions approach of running a script, checking for changed files, committing, and pushing from the action.

## Database Schema Overview

SQLite path: `data/glp1_reports.sqlite3`.

Main tables:

- `raw_posts`: raw Reddit candidate posts/comments, full text, URL, match terms, post-level parse status, processed text/hash marker, drift flag, parse/rescreen metadata.
- `parse_cache`: one first-pass parse and one rescreen parse per processed content hash, with model, prompt version, cache key, token usage, status, result JSON, converted JSON, and error text.
- `extracted_reports`: one or more extracted drug reports per raw post/comment, including raw values and computed kg/day/week values. Rescreened reports become canonical while first-pass reports are retained as non-canonical when feasible.

## Files

- `scripts/crawl_reddit.py`: slow Reddit/PullPush crawler.
- `scripts/parse_reports.py`: one-item-per-call OpenAI parser with strict JSON validation.
- `scripts/build_site.py`: SQLite-to-static-site generator.
- `prompts/extract_glp1_report.md`: extraction prompt under 3000 words.
- `static/app.js`, `static/styles.css`: dependency-free browser UI.
- `config/*.json`: sources, search terms, side-effect normalization.
