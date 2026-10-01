// Pelican game-server monitor.  Runs sandboxed (opaque origin): no network,
// no storage, no confirm() — everything goes through the `ph` bridge.  Text
// from the panel (server and node names) is only ever set with textContent.
(function () {
  'use strict';

  var POLL_MS = 5000, FAST_MS = 1500, ARM_MS = 3000;
  var busy = false, timer = 0, data = null;
  var cards = {};          // uuid -> card parts
  var armed = {};          // "uuid:signal" | "bulk:action" -> expiry (ms)
  var isAdmin = ph.user.role === 'admin';

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ── formatting ────────────────────────────────────────────────────────
  var GIB = 1024 * 1024 * 1024, MIB = 1024 * 1024;
  function gib(bytes) {
    if (bytes < GIB) return Math.round(bytes / MIB) + ' MiB';
    var g = bytes / GIB;
    return (g < 10 ? g.toFixed(1) : Math.round(g)) + ' GiB';
  }
  function used(bytes, limit) {
    if (bytes == null) return '–';
    return gib(bytes) + ' / ' + (limit > 0 ? gib(limit) : '∞');
  }
  function rate(bps) {
    if (bps == null) return '–';
    if (bps < 1024) return Math.round(bps) + ' B/s';
    if (bps < MIB) return (bps / 1024).toFixed(bps < 10240 ? 1 : 0) + ' KB/s';
    return (bps / MIB).toFixed(1) + ' MB/s';
  }
  function uptime(s) {
    if (!s || s <= 0) return '';
    if (s < 60) return s + 's';
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + m + 'm';
    return m + 'm';
  }
  var PENDING = { start: 'starting…', stop: 'stopping…', restart: 'restarting…', kill: 'killing…' };

  // ── sparkline ─────────────────────────────────────────────────────────
  function spark(canvas, pts, max, color) {
    var dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 160, h = 30;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    var g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    var vals = pts.filter(function (v) { return v != null; });
    max = Math.max(max || 0, Math.max.apply(null, vals.concat([1])));
    var step = w / Math.max(pts.length - 1, 1);
    function y(v) { return h - 2 - (v / max) * (h - 4); }
    // baseline
    g.globalAlpha = 0.25; g.fillStyle = ph.theme.vars['--border'] || '#ccc';
    g.fillRect(0, h - 1, w, 1); g.globalAlpha = 1;
    // runs of consecutive points: filled area + line
    var runs = [], cur = null;
    pts.forEach(function (v, i) {
      if (v == null) { cur = null; return; }
      if (!cur) { cur = []; runs.push(cur); }
      cur.push([i * step, y(v)]);
    });
    runs.forEach(function (run) {
      if (run.length === 1) { g.fillStyle = color; g.fillRect(run[0][0] - 1, run[0][1] - 1, 2, 2); return; }
      g.beginPath(); g.moveTo(run[0][0], h - 1);
      run.forEach(function (p) { g.lineTo(p[0], p[1]); });
      g.lineTo(run[run.length - 1][0], h - 1); g.closePath();
      g.globalAlpha = 0.15; g.fillStyle = color; g.fill(); g.globalAlpha = 1;
      g.beginPath(); g.moveTo(run[0][0], run[0][1]);
      run.forEach(function (p) { g.lineTo(p[0], p[1]); });
      g.lineWidth = 1.5; g.strokeStyle = color; g.stroke();
    });
  }

  // ── actions ───────────────────────────────────────────────────────────
  function needsConfirm(sig) { return sig === 'stop' || sig === 'restart' || sig === 'kill' ||
                                      sig === 'stop-running' || sig === 'restart-running'; }

  // First click arms a dangerous button, the second (within 3 s) fires it.
  function guarded(key, sig, fire) {
    var now = Date.now();
    if (needsConfirm(sig) && !(armed[key] > now)) {
      armed[key] = now + ARM_MS;
      setTimeout(function () { if (armed[key] && armed[key] <= Date.now()) { delete armed[key]; redraw(); } }, ARM_MS + 50);
      redraw();
      return;
    }
    delete armed[key];
    fire();
  }

  function after(p) {
    p.then(function () { schedule(FAST_MS); })
     .catch(function (e) { ph.toast(e && e.message ? e.message : 'Request failed', 'err'); schedule(FAST_MS); });
  }

  function power(uuid, sig) {
    guarded(uuid + ':' + sig, sig, function () {
      after(ph.call('power', { method: 'POST', body: { signal: sig, servers: [uuid] } }));
    });
  }
  function bulk(action) {
    guarded('bulk:' + action, action, function () {
      after(ph.call('bulk/' + action, { method: 'POST' }));
    });
  }

  function button(label, key, sig, cls, onClick, disabled) {
    var on = armed[key] > Date.now();
    var b = el('button', 'btn ' + (cls || '') + (on ? ' armed' : ''), on ? label + '? Click again' : label);
    b.type = 'button';
    b.disabled = !!disabled;
    b.addEventListener('click', onClick);
    return b;
  }

  // Which buttons a server gets in each state.
  var BUTTONS = {
    offline: ['start'],
    running: ['stop', 'restart', 'kill'],
    starting: ['stop', 'kill'],
    stopping: ['kill'],
    unavailable: ['start', 'stop', 'kill']
  };
  var LABEL = { start: 'Start', stop: 'Stop', restart: 'Restart', kill: 'Kill' };

  // ── rendering ─────────────────────────────────────────────────────────
  var root = document.body;
  var top = root.appendChild(el('div', 'top'));
  var sum = top.appendChild(el('div', 'sum'));
  var bulkBox = top.appendChild(el('div', 'bulk'));
  var notice = root.appendChild(el('p', 'line'));
  var task = root.appendChild(el('p', 'line'));
  var grid = root.appendChild(el('div', 'grid'));
  var empty = root.appendChild(el('div', 'empty'));

  function stat(value, label) {
    var s = el('div', 'stat');
    s.appendChild(el('b', null, value));
    s.appendChild(el('span', null, label));
    return s;
  }

  function makeCard(uuid) {
    var c = { root: el('div', 'card') };
    var head = c.root.appendChild(el('div', 'head'));
    head.appendChild(el('span', 'dot'));
    c.name = head.appendChild(el('span', 'name'));
    c.state = head.appendChild(el('span', 'state'));
    c.meta = c.root.appendChild(el('div', 'meta'));
    function metric(label) {
      var m = c.root.appendChild(el('div', 'metric'));
      m.appendChild(el('span', 'lbl', label));
      var vis = m.appendChild(label === 'Disk' ? el('div', 'bar') : el('canvas'));
      var val = m.appendChild(el('span', 'val'));
      return { vis: vis, val: val };
    }
    c.cpu = metric('CPU');
    c.ram = metric('RAM');
    c.disk = metric('Disk');
    c.disk.fill = c.disk.vis.appendChild(el('i'));
    c.net = c.root.appendChild(el('div', 'net'));
    c.btns = c.root.appendChild(el('div', 'btns'));
    cards[uuid] = c;
    return c;
  }

  function drawCard(c, s) {
    var inert = s.state === 'installing' || s.state === 'suspended';
    c.root.className = 'card s-' + s.state + (s.pending ? ' pending' : '') + (inert ? ' inert' : '');
    c.name.textContent = s.name;
    c.name.title = s.name;
    var st = s.pending ? PENDING[s.pending] || s.state : s.state;
    var up = s.state === 'running' ? uptime(s.uptime) : '';
    c.state.textContent = up ? st + ' · ' + up : st;

    c.meta.textContent = '';
    if (s.node) c.meta.appendChild(document.createTextNode(s.node + (s.ip ? ' · ' : '')));
    if (s.ip) c.meta.appendChild(el('span', 'ip', s.ip));
    c.meta.title = c.meta.textContent;

    var vars = ph.theme.vars, accent = vars['--accent'] || '#b45309', ok = vars['--ok'] || '#15803d';
    var cpuMax = s.cpu_limit > 0 ? s.cpu_limit : 100;
    spark(c.cpu.vis, s.cpu_pts || [], cpuMax, accent);
    // An offline server reports zeros; show them as "no value".
    var live = s.state === 'running' || s.state === 'starting' || s.state === 'stopping';
    var limit = s.cpu_limit > 0 ? ' / ' + s.cpu_limit + '%' : '';
    c.cpu.val.textContent = live && s.cpu != null ? Math.round(s.cpu) + '%' + limit : '–' + limit;
    spark(c.ram.vis, s.mem_pts || [], s.mem_limit > 0 ? s.mem_limit / MIB : 0, ok);
    c.ram.val.textContent = live ? used(s.mem, s.mem_limit) : '– / ' + (s.mem_limit > 0 ? gib(s.mem_limit) : '∞');
    var pct = s.disk != null && s.disk_limit > 0 ? Math.min(100, s.disk / s.disk_limit * 100) : 0;
    c.disk.fill.style.width = pct.toFixed(1) + '%';
    c.disk.vis.className = 'bar' + (pct >= 95 ? ' bad' : pct >= 80 ? ' warn' : '');
    c.disk.val.textContent = used(s.disk, s.disk_limit);
    c.net.textContent = s.state === 'running' ? '↓ ' + rate(s.rx) + '   ↑ ' + rate(s.tx) : '';

    c.btns.textContent = '';
    if (!isAdmin || inert) return;
    (BUTTONS[s.state] || []).forEach(function (sig) {
      var key = s.uuid + ':' + sig;
      // While a signal is pending only Kill stays available.
      var off = data.busy || (!!s.pending && sig !== 'kill');
      c.btns.appendChild(button(LABEL[sig], key, sig, sig === 'start' ? 'primary' : sig === 'kill' ? 'danger' : '',
        function () { power(s.uuid, sig); }, off));
    });
  }

  function showEmpty(title, text) {
    empty.textContent = '';
    if (!title) { empty.style.display = 'none'; return; }
    empty.style.display = '';
    empty.appendChild(el('b', null, title));
    empty.appendChild(document.createTextNode(text || ''));
  }

  function redraw() {
    if (!data) return;
    var d = data, sm = d.summary;
    sum.textContent = '';
    sum.appendChild(stat(sm.running + ' / ' + sm.total, 'running'));
    sum.appendChild(stat(Math.round(sm.cpu) + '%', 'CPU total'));
    sum.appendChild(stat(gib(sm.mem || 0), 'RAM total'));

    bulkBox.textContent = '';
    if (isAdmin && d.servers.length) {
      var anyOff = d.servers.some(function (s) { return s.state === 'offline'; });
      var anyOn = d.servers.some(function (s) { return s.state === 'running'; });
      bulkBox.appendChild(button('Start stopped', 'bulk:start-stopped', 'start-stopped', 'primary',
        function () { bulk('start-stopped'); }, d.busy || !anyOff));
      bulkBox.appendChild(button('Stop running', 'bulk:stop-running', 'stop-running', '',
        function () { bulk('stop-running'); }, d.busy || !anyOn));
      bulkBox.appendChild(button('Restart running', 'bulk:restart-running', 'restart-running', '',
        function () { bulk('restart-running'); }, d.busy || !anyOn));
    }
    if (isAdmin) {
      var r = button('Refresh', 'refresh', 'refresh', '', function () { after(ph.call('refresh', { method: 'POST' })); }, false);
      bulkBox.appendChild(r);
    }

    var line = '';
    if (d.notice) line = d.notice + (d.stale && d.age_seconds >= 0 ? ' — showing data from ' + d.age_seconds + ' s ago' : '');
    notice.textContent = line;
    notice.className = 'line' + (d.notice ? (d.age_seconds >= 0 ? ' warn' : ' bad') : '');
    task.textContent = d.task && d.task.status === 'running' && d.task.message ? d.task.message : '';

    var seen = {};
    d.servers.forEach(function (s, i) {
      seen[s.uuid] = true;
      var c = cards[s.uuid] || makeCard(s.uuid);
      if (grid.children[i] !== c.root) grid.insertBefore(c.root, grid.children[i] || null);
      drawCard(c, s);
    });
    Object.keys(cards).forEach(function (u) {
      if (!seen[u]) { cards[u].root.remove(); delete cards[u]; }
    });

    if (!d.configured) showEmpty('Not set up yet. ', 'Settings → Plugins → pelican-panel → Configure: panel URL and a client API key.');
    else if (!d.servers.length && d.age_seconds >= 0) showEmpty('No servers. ', 'The API key sees no game servers.');
    else if (!d.servers.length) showEmpty('Waiting for the first poll… ', '');
    else showEmpty('');
  }

  function poll() {
    if (busy || !ph.visible) return;
    busy = true;
    ph.call('monitor')
      .then(function (d) { data = d; redraw(); })
      .catch(function (e) {
        if (e && e.status === 403) {
          grid.textContent = ''; cards = {}; sum.textContent = ''; bulkBox.textContent = '';
          showEmpty('Admins only. ', 'Game servers are shown to Pi Hub admins.');
        }
      })
      .then(function () { busy = false; });
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(function () { poll(); schedule(POLL_MS); }, ms);
  }

  ph.on('visible', function (v) { if (v) schedule(0); });
  ph.on('refresh', function () { schedule(0); });
  ph.on('theme', redraw);
  window.addEventListener('resize', redraw);
  schedule(0);
})();
