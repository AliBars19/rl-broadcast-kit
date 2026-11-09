# TikTok research tools

Three small standard-library Python scripts for looking at public TikTok videos in bulk. They help answer questions such as how often a set of accounts posts, how long their videos run, how many views they get, and how many scene cuts a compilation has.

## What each script does

| Script | What it does |
|---|---|
| `collect.py` | Fetches the metadata of the first 60 videos for each account with `yt-dlp` and saves it to `raw/<handle>.json`. Skips accounts that already have a valid file, so you can stop and resume. |
| `stats.py` | Reads the raw files and writes per-account, per-video and summary outputs. It counts posts in the last 30 and 90 days, and it gives duration and view medians. |
| `cuts.py` | Picks a sample of longer compilation-style videos from `stats-videos.csv`, downloads a low-quality copy of each, and uses ffmpeg to count scene cuts and short black cards. |

Run `stats.py` before `cuts.py`, because `cuts.py` reads the video list that `stats.py` writes.

## Input file

Put one or more account lists in the working folder. Each file name must match `accounts-*.json`. Copy `accounts.example.json` to start:

```json
[
  {"handle": "example_creator_1", "segment": "example", "validated": true},
  {"handle": "example_creator_2", "segment": "example", "validated": true}
]
```

- `handle` is the TikTok username, with or without the `@`.
- `segment` is a label you choose. Results are grouped by it. Missing values become `unassigned`.
- `validated` set to `false` skips that entry.
- Duplicate handles are used once, and the first one wins.

The example file is not read by the scripts, because its name does not match `accounts-*.json`.

## Commands

Run everything from the folder that holds your account files, or pass `--dir`. Use `-I` so the folder does not shadow Python modules.

```sh
python3 -I /path/to/tools/research/collect.py --dir ~/tiktok-research
python3 -I /path/to/tools/research/stats.py --dir ~/tiktok-research
python3 -I /path/to/tools/research/cuts.py --dir ~/tiktok-research
```

Flags:

| Flag | Scripts | Meaning |
|---|---|---|
| `--dir FOLDER` | all | Folder with your `accounts-*.json` and the outputs. Default: the current folder. |
| `--yt-dlp COMMAND` | `collect.py`, `cuts.py` | The yt-dlp command or path. Default: `yt-dlp` on PATH. |
| `--limit N` | `collect.py`, `cuts.py` | Only the first N accounts (or N unfinished videos for `cuts.py`). |
| `--force` | `collect.py` | Fetch again even when a raw file exists. |
| `--today YYYY-MM-DD` | `stats.py` | The reference date for the 30 and 90 day windows. Default: today. |

You need `yt-dlp` for `collect.py` and `cuts.py`, and `ffmpeg` for `cuts.py`. The scripts do not install them.

Run the tests with:

```sh
cd tools/research && python3 -I -m unittest discover -s . -p 'test_*.py'
```

## Outputs

All outputs go into the `--dir` folder:

- `raw/<handle>.json`: the yt-dlp metadata per account. `raw/_failures.json` lists accounts that failed.
- `stats-accounts.csv`: one row per account (posts per week, durations, median views, and so on).
- `stats-videos.csv`: one row per video, with its date, duration, views, likes and a short title.
- `stats-summary.json` and the text printed by `stats.py`: overall and per-segment summaries.
- `cuts-sample.json`, `cuts-results.json` and `cuts-results.csv`: the sampled videos and their cut counts. Downloaded files go in `cuts-dl/`, and you can delete that folder when you are done.

## Limits

- The hashtag and discovery pages that you might sample from tend to show accounts that already get views. A list built this way is not a random sample of TikTok.
- Only the last 60 videos per account are fetched, so older posting history is not included.
- The results show association, not cause. An account that posts more or uses longer videos may differ for many other reasons.
- Scene detection uses a threshold of 0.35. It misses soft transitions such as fades and dissolves, so the clip counts from `cuts.py` are a lower bound.
- Results depend on what TikTok returns on the day you run the tools, and the numbers will change over time.

## Being polite to TikTok

The scripts only read public video metadata. They make one request at a time, and they wait between accounts and downloads. Keep it that way. You are responsible for following TikTok's terms of service and the law where you live. Do not republish the downloaded videos, and do not commit your account lists or outputs to a public repository. The `.gitignore` in this folder already excludes them.
