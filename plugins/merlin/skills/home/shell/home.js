/* Merlin home: the page script.

   PAGE-TO-SERVER CONTRACT, version 1. The home server implements exactly this.

   1. Files. home.html loads home.css, home.js, fonts.css and the two logo files
      (merlin-logo-master-dark.svg, merlin-logo-master-light.svg) by relative address from its
      own folder. The page asks no other host for anything, ever.

   2. First paint. home.html ships as
          <html lang="en" data-mode="dark" data-layout="top" data-rail="closed">
      The server may rewrite the two values data-mode="dark|light" and data-layout="top|side"
      when it serves home.html, to the saved look and menu position. Nothing else in the file
      is rewritten. This script applies the saved settings from data.json as well.

   3. Data. One request: GET data.json (relative address, so it sits beside home.html),
      answered with JSON:
        {
          "home": <the dict home_data.read_home() returns for the business folder>,
          "settings": {"menu": "top" | "side", "look": "dark" | "light"},
          "actions": {
            "settings":    <address>,   saves both settings
            "open_url":    <address>,   opens an address in the computer's own browser
            "open_folder": <address>    opens a project folder in Finder or File Explorer
          },
          "addon_pages": {
            "<add-on id>": {"state": "activated" | "bought-not-installed" | "not-activated",
                            "url": <address to frame> or null,
                            "sample": <address of the sample page> or null}
          }
        }
      A missing "settings" value means the default: menu "top", look "dark". A missing action
      means the page cannot do that thing; it says so in a short note and changes nothing.

   4. Actions. The server answers GET and HEAD only, so every action is a GET. Each address
      in "actions" already carries its own key. The page never edits what the server put in
      the address: it parses it with new URL(address, location.href) and adds its own values
      with searchParams.set, then sends GET with fetch, same origin, no body. These values:
        settings     menu=top|side and look=dark|light, always both
        open_url     url=<the full https://www.praxisatwork.com/... address>; the page sends
                     no other address
        open_folder  id=<a project's "id", or a construction item's "id", from the home data>;
                     never a path. The server finds the folder by reading its own data.
      An action answers 204 when it worked. Any other answer, or no answer, shows the person
      a short "did not work" note.

   5. Add-ons. The menu holds one link per entry of home.addons. When addon_pages has an entry
      for the add-on, its "state" wins over home.addons[].state; with no entry the page uses
      home.addons[].state with url and sample null.
        activated, url set       the link opens url in a frame, with merlin-home=1 and
                                 look=<light|dark> added to its query. When the look changes
                                 the home posts {type:"merlin-home", v:1, look:"<look>"} to the
                                 frame, aimed at the frame address's own origin.
        activated, url null      the link opens a list page in the home (empty in this
                                 version).
        not activated, or bought but not installed
                                 the link opens sample in a frame, greyed out and switched off
                                 (inert), under a banner. The sample address gets the same
                                 merlin-home=1 and look values. An add-on with sample null and
                                 url null gets no menu link and no Plugins card.
      A new frame element is made every time a plugin page opens; one is never reused.
      An add-on's "sample_pages" (a list of names, from its definition) makes a not-activated
      sample move through those pages by itself every 5 seconds, by location.replace on the
      frame to <sample address>#<name>, so no Back step is added. It stands still when the
      computer asks for reduced motion.

   6. Addresses the page opens itself: "claude://code/new?folder=<the project's path,
      encodeURIComponent>" for Open in Claude, and #projects, #project/<name>, #settings,
      #<add-on id> inside the home (#jobs lands on #construction; an address the home does
      not know shows Projects). Moving between them never reloads the page.
*/
(function () {
  'use strict';

  var H = document.documentElement;
  var SITE = 'https://www.praxisatwork.com/';
  var GUIDES = SITE + 'guides';
  var STALE_DAYS = 30;
  var FIRST_ROWS = 3;
  var FIRST_TASKS = 4;
  var FIRST_LINES = 4;
  var TOUR_MS = 5000;
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

  var ICONS = {
    folders: '<path d="M20 5a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h2.5a1.5 1.5 0 0 1 1.2.6l.6.8a1.5 1.5 0 0 0 1.2.6z"/><path d="M3 8.268a2 2 0 0 0-1 1.738V19a2 2 0 0 0 2 2h11a2 2 0 0 0 1.732-1"/>',
    megaphone: '<path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1z"/><path d="M15 9a4 4 0 0 1 0 6"/><path d="M18 6a8 8 0 0 1 0 12"/>',
    'hard-hat': '<path d="M10 10V5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v5"/><path d="M14 6a6 6 0 0 1 6 6v3"/><path d="M4 15v-3a6 6 0 0 1 6-6"/><rect x="2" y="15" width="20" height="4" rx="1"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    box: '<rect x="3" y="3" width="18" height="18" rx="3"/>'
  };

  /* The 11 base skills, their groups and their "Say" lines. */
  var SKILLS = [
    ['Getting set up', [
      ['Set up your profile', 'Teaches Merlin who you are and how you write.', 'set up my profile'],
      ['Set up your company', 'Saves what your business does, its words and its playbooks.', 'set up my company info'],
      ['Start a project', 'Makes a project folder with a brief and a status file.', 'set up a project'],
      ['Turn a folder into a project', 'Adopts a folder you already have, without moving your files.', 'make this folder a project']]],
    ['Every day', [
      ['Keep your task list', 'Adds, closes and reorders tasks, grouped by project.', 'what’s on my plate'],
      ['Draft an email', 'Writes one email in your voice. You press Send.', 'draft an email to Dana about the quote'],
      ['Sound like you', 'Edits a draft so it reads the way you write.', 'edit this so it sounds like me'],
      ['Pull the asks out of a meeting', 'Lists every ask and number from a transcript, each with its quote.', 'dissect this meeting']]],
    ['Housekeeping', [
      ['Save and close', 'Saves decisions and the next step, then closes the chat.', 'save and close'],
      ['Check your setup', 'Checks that Merlin’s files and folders are in place.', 'check my Merlin setup'],
      ['Show the rules', 'Shows the rules Merlin works by.', 'show me Merlin’s rules']]]
  ];

  /* Words for the add-ons the mock-up names, by add-on id. */
  var ADDON_WORDS = {
    construction: {
      blurb: 'Gives each construction project its own dashboard: takeoff, bids, budget, contracts, schedule, bills.',
      sample: 'This is a sample project. With the plugin on, each of your construction projects gets a dashboard like this.',
      open: 'See your construction projects'
    },
    marketing: {
      blurb: 'Your posts, your numbers and your content calendar, on one page.',
      sample: 'This is a sample with example numbers. With the plugin on, it shows your own posts, numbers and content calendar.',
      open: 'Open Marketing'
    }
  };

  var DATA = null;
  var HOME = null;
  var SETTINGS = { menu: 'top', look: 'dark' };
  var ACTIONS = {};
  var PAGES = {};
  var state = { kind: 'reg', filt: 'all', frameKey: null, frameSrc: null, frameLive: false, tour: null };

  /* ---------------------------------------------------------------- small helpers */

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return [].slice.call((r || document).querySelectorAll(s)); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function add(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null || c === false) continue;
      parent.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return parent;
  }

  function icon(name, cls) {
    var ns = 'http://www.w3.org/2000/svg';
    var s = document.createElementNS(ns, 'svg');
    s.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
    s.setAttribute('viewBox', '0 0 24 24');
    s.innerHTML = ICONS[name] || ICONS.box;
    return s;
  }

  function plain(s) {
    if (s == null) return '';
    return String(s)
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/(\*\*|__)(.+?)\1/g, '$2')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/^#+\s*/, '')
      .trim();
  }

  function base(p) {
    if (!p) return '';
    var parts = String(p).split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(p);
  }

  function shortPath(p) {
    var parts = String(p || '').split(/[\\/]+/).filter(Boolean);
    return parts.slice(-2).join(' / ');
  }

  function ymd(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }

  function fmtShort(s) {
    var d = ymd(s);
    return d ? MONTHS[d.getMonth()].slice(0, 3) + ' ' + d.getDate() : '';
  }

  function fmtTime(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || '');
    if (!m) return '';
    var h = +m[4], ap = h < 12 ? 'AM' : 'PM';
    h = h % 12 || 12;
    return MONTHS[+m[2] - 1].slice(0, 3) + ' ' + (+m[3]) + ', ' + h + ':' + m[5] + ' ' + ap;
  }

  function daysSince(s) {
    var d = ymd(s), t = ymd(HOME && HOME.today);
    if (!d || !t) return null;
    return Math.max(0, Math.round((t - d) / 86400000));
  }

  function ago(n) {
    return n === 0 ? 'updated today' : n === 1 ? 'updated yesterday' : 'updated ' + n + ' days ago';
  }

  function claudeLink(path) {
    return 'claude://code/new?folder=' + encodeURIComponent(path);
  }

  function withParams(address, params) {
    var u = new URL(address, location.href);
    Object.keys(params).forEach(function (k) { u.searchParams.set(k, params[k]); });
    return u.href;
  }

  var toastTimer = null;
  function toast(m) {
    var t = $('#toast');
    t.textContent = m;
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('on'); }, 2600);
  }

  var FAILED = 'That did not work. Ask Claude to open your Merlin home again.';

  function act(name, params) {
    var address = ACTIONS[name];
    if (!address) { toast(FAILED); return Promise.resolve(false); }
    var url;
    try { url = withParams(address, params); } catch (e) { toast(FAILED); return Promise.resolve(false); }
    return fetch(url, { method: 'GET', cache: 'no-store', credentials: 'same-origin' }).then(function (r) {
      if (r.status !== 204) { toast(FAILED); return false; }
      return true;
    }, function () { toast(FAILED); return false; });
  }

  function openAddress(u) {
    if (typeof u !== 'string' || u.indexOf(SITE) !== 0) return;
    act('open_url', { url: u });
  }

  function copyLine(txt) {
    var done = function () { toast('Copied: "' + txt + '". Paste it into any Claude chat.'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(done, function () { fallbackCopy(txt); done(); });
    } else { fallbackCopy(txt); done(); }
  }

  function fallbackCopy(txt) {
    try {
      var a = el('textarea');
      a.value = txt;
      a.setAttribute('readonly', '');
      a.style.position = 'fixed';
      a.style.opacity = '0';
      document.body.appendChild(a);
      a.select();
      document.execCommand('copy');
      a.remove();
    } catch (e) { /* nothing more to try */ }
  }

  function sayRow(line) {
    var row = el('div', 'say');
    var b = el('button', 'cp');
    b.type = 'button';
    b.setAttribute('aria-label', 'Copy');
    b.dataset.copy = line;
    add(b, icon('copy'));
    return add(row, el('i', null, 'Say'), el('q', null, line), b);
  }

  function setSeg(id, v) {
    $$('#' + id + ' button').forEach(function (b) { b.setAttribute('aria-pressed', b.dataset.v === v ? 'true' : 'false'); });
  }

  /* ---------------------------------------------------------------- add-ons */

  function addons() {
    return ((HOME && HOME.addons) || []).map(function (a) {
      var p = PAGES[a.id] || {};
      return {
        id: a.id, name: a.name || a.label || a.id, label: a.label || a.name || a.id, icon: a.icon,
        marker: a.marker, product_page: a.product_page, sample_pages: a.sample_pages,
        state: p.state || a.state || 'not-activated', url: p.url || null, sample: p.sample || null
      };
    }).filter(function (a) {
      return a.id && (a.state === 'activated' || a.url || a.sample);
    });
  }

  function addonById(id) {
    var all = addons();
    for (var i = 0; i < all.length; i++) if (all[i].id === id) return all[i];
    return null;
  }

  /* Construction folders leave Projects only when the add-on that owns them is on and has a list page. */
  function splitJobs() {
    return addons().some(function (a) { return a.marker === 'job.json' && a.state === 'activated' && a.url; });
  }

  /* ---------------------------------------------------------------- projects */

  function regularProjects() {
    var split = splitJobs();
    var out = (HOME.projects || []).filter(function (p) { return !(split && p.has_job_json); });
    if (!split) {
      ((HOME.construction_projects || {}).items || []).forEach(function (j) {
        if (!j.in_projects) out.push(j);
      });
    }
    return out;
  }

  function findProject(name) {
    var all = regularProjects();
    for (var i = 0; i < all.length; i++) if (all[i].name === name) return all[i];
    return null;
  }

  function updatedDays(p) {
    var u = p.continuity && p.continuity.updated;
    return u && u.date ? daysSince(u.date) : null;
  }

  function projectCard(p) {
    var c = el('div', 'card pc click');
    c.tabIndex = 0;
    c.setAttribute('role', 'link');
    c.setAttribute('aria-label', p.name);
    c.dataset.go = '#project/' + encodeURIComponent(p.name);
    var who = el('div');
    who.style.minWidth = '0';
    add(who, el('div', 'nm', p.name), el('div', 'lab', 'Project'));
    add(c, add(el('div', 'row1'), el('div', 'mono', p.name.slice(0, 2)), who));
    var status = p.continuity && p.continuity.status;
    if (status) add(c, el('p', 'st', plain(status)));
    var meta = el('div', 'meta');
    var t = p.tasks || { open: 0, p1: 0 };
    if (t.p1) add(meta, el('span', 'pill act', t.p1 + ' priority 1'));
    add(meta, add(el('span'), el('b', null, String(t.open)), t.open === 1 ? ' open task' : ' open tasks'));
    var days = updatedDays(p);
    if (days != null) add(meta, el('span', days >= STALE_DAYS ? 'stale' : '', ago(days)));
    add(c, meta);
    if (p.path) {
      var foot = el('div', 'foot');
      var a = el('a', 'ghost', 'Open in Claude');
      a.href = claudeLink(p.path);
      add(c, add(foot, a));
    }
    return c;
  }

  function drawProjects() {
    var con = state.kind === 'con';
    var split = splitJobs();
    var q = ($('#q').value || '').toLowerCase();
    var filt = state.filt;
    var list = con ? [] : regularProjects();
    var grid = $('#pgrid');
    grid.textContent = '';
    var shown = 0, withP1 = 0;
    list.forEach(function (p) {
      if (q && p.name.toLowerCase().indexOf(q) < 0) return;
      if (filt === 'p1' && !(p.tasks && p.tasks.p1)) return;
      if (filt === 'stale') { var d = updatedDays(p); if (d == null || d < STALE_DAYS) return; }
      shown++;
      if (p.tasks && p.tasks.p1) withP1++;
      grid.appendChild(projectCard(p));
    });
    var plainView = !q && filt === 'all';
    var bare = !shown && plainView;
    var empty = $('#pempty');
    empty.hidden = shown > 0;
    empty.textContent = '';
    if (!plainView) empty.textContent = 'No project matches.';
    else if (con) empty.textContent = 'Nothing here yet.';
    else if (split) {
      var link = el('a', null, 'Construction');
      link.href = '#construction';
      add(empty, 'Your construction projects are on the ', link, ' page.');
    } else empty.textContent = 'Nothing here yet.';

    if (plainView) {
      var hero;
      if (con) hero = 'No construction projects yet.';
      else if (shown) hero = shown + (shown === 1 ? ' project.' : ' projects.') + (withP1 ? ' ' + withP1 + (withP1 === 1 ? ' has' : ' have') + ' priority 1 work.' : '');
      else hero = split ? 'No regular projects yet.' : 'No projects yet.';
      $('#heroLine').textContent = hero;
    }
    var conAddon = addonById('construction');
    $('#plead').textContent = con
      ? 'Every project made with ' + (conAddon ? conAddon.name : 'Merlin for Construction') + '. Click a card to open its dashboard.'
      : bare && split ? 'Regular projects show here once you start one.'
        : split ? 'Your regular project folders. Construction projects have their own page. Click a card to see where it stands.'
          : 'Every project folder in your business folder. Click a card to see where it stands.';
    var ph = con ? 'Find a construction project' : 'Find a project';
    $('#q').placeholder = ph;
    $('#q').setAttribute('aria-label', ph);
    $('#segFilter').style.display = con ? 'none' : '';
    $('#ptools').hidden = bare;

    var np = $('#newproj');
    np.hidden = con;
    np.textContent = '';
    if (!con) {
      add(np, el('b', null, 'Start a new project.'), ' Open a Claude chat on your company folder, ',
        el('b', null, base(HOME.business.folder)), '. Say "start a new project". Claude makes the project folder. Nothing is made from this page.');
    }
  }

  /* ---------------------------------------------------------------- one project */

  function listCard(label, count, src) {
    var c = el('div', 'card');
    var lab = el('div', 'lab', label + (count != null ? ' · ' + count : ''));
    var ul = el('ul', 'list');
    add(c, lab, ul);
    return { card: c, list: ul, src: function (s) { add(c, el('div', 'src', s)); } };
  }

  function fillList(ul, rows, first, total) {
    rows.forEach(function (r, i) {
      if (i >= first) r.hidden = true;
      ul.appendChild(r);
    });
    if (rows.length > first) {
      var li = el('li');
      var b = el('button', 'tl');
      b.type = 'button';
      var openText = total === rows.length ? 'Show all ' + total : 'Show the ' + rows.length + ' newest';
      b.textContent = openText;
      b.addEventListener('click', function () {
        var open = b.dataset.open !== '1';
        b.dataset.open = open ? '1' : '';
        rows.forEach(function (r, i) { if (i >= first) r.hidden = !open; });
        b.textContent = open ? 'Show less' : openText;
      });
      ul.appendChild(add(li, b));
    }
  }

  function noneRow(text) {
    return el('li', 'none', text);
  }

  function dated(text, date) {
    var li = el('li');
    add(li, el('span', null, text));
    if (date) add(li, el('span', 'r', date));
    return li;
  }

  function fileCard(label, files, folderName, emptyText, withCount) {
    var f = files || { count: 0, newest: [] };
    var k = listCard(label, withCount ? f.count : null);
    if (!f.newest || !f.newest.length) k.list.appendChild(noneRow(emptyText));
    else fillList(k.list, f.newest.map(function (x) { return dated(x.name, fmtShort(x.saved)); }), FIRST_ROWS, f.count);
    k.src('from ' + folderName);
    return k.card;
  }

  function drawProject(p) {
    $('#pkind').textContent = 'Project';
    $('#pname').textContent = p.name;
    var c = p.continuity;
    var status = c && c.status;
    $('#pstate').textContent = status ? plain(status) : '';
    $('#pstate').hidden = !status;
    var cl = $('#pclaude');
    if (p.path) { cl.href = claudeLink(p.path); cl.hidden = false; } else { cl.removeAttribute('href'); cl.hidden = true; }
    var fo = $('#pfolder');
    fo.dataset.id = p.id || '';
    fo.hidden = !p.id;

    var cards = $('#pcards');
    cards.textContent = '';

    var nx = el('div', 'card');
    add(nx, el('div', 'lab', 'Next move'));
    if (c && c.next_move) add(nx, el('p', null, plain(c.next_move))).lastChild.style.margin = '0';
    else add(nx, el('p', 'saved none', 'No next move saved yet. To add one, tell Claude: "save and close".'));
    var days = updatedDays(p);
    add(nx, el('div', 'src', 'from CONTINUITY.md' + (days != null ? ' · ' + ago(days) + ' (' + fmtShort(c.updated.date) + ')' : '')));
    cards.appendChild(nx);

    var dec = listCard('Recent decisions');
    var ds = (c && c.decisions) || [];
    if (!ds.length) dec.list.appendChild(noneRow('No decisions saved yet. To add one, tell Claude: "save and close".'));
    else ds.forEach(function (d) { dec.list.appendChild(dated(plain(d))); });
    dec.src('from CONTINUITY.md');
    cards.appendChild(dec.card);

    var t = p.tasks || { open: 0, lines: [] };
    var tk = listCard('Open tasks', t.open);
    var lines = (t.lines || []).slice().sort(function (a, b) { return (b.priority === 'P1') - (a.priority === 'P1'); });
    if (!lines.length) tk.list.appendChild(noneRow('No open tasks. To add one, tell Claude: "add a task".'));
    else {
      fillList(tk.list, lines.map(function (x) {
        var li = el('li');
        if (x.priority === 'P1') add(li, el('span', 'pill act', 'Priority 1'));
        add(li, el('span', null, plain(x.title || '')));
        if (x.date) add(li, el('span', 'r', fmtShort(x.date)));
        return li;
      }), FIRST_TASKS, t.open);
    }
    var listFile = lines.length && lines[0].list ? base(lines[0].list) : (HOME.tasks && HOME.tasks.lists && HOME.tasks.lists[0] ? base(HOME.tasks.lists[0]) : 'TASKS.md');
    tk.src('from ' + listFile);
    cards.appendChild(tk.card);

    var fl = p.files || {};
    cards.appendChild(fileCard('Latest files', fl.outputs, 'outputs/', 'No files saved yet. To add one, tell Claude: "save this to the project".', true));
    cards.appendChild(fileCard('Meetings', fl.meetings, 'knowledge/meetings/', 'No meetings saved yet. To add one, tell Claude: "dissect this meeting".', true));
    cards.appendChild(fileCard('Session log', fl.updates, 'updates/', 'No sessions saved yet. To add one, tell Claude: "save and close".', false));
  }

  /* ---------------------------------------------------------------- settings */

  function firstLines(text) {
    var out = [];
    String(text || '').split('\n').forEach(function (l) {
      if (out.length < FIRST_LINES && l.trim()) out.push(l.replace(/\s+$/, ''));
    });
    return out.join('\n');
  }

  /* Saved text as the person wrote it, minus the marks that only mean layout: a heading line
     loses its leading # signs and shows bold, and ** pairs around a word are dropped. */
  function savedBlock(cls, text) {
    var p = el('p', cls);
    String(text).split('\n').forEach(function (l, i) {
      if (i) p.appendChild(document.createTextNode('\n'));
      var h = /^#{1,6}\s+(.*)$/.exec(l);
      var t = (h ? h[1] : l).replace(/\*\*(.+?)\*\*/g, '$1');
      if (h) add(p, el('b', null, t)); else p.appendChild(document.createTextNode(t));
    });
    return p;
  }

  function savedCard(title, files, sayLine, src, extra) {
    var c = el('div', 'card');
    add(c, el('h2', null, title));
    var texts = files.filter(function (f) { return f && f.text != null && String(f.text).trim(); });
    if (!texts.length) {
      add(c, el('p', 'saved none', 'Nothing saved yet.'));
    } else {
      var first = firstLines(texts[0].text);
      add(c, savedBlock('saved first', first));
      if (extra) add(c, el('p', 'saved', extra)).lastChild.style.color = 'var(--app-muted)';
      var more = el('div', 'more');
      texts.forEach(function (f) {
        if (f.label) add(more, el('div', 'lab', f.label));
        add(more, savedBlock('saved', String(f.text).replace(/\s+$/, '')));
      });
      add(c, more);
      var b = el('button', 'tl', 'Show all');
      b.type = 'button';
      b.dataset.more = '1';
      add(c, b);
    }
    add(c, sayRow(sayLine));
    (Array.isArray(src) ? src : [src]).forEach(function (s) { if (s) add(c, el('div', 'src', s)); });
    return c;
  }

  function fileSrc(f, fallback) {
    return f ? f.file + (f.saved ? ' · updated ' + fmtShort(f.saved) : '') : fallback;
  }

  function drawSettings() {
    var am = HOME.about_me || {};
    var box = $('#aboutCards');
    box.textContent = '';
    box.appendChild(savedCard('About me', [am.about_me], 'update my about-me', fileSrc(am.about_me, 'about-me.md')));

    var bz = HOME.businesses || { count: 0, items: [] };
    var bfiles = [];
    (bz.items || []).forEach(function (b) {
      (b.files || []).forEach(function (f, i) {
        bfiles.push({ text: f.text, label: (bz.items.length > 1 || b.files.length > 1) ? b.name + ' · ' + f.file : (i === 0 ? null : f.file) });
      });
    });
    var bsrc = 'memory/businesses/' + (bz.count ? ' · ' + bz.count + (bz.count === 1 ? ' business' : ' businesses') : '') + (bz.saved ? ' · updated ' + fmtShort(bz.saved) : '');
    box.appendChild(savedCard('My company', bfiles, 'update my company info', bsrc));

    var v = am.voice;
    var samples = v && v.samples ? v.samples + (v.samples === 1 ? ' piece of your own writing saved' : ' pieces of your own writing saved') : null;
    box.appendChild(savedCard('My writing style', [v], 'add a writing sample', fileSrc(v, 'voice.md'), samples));

    var sg = am.email_signature, tp = am.email_template;
    var srcs = [fileSrc(sg, 'email-signature.md')];
    if (tp && !tp.is_starter) srcs.push(fileSrc(tp));
    box.appendChild(savedCard('Email sign-off', [sg], 'change my email signature', srcs));

    var sk = $('#skills');
    sk.textContent = '';
    SKILLS.forEach(function (g) {
      var grid = el('div', 'grid');
      g[1].forEach(function (s) {
        grid.appendChild(add(el('div', 'card sk'), el('h3', null, s[0]), el('p', null, s[1]), sayRow(s[2])));
      });
      sk.appendChild(add(el('div', 'grp'), el('div', 'lab', g[0]), grid));
    });

    var pc = $('#pluginCards');
    pc.textContent = '';
    var mv = HOME.plugins && HOME.plugins['merlin'];
    var head = add(el('div', 'cardhead'), el('h2', null, 'Merlin'));
    if (mv && mv.numbers) add(head, el('span', 'pill', 'Version ' + mv.numbers.join('.')));
    pc.appendChild(add(el('div', 'card'), head, el('p', 'lead', 'The base. Projects, tasks, your profile, email drafts, meetings.')));
    var list = addons();
    list.forEach(function (a) {
      var words = ADDON_WORDS[a.id] || {};
      var on = a.state === 'activated';
      var pill = on ? el('span', 'pill', 'Activated')
        : el('span', 'pill off', a.state === 'bought-not-installed' ? 'Not installed' : 'Not activated');
      var card = add(el('div', 'card'), add(el('div', 'cardhead'), el('h2', null, a.name), pill));
      if (words.blurb) add(card, el('p', 'lead', words.blurb));
      var link = el('a', 'tl', on ? (words.open || 'Open ' + a.label) : 'See a sample');
      link.href = '#' + a.id;
      add(card, link);
      pc.appendChild(card);
    });
    if (list.length) {
      var g = el('a', 'tl', 'Open the guides');
      g.href = GUIDES;
      g.dataset.guides = '1';
      pc.appendChild(add(el('div', 'card'), el('h2', null, 'Add-ons'), el('p', 'lead', 'Each add-on has a sample in the menu. The guides say what each one does.'), g));
    }

    var kv = $('#installKv');
    kv.textContent = '';
    var b = HOME.business || {};
    var rows = [['Business folder', shortPath(b.folder), b.folder]];
    if (b.hq) rows.push(['Merlin’s own files', b.hq === b.folder ? shortPath(b.hq) : shortPath(b.folder) + ' / ' + base(b.hq), b.hq]);
    rows.push(['Projects', String(regularProjects().length)]);
    rows.forEach(function (r) {
      var dd = el('dd', null, r[1]);
      if (r[2]) dd.title = r[2];
      add(kv, el('dt', null, r[0]), dd);
    });
    var help = el('a', 'tl', 'Guides');
    help.href = GUIDES;
    help.dataset.guides = '1';
    add(kv, el('dt', null, 'Help'), add(el('dd'), help));

    var from = b.hq ? (b.layout === 'hq' ? base(b.hq) + '/context' : base(b.hq)) + ' and the installed plugins' : 'the installed plugins';
    $('#sfoot').textContent = 'Read from ' + from + ' · read ' + fmtTime(HOME.generated_at);
  }

  /* ---------------------------------------------------------------- menu */

  function drawMenu() {
    $$('.addon-links').forEach(function (box) {
      box.textContent = '';
      var side = box.dataset.place === 'side';
      addons().forEach(function (a) {
        var off = a.state !== 'activated';
        var link = el('a', 'nl' + (off ? ' off' : ''));
        link.href = '#' + a.id;
        link.dataset.nav = a.id;
        link.setAttribute('aria-label', a.label + (off ? ', add-on, not activated' : ''));
        add(link, icon(a.icon), el('span', null, a.label));
        if (side) {
          link.dataset.tip = off ? a.label + ' · add-on, not activated' : a.label;
          add(link, el('em', 'tag', off ? 'Add-on' : 'Plugin'));
        } else if (off) {
          add(link, icon('lock', 'lk'));
        }
        box.appendChild(link);
      });
    });
  }

  /* ---------------------------------------------------------------- plugin frame */

  function stopTour() {
    if (state.tour) clearInterval(state.tour);
    state.tour = null;
  }

  function startTour(src, pages) {
    stopTour();
    if (!pages || pages.length < 2) return;
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var i = 0;
    var root = src.split('#')[0];
    state.tour = setInterval(function () {
      var f = $('#framewrap iframe');
      if (!f) { stopTour(); return; }
      i = (i + 1) % pages.length;
      try { f.contentWindow.location.replace(root + '#' + encodeURIComponent(pages[i])); } catch (e) { stopTour(); }
    }, TOUR_MS);
  }

  function closeFrame() {
    stopTour();
    var f = $('#framewrap iframe');
    if (f) f.remove();
    state.frameKey = null;
    state.frameSrc = null;
    state.frameLive = false;
  }

  function openFrame(key, a, pre) {
    if (state.frameKey === key && $('#framewrap iframe')) return;
    closeFrame();
    var address = pre ? a.sample : a.url;
    if (!address) return;
    var src;
    try { src = withParams(address, { 'merlin-home': '1', look: SETTINGS.look }); } catch (e) { return; }
    var f = document.createElement('iframe');
    f.title = a.name;
    if (pre) {
      f.setAttribute('inert', '');
      f.tabIndex = -1;
    }
    f.src = src;
    $('#framewrap').appendChild(f);
    state.frameKey = key;
    state.frameSrc = src;
    state.frameLive = !pre;
    if (pre) startTour(src, a.sample_pages);
  }

  function tellFrame() {
    var f = $('#framewrap iframe');
    if (!f || !state.frameSrc) return;
    try {
      var origin = new URL(state.frameSrc).origin;
      f.contentWindow.postMessage({ type: 'merlin-home', v: 1, look: SETTINGS.look }, origin);
    } catch (e) { /* the frame is gone or on an address that takes no message */ }
  }

  function drawBanner(a, pre) {
    var bar = $('#prebar');
    bar.hidden = !pre;
    $('#framewrap').classList.toggle('pre', !!pre);
    if (!pre) return;
    var words = ADDON_WORDS[a.id] || {};
    var go = $('#preGo');
    if (a.state === 'bought-not-installed') {
      $('#preT').textContent = a.name + ' is part of your account but not installed on this computer.';
      $('#preS').textContent = 'Say: install my Praxis tools.';
      go.hidden = true;
    } else {
      $('#preT').textContent = a.name + ' is not activated for your account.';
      $('#preS').textContent = words.sample || '';
      var page = typeof a.product_page === 'string' && a.product_page.indexOf(SITE) === 0 ? a.product_page : null;
      go.hidden = !page;
      $('#preGo span').textContent = 'See ' + a.name;
      go.dataset.url = page || '';
    }
    $('#preS').hidden = !$('#preS').textContent;
  }

  /* ---------------------------------------------------------------- router */

  function showView(id) {
    $$('.view').forEach(function (s) { s.classList.toggle('on', s.id === 'v-' + id); });
  }

  function light(nav) {
    $$('[data-nav]').forEach(function (a) {
      if (a.dataset.nav === nav) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function setKind(k) {
    if (k === state.kind) return;
    state.kind = k;
    state.filt = 'all';
    setSeg('segFilter', 'all');
    $('#q').value = '';
  }

  function route() {
    if (!HOME) return;
    var raw = (location.hash || '#projects').slice(1);
    var parts = raw.split('/');
    var v = parts[0];
    if (v === 'jobs') v = 'construction';
    var a = addonById(v);
    var p = null;
    if (v === 'project') {
      var name = '';
      try { name = decodeURIComponent(parts.slice(1).join('/')); } catch (e) { name = ''; }
      p = findProject(name);
      if (!p) v = 'projects';
    }
    if (!a && ['projects', 'project', 'settings'].indexOf(v) < 0) v = 'projects';

    if (a) {
      var off = a.state !== 'activated';
      if (off || a.url) {
        showView('plugin');
        drawBanner(a, off);
        openFrame(a.id + '|' + (off ? 'sample' : 'page'), a, off);
      } else {
        closeFrame();
        setKind('con');
        showView('projects');
        drawProjects();
      }
      light(a.id);
    } else {
      closeFrame();
      if (v === 'projects') { setKind('reg'); showView('projects'); drawProjects(); }
      else if (v === 'project') { showView('project'); drawProject(p); }
      else showView(v);
      light(v === 'project' ? 'projects' : v);
    }
    window.scrollTo(0, 0);
  }

  /* ---------------------------------------------------------------- settings */

  function applyLook(look) {
    SETTINGS.look = look === 'light' ? 'light' : 'dark';
    H.setAttribute('data-mode', SETTINGS.look);
    setSeg('segMode', SETTINGS.look);
    var dark = SETTINGS.look === 'dark';
    $$('.modelabel').forEach(function (e) { e.textContent = dark ? 'Light mode' : 'Dark mode'; });
    $$('.modebtn .sun').forEach(function (s) { s.innerHTML = dark ? ICONS.sun : ICONS.moon; });
  }

  function applyMenu(menu) {
    SETTINGS.menu = menu === 'side' ? 'side' : 'top';
    H.setAttribute('data-layout', SETTINGS.menu);
    setSeg('segLayout', SETTINGS.menu);
  }

  function saveSettings() {
    act('settings', { menu: SETTINGS.menu, look: SETTINGS.look });
  }

  function setLook(look) {
    if (look === SETTINGS.look) return;
    applyLook(look);
    tellFrame();
    saveSettings();
  }

  function setMenu(menu) {
    if (menu === SETTINGS.menu) return;
    applyMenu(menu);
    saveSettings();
  }

  /* ---------------------------------------------------------------- start */

  function staticLinks() {
    $$('[data-hash]').forEach(function (a) { a.href = '#' + a.dataset.hash; });
    $$('[data-home]').forEach(function (a) { a.href = '#projects'; });
    $$('[data-guides]').forEach(function (a) { a.href = GUIDES; });
  }

  function failed() {
    showView('projects');
    $('#heroLine').textContent = 'Could not read your business folder.';
    $('#plead').textContent = 'Ask Claude to open your Merlin home again.';
    $('#ptools').hidden = true;
    $('#newproj').hidden = true;
  }

  function start(data) {
    DATA = data || {};
    HOME = DATA.home;
    if (!HOME || typeof HOME !== 'object') { failed(); return; }
    ACTIONS = DATA.actions && typeof DATA.actions === 'object' ? DATA.actions : {};
    PAGES = DATA.addon_pages && typeof DATA.addon_pages === 'object' ? DATA.addon_pages : {};
    var s = DATA.settings || {};
    applyLook(s.look || H.getAttribute('data-mode'));
    applyMenu(s.menu || H.getAttribute('data-layout'));

    var now = new Date();
    $('#dateLine').textContent = DAYS[now.getDay()] + ' · ' + MONTHS[now.getMonth()] + ' ' + now.getDate() + ', ' + now.getFullYear() + ' · ' + ((HOME.business && HOME.business.name) || '');
    $('#pfoot').textContent = 'Read from manifest.yaml, each project\'s CONTINUITY.md and TASKS.md · read ' + fmtTime(HOME.generated_at);
    drawMenu();
    drawSettings();
    route();
  }

  function wire() {
    staticLinks();
    applyLook(H.getAttribute('data-mode'));
    applyMenu(H.getAttribute('data-layout'));
    H.setAttribute('data-rail', window.innerWidth >= 1000 ? 'open' : 'closed');

    window.addEventListener('hashchange', route);
    $('#railToggle').addEventListener('click', function () {
      H.setAttribute('data-rail', H.getAttribute('data-rail') === 'open' ? 'closed' : 'open');
    });
    $$('.modebtn').forEach(function (b) {
      b.addEventListener('click', function () { setLook(SETTINGS.look === 'dark' ? 'light' : 'dark'); });
    });
    $('#segMode').addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) setLook(b.dataset.v); });
    $('#segLayout').addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) setMenu(b.dataset.v); });
    $('#segFilter').addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      state.filt = b.dataset.v;
      setSeg('segFilter', state.filt);
      drawProjects();
    });
    $('#q').addEventListener('input', function () { if (HOME) drawProjects(); });
    $('#pfolder').addEventListener('click', function () {
      var id = this.dataset.id;
      if (id) act('open_folder', { id: id });
    });
    $('#preGo').addEventListener('click', function () { openAddress(this.dataset.url); });

    document.addEventListener('click', function (e) {
      var g = e.target.closest('[data-guides]');
      if (g) { e.preventDefault(); openAddress(GUIDES); return; }
      var c = e.target.closest('[data-copy]');
      if (c) { e.preventDefault(); copyLine(c.dataset.copy); return; }
      var m = e.target.closest('[data-more]');
      if (m) {
        var card = m.closest('.card');
        card.classList.toggle('open');
        m.textContent = card.classList.contains('open') ? 'Show less' : 'Show all';
        return;
      }
      if (e.target.closest('a,button,input')) return;
      var go = e.target.closest('.card.click[data-go]');
      if (go) location.hash = go.dataset.go;
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var go = e.target.closest && e.target.closest('.card.click[data-go]');
      if (go && e.target === go) { e.preventDefault(); location.hash = go.dataset.go; }
    });

    fetch('data.json', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('data'); return r.json(); })
      .then(start, failed);
  }

  wire();
})();
