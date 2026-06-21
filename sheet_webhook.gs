/**
 * sheet_webhook.gs — Google Apps Script web app backing the job-search worklist.
 *
 * The poller (watchlist_poller.py) POSTs net-new roles here; this script appends them to a
 * Google Sheet, dedupes by uid, and permanently retires any row the user marks status="delete".
 * No Python dependencies, no service account — just an HTTP POST.
 *
 * ── ONE-TIME SETUP ────────────────────────────────────────────────────────────────────────
 * 1. Create a new Google Sheet (this becomes your worklist).
 * 2. Extensions → Apps Script. Delete the stub, paste this whole file, Save.
 * 3. Project Settings (gear) → Script Properties → Add:  SECRET = <a long random string>
 *      (generate one, e.g. `python3 -c "import secrets;print(secrets.token_urlsafe(24))"`)
 * 4. Deploy → New deployment → type "Web app".
 *      - Description: worklist
 *      - Execute as: Me
 *      - Who has access: Anyone with the link
 *    Deploy, authorize when prompted, and COPY the Web app URL (ends in /exec).
 * 5. Give the poller these two values (env vars / GitHub secrets):
 *      WORKLIST_WEBHOOK_URL = <the /exec URL>
 *      WORKLIST_TOKEN       = <the SECRET from step 3>
 *
 * NOTE: after editing this script you must Deploy → Manage deployments → Edit → new version
 * for changes to take effect at the same /exec URL.
 */

// Must stay in sync with WORKLIST_FIELDS in watchlist_poller.py.
// fit_tier (A/B/C Stage-A fit rank) + fit_score/fit_why are distinct from `tier` (geo bucket).
var FIELDS = ["status", "uid", "fit_tier", "fit_score", "fit_why", "tier", "company", "title", "location", "comp", "posted", "apply_url"];
// Fields refreshed in-place when an incoming role's uid already exists (so a re-push
// updates the fit ranking without disturbing the user's status edits or row order).
var FIT_FIELDS = ["fit_tier", "fit_score", "fit_why"];
var WORKLIST = "Worklist";
var DELETED = "Deleted";   // hidden tombstone tab (one uid per row)

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);                       // serialize concurrent runs
  try {
    var body = JSON.parse(e.postData.contents);
    var secret = PropertiesService.getScriptProperties().getProperty("SECRET");
    if (!secret || body.token !== secret) {
      return _json({ error: "unauthorized" });
    }
    var incoming = body.roles || [];
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = _ensureSheet(ss, WORKLIST, FIELDS);
    var deleted = _ensureSheet(ss, DELETED, ["uid"]);

    // READ MODE (body.action === "deleted"): return every retired uid -- the durable Deleted
    // tab PLUS any worklist row currently marked status='delete' (a just-typed DELETE not yet
    // swept into the tab). run_profiles pulls this each run to feed the git-side tombstone, so
    // a delete the user types in the Sheet propagates to the engine. Read-only; no mutation.
    if (body.action === "deleted") {
      var dset = {};
      var dvals0 = deleted.getDataRange().getValues();
      for (var i0 = 1; i0 < dvals0.length; i0++) { if (dvals0[i0][0]) dset[dvals0[i0][0]] = true; }
      var wvals = sheet.getDataRange().getValues();
      var whead = wvals.length ? wvals[0] : FIELDS;
      var ws = whead.indexOf("status"), wu = whead.indexOf("uid");
      for (var w0 = 1; w0 < wvals.length; w0++) {
        var st0 = String(ws >= 0 ? wvals[w0][ws] : "").trim().toLowerCase();
        var u0 = wu >= 0 ? wvals[w0][wu] : "";
        if (st0 === "delete" && u0) dset[u0] = true;
      }
      return _json({ deleted: Object.keys(dset) });
    }

    // REBUILD MODE (body.mode === "replace"): make the sheet an exact mirror of the
    // incoming worklist. Clears the Worklist + Deleted (tombstone) tabs and writes the
    // incoming rows fresh, carrying over each row's existing status by uid so triage
    // survives. Used by backfill_sheet.py --rebuild to resync after CSV/sheet drift.
    if (body.mode === "replace") {
      if (!incoming.length) return _json({ error: "refusing to rebuild with empty roles" });
      var prev = sheet.getDataRange().getValues();
      var pHead = prev.length ? prev[0] : FIELDS;
      var pStatus = pHead.indexOf("status"), pUid = pHead.indexOf("uid");
      var statusByUid = {};
      for (var p = 1; p < prev.length; p++) {
        var pu = pUid >= 0 ? prev[p][pUid] : "";
        if (pu) statusByUid[pu] = pStatus >= 0 ? prev[p][pStatus] : "";
      }
      var fresh = incoming.map(function (role) {
        return FIELDS.map(function (f) {
          if (f === "status") return statusByUid[role.uid] || "";
          return (role[f] !== undefined && role[f] !== null) ? role[f] : "";
        });
      });
      sheet.clearContents();
      var rebuilt = [FIELDS].concat(fresh);
      sheet.getRange(1, 1, rebuilt.length, FIELDS.length).setValues(rebuilt);
      deleted.clearContents();
      deleted.getRange(1, 1, 1, 1).setValues([["uid"]]);   // reset the tombstone
      return _json({ added: fresh.length, updated: 0, total: fresh.length, deleted: 0, rebuilt: true });
    }

    // Load tombstone (durable set of retired uids).
    var tomb = {};
    var dvals = deleted.getDataRange().getValues();
    for (var i = 1; i < dvals.length; i++) { if (dvals[i][0]) tomb[dvals[i][0]] = true; }

    // Read current worklist; drop delete-marked rows (tombstoning their uids), keep the rest.
    var rows = sheet.getDataRange().getValues();
    var header = rows.length ? rows[0] : FIELDS;
    var idx = {};
    FIELDS.forEach(function (f) { idx[f] = header.indexOf(f); });
    var kept = [];                 // array of row arrays (in FIELDS order)
    var keptUids = {};
    var keptIdx = {};              // uid -> index into kept (for update-in-place)
    var newlyDeleted = [];
    for (var r = 1; r < rows.length; r++) {
      var row = rows[r];
      var status = String(idx.status >= 0 ? row[idx.status] : "").trim().toLowerCase();
      var uid = idx.uid >= 0 ? row[idx.uid] : "";
      if (status === "delete") {
        if (uid) { tomb[uid] = true; newlyDeleted.push([uid]); }
        continue;                  // retire permanently
      }
      if (row.join("").trim() === "") continue;   // skip blank rows
      var norm = FIELDS.map(function (f) { return idx[f] >= 0 ? row[idx[f]] : ""; });
      kept.push(norm);
      if (uid) { keptUids[uid] = true; keptIdx[uid] = kept.length - 1; }
    }

    // For each incoming role: tombstoned -> skip; already present -> refresh its fit columns
    // in place (preserving status + row position); otherwise append as a new row.
    var added = 0, updated = 0;
    incoming.forEach(function (role) {
      var uid = role.uid || "";
      if (uid && tomb[uid]) return;                       // retired -> never re-add
      if (uid && keptUids[uid]) {                         // exists -> update fit only
        var existing = kept[keptIdx[uid]];
        FIT_FIELDS.forEach(function (f) {
          var i = FIELDS.indexOf(f);
          if (i >= 0 && role[f] !== undefined && role[f] !== null) existing[i] = role[f];
        });
        updated++;
        return;
      }
      if (uid) keptUids[uid] = true;
      kept.push(FIELDS.map(function (f) { return f === "status" ? "" : (role[f] || ""); }));
      added++;
    });

    // Rewrite the worklist (header + kept + appended).
    sheet.clearContents();
    var out = [FIELDS].concat(kept);
    sheet.getRange(1, 1, out.length, FIELDS.length).setValues(out);
    if (newlyDeleted.length) {
      deleted.getRange(deleted.getLastRow() + 1, 1, newlyDeleted.length, 1).setValues(newlyDeleted);
    }
    return _json({ added: added, updated: updated, total: kept.length, deleted: newlyDeleted.length });
  } catch (err) {
    return _json({ error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

function doGet() { return _json({ ok: true, service: "worklist" }); }  // health check

function _ensureSheet(ss, name, header) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, header.length).setValues([header]);
    if (name === DELETED) sh.hideSheet();
  } else if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, header.length).setValues([header]);
  }
  return sh;
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
