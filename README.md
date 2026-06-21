# Your own daily job finder 🛰️

Every morning, a free robot checks the career pages of **~1,500 companies** directly (not
LinkedIn — the actual company job boards, where roles show up *first*) and gives you a fresh,
de-duplicated list of **only the new roles that match what you're looking for**. No more
scrolling stale listings where the pipeline is already full.

It's free, it's private to you, and there's nothing to install. You set it up once (~20
minutes), then it runs itself and updates your list every day.

**What you'll end up with:** a Google Sheet that fills with new matching jobs each morning —
company, title, location, how fresh it is, and a direct apply link. Sort it, filter it, and
type `DELETE` in any row to make it disappear for good.

> This tool finds **where** to apply. Tailoring your résumé for each role is still on you.

---

## Setup — do this once (~20 minutes)

You need a **GitHub account** and a **Google account**. Both free. No coding.

### 1. Make your own copy
At the top of this repo, click **“Use this template” → “Create a new repository.”** Give it a
name (e.g. `my-job-finder`), set it to **Private**, and create it. Everything below happens in
*your* new copy.

### 2. Turn the robot on
GitHub disables automation on brand-new copies until you say go.
- Open the **Actions** tab → click **“I understand my workflows, go ahead and enable them.”**

**Don’t skip this** — it’s the one step everyone forgets, and without it nothing runs.

### 3. Create your Google Sheet (your daily list)
This is the part that takes the most clicks. Follow it slowly once and you’re done forever.

1. Create a new, blank **Google Sheet** (sheets.new). This becomes your job list.
2. In the Sheet: **Extensions → Apps Script**. Delete whatever’s in the editor.
3. Open the file [`sheet_webhook.gs`](sheet_webhook.gs) in this repo, copy the **whole** thing,
   and paste it into the Apps Script editor. Click **Save** 💾.
4. Click the **gear (Project Settings)** → scroll to **Script Properties** → **Add script
   property**:
   - Property: `SECRET`
   - Value: a long random password you make up (e.g. mash the keyboard, 20+ characters). Keep
     this handy for step 4.
5. Click **Deploy → New deployment**. For type, pick **Web app**.
   - Description: `worklist`
   - Execute as: **Me**
   - Who has access: **Anyone with the link**
   - Click **Deploy**. Google will ask you to **authorize** — because it’s your own script,
     you’ll see an **“unverified app”** warning. That’s expected: click **Advanced → Go to
     (your project)** and **Allow**.
6. Copy the **Web app URL** it gives you (it ends in `/exec`). Keep it for the next step.

*(Editing the script later? You must re-deploy: Deploy → Manage deployments → ✏️ → New version.)*

### 4. Connect the Sheet to your robot
Back in your GitHub repo: **Settings → Secrets and variables → Actions → New repository
secret.** Add **two** secrets (the names must end in `_ME` exactly):

| Name | Value |
|------|-------|
| `WORKLIST_WEBHOOK_URL_ME` | the `/exec` URL from step 3.6 |
| `WORKLIST_TOKEN_ME` | the `SECRET` you made up in step 3.4 |

### 5. Tell it what you’re looking for
Edit **[`profiles/me.json`](profiles/me.json)** (click it → ✏️ pencil → edit → “Commit changes”).
Change three things — see **[Pick your roles](#pick-your-roles)** and **[Set your
location](#set-your-location)** below:
- `title_include` / `title_exclude` / `workday_search_text` — the kind of job you want
- `geo` — where you’ll work
- `max_age_days` (optional) — how fresh; `7` = posted in the last week

### 6. Run it once now
**Actions** tab → click **“job-poller”** → **“Run workflow”** → green button. It takes
**5–20 minutes** (it’s politely visiting a lot of career pages). Refresh to watch it finish ✅.

### 7. Read your list
Open your Google Sheet — your matches are there, **newest first**. The columns you’ll use:
`status` (column A — type `DELETE` here to retire a row for good), `company`, `title`,
`location`, `posted`, and `apply_url` (click to apply). Triage as you go; a `DELETE`’d row won’t
come back.

> There’s also a `fit_tier` (A/B/C) column. **It’s tuned for *product* roles only** — if you’re
> in another function, ignore it and just sort by `posted`. (It’ll show mostly "C" for you, which
> is meaningless for non-product searches — not a sign anything’s wrong.)

**That’s it.** From now on it runs **every day automatically** (~10pm Mountain by default) and
adds only what’s new.

---

## Pick your roles

Your matches are chosen by two title filters and one Workday search term in `profiles/me.json`:
- **`title_include`** — keep a job only if its title matches this.
- **`title_exclude`** — …but never if it *also* matches this (kills interns, wrong seniority, etc.).
- **`workday_search_text`** — a plain phrase Workday searches for (it can’t use the patterns above).

`null` means “use the built-in Product-Manager setting.” For anything else, **copy one block
below** into `profiles/me.json`. ⚠️ The `\\b` (double backslash) is required by the file format —
keep it exactly, it just means “whole word.”

**Product / Product Management** (the default — leave the file as-is):
```json
  "title_include": null,
  "title_exclude": null,
  "workday_search_text": "Product Manager",
```

**Software Engineering:**
```json
  "title_include": "\\bsoftware engineer\\b|\\bsenior engineer\\b|\\bstaff engineer\\b|\\bbackend\\b|\\bfull[ -]?stack\\b|\\bfrontend\\b",
  "title_exclude": "\\bintern\\b|\\bmanager\\b|\\bsales\\b|\\brecruit",
  "workday_search_text": "Software Engineer",
```

**Data (Scientist / Engineer / Analyst):**
```json
  "title_include": "\\bdata (scientist|engineer|analyst)\\b|\\bmachine learning\\b|\\banalytics engineer\\b",
  "title_exclude": "\\bintern\\b|\\bsales\\b",
  "workday_search_text": "Data Scientist",
```

**Design (Product / UX):**
```json
  "title_include": "\\bproduct designer\\b|\\bux\\b|\\bui designer\\b|\\bdesign lead\\b|\\bsenior designer\\b",
  "title_exclude": "\\bintern\\b|\\bgraphic\\b|\\bmarketing\\b",
  "workday_search_text": "Product Designer",
```

**Marketing / Growth:**
```json
  "title_include": "\\bmarketing\\b|\\bgrowth\\b|\\bdemand gen\\b|\\bbrand\\b|\\bcontent\\b",
  "title_exclude": "\\bintern\\b|\\bengineer\\b|\\bproduct manager\\b",
  "workday_search_text": "Marketing Manager",
```

**Rolling your own:** `title_include` is a list of phrases you *want*, separated by `|` (means
“or”). `title_exclude` is the list you *never* want. Wrap phrases in `\\b…\\b` so “engineer”
doesn’t also match “reengineered.” Set `workday_search_text` to the single most important
phrase for your field. Keep all three in sync when you change function.

> **Coverage note (honest):** most of the ~1,500 boards work for any field. Two kinds
> (BambooHR and Workday) only return roles matching your titles, and **Workday additionally needs
> `workday_search_text`** set to your function (it searches server-side and can’t use the patterns
> above). So when you switch function, change **all three** lines together. If you forget
> `workday_search_text`, you’ll silently get ~0 Workday roles — the run log prints a `note:` line
> when it spots this, and everything else still works.

---

## Set your location

`geo` in `profiles/me.json` has three on/off switches:

```json
  "geo": { "keep_utah": false, "keep_remote_us": true, "keep_onsite_other": false }
```

- **`keep_remote_us`** — US-based remote roles. Leave this `true`; it’s the main event.
- **`keep_onsite_other`** — in-office roles anywhere in the US. Turning this `true` is *noisy*
  (lots of cities, no per-city filter), but flip it on if you’re open to relocating or there’s a
  hub you care about.
- **`keep_utah`** — a special “local” bucket that’s **hardwired to Utah** (this started as a
  Utah job search). **Honest limitation:** if you live somewhere else, you can’t make “local =
  my city.” Your realistic options are **remote-only** (the default above — recommended) or
  remote **+** `keep_onsite_other: true` to also see in-office roles and skim for your city.

---

## What to expect

- **Your first run shows EVERYTHING that currently matches** — possibly dozens of roles,
  because it has no history yet. Don’t panic. *Every run after that shows only what’s new since
  the last one.*
- **Get notified when it runs.** GitHub can email you each morning: click **Watch** (top-right
  of your repo) → **Custom** → check **Actions**. (Heads-up: GitHub emails you that the *run
  happened*, not a list of new jobs — open your Sheet to see what landed. The daily commit’s
  diff also shows exactly which rows were added.)
- **Don’t delete the files in `state/`.** That’s the robot’s memory of what it’s already shown
  you; deleting it makes every old job look “new” again.
- **No Sheet? No problem.** If you skip steps 3–4, everything still works — after your first
  successful run, your list lives as a file at `out/me_new_roles.csv` in this repo, which GitHub
  renders as a sortable table. (It doesn't exist until that first green run commits it, so check
  *after* you run, not before.)

## Keeping the company list fresh

Your list of ~1,500 companies (`boards.json`) is a snapshot from when you made your copy. It’s
already deep, so you don’t *need* to touch it. If you ever want the latest version, copy
`boards.json` from the original template repo into yours.

## Troubleshooting

- **The run failed (red ❌).** Open the failed run → read the log. Most common cause: a typo in
  `profiles/me.json` (a missing comma or quote). Fix the file and re-run.
- **Nothing in my Sheet, but the run was green.** You probably skipped a secret, or a secret
  name doesn’t end in `_ME`, or you didn’t re-deploy the Apps Script after editing it. Your
  roles are still in `out/me_new_roles.csv` regardless.
- **Got zero roles.** Your filters may be too narrow, or your window too short — try widening
  `title_include` or bumping `max_age_days` to `14`, then **Run workflow** again.
- **Plenty of roles, but none from big "Workday" companies.** You changed `title_include` but
  left `workday_search_text` on the product default — set it to your field's main phrase (e.g.
  `"Software Engineer"`). The run log prints a `note:` line when this is the cause.
