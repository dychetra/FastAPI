/**
 * ScanPrice -> Google Drive daily backup (Google Apps Script, runs in Google's cloud).
 *
 * Saves into  My Drive / ScanPrice backups :
 *   grocery-backup-YYYY-MM-DD.json   full backup (items, prices, history, sales)
 *   grocery-prices-YYYY-MM-DD.csv    price list for Excel / Google Sheets
 *   photos/<barcode> <name>.jpg      one file per product photo (new/changed only)
 * Keeps the last 60 daily backups. No computer needs to be on.
 *
 * Setup (once), signed in to Google as the account that should own the backups:
 *   1. script.google.com -> New project -> paste this file -> Save.
 *   2. Project Settings (gear) -> Script properties -> Add:
 *        APP_PASSWORD = the ScanPrice shop password
 *   3. Select "dailyBackup" -> Run -> allow access. Check the Drive folder.
 *   4. Select "installDailyTrigger" -> Run once. Done: it runs every evening.
 * Google e-mails you if a scheduled run fails.
 */

const APP_URL = 'https://scan.gechheng.shop';
const FOLDER_NAME = 'ScanPrice backups';
const KEEP_DAYS = 60;
const TIME_ZONE = 'Asia/Phnom_Penh';
const RUN_AT_HOUR = 21;                 // 9 pm Cambodia time
const TIME_BUDGET_MS = 4.5 * 60 * 1000; // Apps Script stops at 6 min; the rest waits for tomorrow

function dailyBackup() {
  wakeApp_();
  const root = folder_(DriveApp.getRootFolder(), FOLDER_NAME);
  const day = Utilities.formatDate(new Date(), TIME_ZONE, 'yyyy-MM-dd');

  const json = fetchApp_('/api/backup').getContentText();
  const data = JSON.parse(json);  // stop here if it is not valid
  replaceFile_(root, 'grocery-backup-' + day + '.json', Utilities.newBlob(json, 'application/json'));
  replaceFile_(root, 'grocery-prices-' + day + '.csv', fetchApp_('/api/export.csv').getBlob());
  prune_(root, 'grocery-backup-');
  prune_(root, 'grocery-prices-');

  const p = syncPhotos_(folder_(root, 'photos'));
  console.log('Saved ' + data.items.length + ' items, ' + (data.sales || []).length + ' sales; photos: ' +
              p.total + ' in folder (' + p.added + ' new/changed, ' + p.removed + ' removed' +
              (p.postponed ? ', ' + p.postponed + ' left for next run' : '') + ')');
}

function installDailyTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'dailyBackup'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('dailyBackup').timeBased().everyDays(1)
    .atHour(RUN_AT_HOUR).inTimezone(TIME_ZONE).create();
  console.log('Daily backup scheduled around ' + RUN_AT_HOUR + ':00 ' + TIME_ZONE);
}

// ---------------------------------------------------------------- helpers
function password_() {
  const p = PropertiesService.getScriptProperties().getProperty('APP_PASSWORD');
  if (!p) throw new Error('Add APP_PASSWORD in Project Settings > Script properties');
  return p;
}

// The free Render server sleeps when unused; the first request wakes it (30-60 s).
function wakeApp_() {
  for (let i = 0; i < 4; i++) {
    try {
      if (UrlFetchApp.fetch(APP_URL + '/healthz', { muteHttpExceptions: true }).getResponseCode() === 200) return;
    } catch (e) { /* still waking up */ }
    Utilities.sleep(15000);
  }
}

function fetchApp_(path) {
  const headers = { Authorization: 'Basic ' + Utilities.base64Encode('shop:' + password_()) };
  let last = '';
  for (let i = 0; i < 4; i++) {
    let r;
    try {
      r = UrlFetchApp.fetch(APP_URL + path, { headers: headers, muteHttpExceptions: true });
    } catch (e) { last = String(e); Utilities.sleep(15000); continue; }
    const code = r.getResponseCode();
    if (code === 200) return r;
    if (code === 401) throw new Error('ScanPrice refused the password: check APP_PASSWORD in Script properties');
    last = path + ' answered HTTP ' + code + ': ' + r.getContentText().slice(0, 200);
    if (code < 500 && code !== 429) break;
    Utilities.sleep(15000);
  }
  throw new Error('Could not download ' + path + ' from ScanPrice. ' + last);
}

function folder_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function replaceFile_(folder, name, blob) {
  const it = folder.getFilesByName(name);
  while (it.hasNext()) it.next().setTrashed(true);
  return folder.createFile(blob.setName(name));
}

function prune_(folder, prefix) {
  const files = [];
  const it = folder.getFiles();
  while (it.hasNext()) {
    const f = it.next();
    if (f.getName().indexOf(prefix) === 0) files.push(f);
  }
  files.sort(function (a, b) { return a.getName() < b.getName() ? -1 : 1; });  // dated names
  files.slice(0, Math.max(0, files.length - KEEP_DAYS)).forEach(function (f) { f.setTrashed(true); });
}

function safeName_(text) {
  return text.replace(/[\\/:*?"<>|\x00-\x1f]+/g, ' ').trim().replace(/\.+$/, '').slice(0, 100);
}

function readIndex_(pdir) {
  const it = pdir.getFilesByName('index.json');
  if (!it.hasNext()) return {};
  try { return JSON.parse(it.next().getBlob().getDataAsString()); } catch (e) { return {}; }
}

function fileById_(id) {
  try { const f = DriveApp.getFileById(id); return f.isTrashed() ? null : f; } catch (e) { return null; }
}

// Mirror the app's photos as '<barcode> <name>.jpg'; index.json remembers Drive file ids.
function syncPhotos_(pdir) {
  const photos = JSON.parse(fetchApp_('/api/photos').getContentText());
  const index = readIndex_(pdir);
  const next = {};
  const start = Date.now();
  let added = 0, removed = 0, postponed = 0;
  photos.forEach(function (p) {
    const stem = safeName_(p.barcode + ' ' + p.name);
    const prev = index[p.barcode];
    const prevFile = prev ? fileById_(prev.id) : null;
    if (prevFile && prev.updated_at === p.updated_at) {
      const ext = (prevFile.getName().match(/\.[a-z]+$/) || ['.jpg'])[0];
      if (prevFile.getName() !== stem + ext) prevFile.setName(stem + ext);  // product renamed
      next[p.barcode] = prev;
      return;
    }
    if (Date.now() - start > TIME_BUDGET_MS) {           // continue tomorrow
      if (prev) next[p.barcode] = prev;
      postponed++;
      return;
    }
    let blob;
    try {
      blob = p.url.indexOf('http') === 0
        ? UrlFetchApp.fetch(p.url).getBlob()             // online photo: no password sent
        : fetchApp_(p.url).getBlob();
    } catch (e) {
      if (prev) next[p.barcode] = prev;
      postponed++;
      return;
    }
    const type = blob.getContentType();
    const ext = type === 'image/png' ? '.png' : type === 'image/webp' ? '.webp' : '.jpg';
    if (prevFile) prevFile.setTrashed(true);
    const f = pdir.createFile(blob.setName(stem + ext));
    next[p.barcode] = { id: f.getId(), updated_at: p.updated_at };
    added++;
  });
  Object.keys(index).forEach(function (code) {             // product or photo deleted
    if (!next[code]) {
      const f = fileById_(index[code].id);
      if (f) { f.setTrashed(true); removed++; }
    }
  });
  replaceFile_(pdir, 'index.json', Utilities.newBlob(JSON.stringify(next), 'application/json'));
  return { total: Object.keys(next).length, added: added, removed: removed, postponed: postponed };
}
