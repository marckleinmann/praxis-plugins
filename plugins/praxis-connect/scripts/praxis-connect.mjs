#!/usr/bin/env node
// praxis-connect.mjs: connects this computer to a Praxis account, so the paid
// Merlin for Construction plugins can install and check access.
//
//   node praxis-connect.mjs link              ask Praxis for a code; print the code and the address to open
//   node praxis-connect.mjs link --wait       wait (up to 9 minutes) for the code to be approved, then save the key
//   node praxis-connect.mjs settings          show the exact Claude Code settings entry this computer needs
//   node praxis-connect.mjs settings --write  add that entry to ~/.claude/settings.json (only after the person's yes)
//   node praxis-connect.mjs headers           print the catalog header for Claude Code (exit 1 when not connected)
//   node praxis-connect.mjs status            say whether this computer is connected and the account is active
//   node praxis-connect.mjs unlink            forget this computer's key; with --settings, also take the entry out
//
// The computer's key lives in ~/.praxis/device.json, readable only by this user (mode 0600).
// No command ever prints it, except `headers`, whose output goes straight to Claude Code.
// No dependencies beyond Node 18 or later.

import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { homedir, hostname, platform, userInfo } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const BASE = (process.env.PRAXIS_ACCESS_URL ||
  "https://kygwvipemaghfgeukqzn.supabase.co/functions/v1/construction-access").replace(/\/+$/, "");
const CATALOG = "praxis-construction";
const WIN = platform() === "win32";
const HOME = homedir();
const DIR = join(HOME, ".praxis");
const DEVICE = join(DIR, "device.json");
const PENDING = join(DIR, "link-pending.json");
const SELF_COPY = join(DIR, "praxis-connect.mjs");
const SETTINGS = join(HOME, ".claude", "settings.json");
const SETTINGS_BACKUP = `${SETTINGS}.bak-praxis-connect`;
const WAIT_LIMIT_MS = Number(process.env.PRAXIS_WAIT_LIMIT_MS || 9 * 60_000);
const CHECK_LIMIT_MS = 5_000;

const say = (line = "") => process.stdout.write(`${line}\n`);
const fail = (line, code = 1) => {
  process.stderr.write(`${line}\n`);
  process.exit(code);
};

// ── files ────────────────────────────────────────────────────────────────────
function ensureDir() {
  mkdirSync(DIR, { recursive: true, mode: 0o700 });
  if (!WIN) chmodSync(DIR, 0o700);
}

/** Write a private file: temp file at 0600, then rename, so no reader ever sees it half-written or open. */
function writePrivate(path, data) {
  ensureDir();
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, data, { mode: 0o600 });
  if (!WIN) chmodSync(tmp, 0o600);
  renameSync(tmp, path);
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function removeIfThere(path) {
  if (existsSync(path)) unlinkSync(path);
}

const device = () => {
  const d = readJson(DEVICE);
  return d && typeof d.secret === "string" && d.secret.length >= 40 ? d : null;
};

// ── network ──────────────────────────────────────────────────────────────────
async function call(route, { method = "GET", body, secret, limitMs = 15_000 } = {}) {
  const headers = { accept: "application/json" };
  if (body) headers["content-type"] = "application/json";
  if (secret) headers.authorization = `Bearer ${secret}`;
  const res = await fetch(`${BASE}/${route}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(limitMs),
  });
  let data = {};
  try {
    data = await res.json();
  } catch { /* an empty or non-JSON answer */ }
  return { status: res.status, data };
}

const offline = (e) =>
  e?.name === "TimeoutError" || e?.name === "AbortError" || e?.cause?.code === "ENOTFOUND" ||
  e?.cause?.code === "ECONNREFUSED" || e?.cause?.code === "ECONNRESET" || e?.message === "fetch failed";

function computerName() {
  // On a Mac, the name the person gave it in System Settings ("Sam's MacBook Pro");
  // the host name is often just "Mac".
  if (platform() === "darwin") {
    try {
      const n = execFileSync("scutil", ["--get", "ComputerName"], { encoding: "utf8", timeout: 2000 }).trim();
      if (n) return n.slice(0, 100);
    } catch { /* fall back to the host name */ }
  }
  const host = hostname().replace(/\.local$/i, "").replace(/[-_]+/g, " ").trim();
  let user = "";
  try {
    user = userInfo().username;
  } catch { /* no user name on this system */ }
  return (host || (user ? `${user}'s computer` : "Computer")).slice(0, 100);
}

// ── link ─────────────────────────────────────────────────────────────────────
async function linkStart(name) {
  if (device()) {
    say("This computer is already connected to a Praxis account.");
    say("To connect it to a different account, first run: unlink");
    return;
  }
  let r;
  try {
    r = await call("link-start", { method: "POST", body: { computer_name: name || computerName() } });
  } catch (e) {
    fail(offline(e) ? "Could not reach Praxis. Check the internet connection and try again." : `Could not start: ${e.message}`);
  }
  if (r.status !== 200 || !r.data.code || !r.data.poll_secret) {
    fail(r.data.message || `Praxis did not give a code (status ${r.status}). Try again in a minute.`);
  }
  writePrivate(PENDING, JSON.stringify({
    poll_secret: r.data.poll_secret,
    code: r.data.code,
    expires_at: r.data.expires_at,
    poll_seconds: r.data.poll_seconds || 3,
  }));
  const address = `${r.data.connect_url}?code=${encodeURIComponent(r.data.code)}`;
  say(`Code: ${r.data.code}`);
  say(`Open: ${address}`);
  say(`The code works for 10 minutes. Sign in there, check the computer's name and the code, then press Connect this computer.`);
}

async function linkWait() {
  const pending = readJson(PENDING);
  if (!pending?.poll_secret) {
    if (device()) return say("This computer is already connected to a Praxis account.");
    fail("There is no code waiting. Start again with: link");
  }
  const until = Math.min(Date.now() + WAIT_LIMIT_MS, Date.parse(pending.expires_at) || Infinity);
  const every = Math.max(1, Number(pending.poll_seconds) || 3) * 1000;
  let misses = 0;
  while (Date.now() < until) {
    let r;
    try {
      r = await call("link-poll", { method: "POST", body: { poll_secret: pending.poll_secret }, limitMs: 10_000 });
      misses = 0;
    } catch (e) {
      if (!offline(e) || ++misses > 5) fail("Lost the connection to Praxis while waiting. Start again with: link");
      await sleep(every);
      continue;
    }
    if (r.status === 200 && r.data.status === "ok" && r.data.device_secret) {
      writePrivate(DEVICE, JSON.stringify({
        v: 1,
        device_id: r.data.device_id,
        account_code: r.data.account_code,
        secret: r.data.device_secret,
        linked_at: new Date().toISOString(),
      }, null, 2) + "\n");
      removeIfThere(PENDING);
      installSelf();
      say(`Connected. This computer is now linked to the Praxis account ${r.data.account_code}.`);
      return;
    }
    if (r.status === 200 && r.data.status === "waiting") {
      await sleep(every);
      continue;
    }
    removeIfThere(PENDING);
    fail(r.data.message || `The code was not approved (status ${r.status}). Start again with: link`);
  }
  if (Date.now() >= (Date.parse(pending.expires_at) || Infinity)) {
    removeIfThere(PENDING);
    fail("The code expired before it was approved. Start again with: link");
  }
  fail("Still waiting for the code to be approved. Run link --wait again to keep waiting.", 4);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Keep a copy of this script at ~/.praxis/praxis-connect.mjs: the settings entry calls it there, and
 *  a plugin update can move or replace the plugin's own folder. */
function installSelf() {
  const me = fileURLToPath(import.meta.url);
  if (me === SELF_COPY) return;
  ensureDir();
  copyFileSync(me, SELF_COPY);
  if (!WIN) chmodSync(SELF_COPY, 0o700);
}

// ── settings ─────────────────────────────────────────────────────────────────
export function settingsEntry() {
  const helper = WIN
    ? 'node "%USERPROFILE%\\.praxis\\praxis-connect.mjs" headers'
    : 'node "$HOME/.praxis/praxis-connect.mjs" headers';
  return {
    source: { source: "url", url: `${BASE}/marketplace.json`, headersHelper: helper },
    autoUpdate: true,
  };
}

function readSettings() {
  if (!existsSync(SETTINGS)) return {};
  const raw = readFileSync(SETTINGS, "utf8");
  if (!raw.trim()) return {};
  try {
    const s = JSON.parse(raw);
    if (s && typeof s === "object" && !Array.isArray(s)) return s;
  } catch { /* fall through */ }
  fail(`${SETTINGS} is not valid JSON, so nothing was changed. Fix that file first.`, 2);
}

function settings(write) {
  const entry = settingsEntry();
  const current = readSettings().extraKnownMarketplaces?.[CATALOG];
  const same = JSON.stringify(current) === JSON.stringify(entry);
  if (!write) {
    say(`File: ${SETTINGS}`);
    say(same ? "Already there, nothing to change:" : current ? "Will replace the existing praxis-construction entry with:" : "Will add, under extraKnownMarketplaces:");
    say(JSON.stringify({ [CATALOG]: entry }, null, 2));
    return;
  }
  if (!device()) fail("Connect this computer first (link), then add the settings entry.");
  if (same) return say(`${SETTINGS} already has the praxis-construction entry. Nothing changed.`);
  const s = readSettings();
  mkdirSync(dirname(SETTINGS), { recursive: true });
  if (existsSync(SETTINGS)) copyFileSync(SETTINGS, SETTINGS_BACKUP);
  s.extraKnownMarketplaces = { ...(s.extraKnownMarketplaces || {}), [CATALOG]: entry };
  const tmp = `${SETTINGS}.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(s, null, 2) + "\n");
  renameSync(tmp, SETTINGS);
  installSelf();
  say(`Added the praxis-construction entry to ${SETTINGS}.`);
  if (existsSync(SETTINGS_BACKUP)) say(`The previous file is kept at ${SETTINGS_BACKUP}.`);
  say("Open a new chat so Claude Code picks up the Praxis catalog.");
}

function removeSettingsEntry() {
  const s = readSettings();
  if (!s.extraKnownMarketplaces?.[CATALOG]) return false;
  copyFileSync(SETTINGS, SETTINGS_BACKUP);
  delete s.extraKnownMarketplaces[CATALOG];
  if (!Object.keys(s.extraKnownMarketplaces).length) delete s.extraKnownMarketplaces;
  writeFileSync(SETTINGS, JSON.stringify(s, null, 2) + "\n");
  return true;
}

// ── headers, status, unlink ──────────────────────────────────────────────────
function headers() {
  const d = device();
  if (!d) process.exit(1);
  process.stdout.write(JSON.stringify({ Authorization: `Bearer ${d.secret}` }));
}

async function status() {
  const d = device();
  const inSettings = !!readSettings().extraKnownMarketplaces?.[CATALOG];
  if (!d) {
    say("This computer is not connected to a Praxis account. Say: connect this computer.");
    if (readJson(PENDING)) say("A code is waiting for approval. Run link --wait after approving it.");
    return void (process.exitCode = 3);
  }
  say(`Connected to the Praxis account ${d.account_code} since ${String(d.linked_at).slice(0, 10)}.`);
  say(inSettings ? "Claude Code settings: the Praxis catalog entry is in place." : "Claude Code settings: the Praxis catalog entry is missing. Run settings to see it.");
  try {
    const r = await call("pass", { secret: d.secret, limitMs: CHECK_LIMIT_MS });
    if (r.status === 200 && r.data.expires_at) {
      say(`Account active. This computer's pass is good until ${r.data.expires_at.slice(0, 10)}.`);
    } else {
      say(r.data.message || `Praxis answered ${r.status}.`);
      process.exitCode = 3;
    }
  } catch (e) {
    say(offline(e) ? "Could not reach Praxis just now, so the account was not checked." : `Check failed: ${e.message}`);
    process.exitCode = 5;
  }
}

function unlink(alsoSettings) {
  const had = !!device();
  removeIfThere(DEVICE);
  removeIfThere(PENDING);
  const removed = alsoSettings ? removeSettingsEntry() : false;
  say(had ? "This computer's Praxis key is deleted from this computer." : "This computer had no Praxis key.");
  if (removed) say(`Took the praxis-construction entry out of ${SETTINGS}.`);
  if (had) say("To remove it from the account's list as well, use Remove on https://app.praxisatwork.com/connect.");
}

// ── main ─────────────────────────────────────────────────────────────────────
const isMain = process.argv[1] && fileURLToPath(import.meta.url) === (await import("node:fs")).realpathSync(process.argv[1]);
if (isMain) {
  const [cmd, ...rest] = process.argv.slice(2);
  const flag = (f) => rest.includes(f);
  const nameAt = rest.indexOf("--name");
  try {
    if (cmd === "link") await (flag("--wait") ? linkWait() : linkStart(nameAt >= 0 ? rest[nameAt + 1] : undefined));
    else if (cmd === "settings") settings(flag("--write"));
    else if (cmd === "headers") headers();
    else if (cmd === "status") await status();
    else if (cmd === "unlink") unlink(flag("--settings"));
    else fail("Use one of: link, link --wait, settings, settings --write, headers, status, unlink [--settings]", 2);
  } catch (e) {
    fail(`Something went wrong: ${e.message}`);
  }
}
