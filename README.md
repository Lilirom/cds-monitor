# Single-name CDS activity monitor

Which reference entities traded today, and which of them are busier than usual.

Source: DTCC's public price dissemination for the SEC swap data repository.
Free, public, no account or licence needed.

- `monitor.py` — downloads the daily file, resolves amendments, writes `activity.json`
- `index.html` — the page; reads `activity.json`, no build step, no dependencies
- `state/` — normalised daily rows, kept so each run downloads one file instead of ninety
- `.github/workflows/update.yml` — runs the pipeline every morning and commits the result

---

## Setup, once

### 1. Create the repository

On github.com, **New repository**. Name it `cds-monitor`. Set it to **Public** — required
for free Pages hosting, and the underlying data is public anyway. Do not tick "Add a README".

### 2. Upload the files

**Add file → Upload files**, drag everything in, **Commit changes**.

The `.github/workflows/` folder must keep that exact path. If your browser flattens it on
upload, create the file manually instead: **Add file → Create new file**, type
`.github/workflows/update.yml` as the name, paste the contents.

### 3. Let the robot write back

**Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → **Save**.

Without this the job runs, builds the file, and fails at the final push.

### 4. Publish the page

**Settings → Pages** → Source: **Deploy from a branch** → Branch: **main**, folder **/ (root)**
→ **Save**.

After a minute or two the page is at
`https://<your-username>.github.io/cds-monitor/`

### 5. Run it once by hand

**Actions** tab → **Update CDS activity** → **Run workflow**. Watch it go green.

You may need to click "I understand my workflows, enable them" the first time — GitHub
disables Actions on newly uploaded repositories until you confirm.

---

## Daily operation

The workflow runs at 07:00 UTC, Monday to Saturday. DTCC publishes the file for day D on
D+1, so Saturday's run is the one that picks up Friday's trading. If nothing new has been
published the job commits nothing and exits quietly.

GitHub's scheduler is best-effort. A run can be delayed by up to an hour when the platform
is busy, and very idle repositories occasionally have schedules suspended — pushing any
commit reactivates them.

Add the Pages URL to your phone's home screen and it behaves like an app.

---

## Product families

The credits file is not only single-name CDS. The tabs split it by UPI FISN:

| Tab | What it is | Typical volume |
|---|---|---|
| Single name | Corporate and sovereign single-name CDS | ~400-600/day |
| Structured ref | CDS referencing ABS, CMBS, tranches | bursty: 0 most days, 100+ on unwinds |
| Baskets & CLO | Bespoke baskets, loan/CLO warehouse portfolios | a few a week |
| Total return | TRS on credit, often loan exposure | ~3/day |
| Index | CDX (the few that land in this file) | rare |
| Other credit | Credit swaps on private debt, Term Loan B references | ~6/day |

Everything outside single name is thin. A 20x on one trade means nothing there; the tab
carries a warning for that reason. The families are still worth watching as event
detectors: a 97-trade day in Structured ref on 29 June was a portfolio unwind across
Ameriquest and BBCMS deals, invisible if you only look at single names.

A handful of rate swaps land in the credits file each month, presumably mis-tagged at
source. Those are dropped, not shown.

## Reading the numbers

**The count** is new single-name trades reported to the SEC repository for that day, after
cancels and corrections are resolved.

**The multiple (`×`)** is that count divided by the name's own mean over the preceding 20
business days, counting days it did not trade as zero.

**Ranking** does not use the multiple. A name averaging 0.05 trades/day posts a 20× on a
single trade, which is noise. Rows are ordered by a regularised deviation,
`(count − λ) / √(λ + 0.5)`, which keeps thin-history names in proportion. The multiple is
still shown because it is the number you can reason about.

**The tick** on each bar marks that name's own mean, so deviation is visible spatially.

**Hatched bars** mean most of that day's notionals were capped.

**`5/20D`** means the name traded on only 5 of the last 20 business days — treat its
multiple with suspicion.

---

## What this is not

**Not volume.** Roughly two thirds of disseminated notionals are capped and rounded under
the dissemination rules, so large tickets are systematically understated. Counts are robust;
notional is reported only as a floor.

**Not the whole market.** This is the SEC regime. European and UK single-name flow reports
mainly to EMIR and UK-EMIR repositories and is largely absent. Index products (iTraxx, CDX)
go to the CFTC repositories, not this file.

**Not stable history.** Earlier days restate as late cancels and corrections arrive, which
is why the pipeline reprocesses a rolling window rather than appending.

**Structured references often have no issuer.** A CDS on a CMBS deal references the deal,
not a company. Those rows show their identifier in brackets and sit behind the RED-only
toggle.

**Names are imperfect.** Entities identified only by a Markit RED code cannot be resolved
without a RED licence; they appear as `RED/ISIN …` behind the RED-only toggle. A few
obligation labels still survive the filters as pseudo-entities — `NIGERIA OMO BILL` is
Nigeria's, but it is the obligation, not the reference entity.

---

## Changing things

**Longer or shorter baseline** — `WINDOW` in `index.html`.

**Keep more history** — `RETAIN_DAYS` in `monitor.py`. The repo grows about 30 KB per day.

**Changed the name filters?** Bump `NORMALISER_VERSION` in `monitor.py`. The cache stores
post-normalisation rows, so without a bump your new logic applies only to future days and
the old days keep serving results from the previous version.

**Different run time** — the `cron` line in `.github/workflows/update.yml`, in UTC.
