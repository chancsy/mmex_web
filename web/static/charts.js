// Report charts, drawn as SVG from the JSON inside each <figure data-chart>
// (see chart_figure in _reports.html; the data comes from web/reports.py).
// Kinds: line (Net worth), bars (Income: net per month + 12-month average),
// hbar (By category: spending ranked), stack (Account changes: each type's
// gain above zero, loss below, and the net as a dot).
// Every value is also in the table under the chart; the hover/keyboard
// read-out never holds the only copy. Charts marked secret follow the eye
// in the top bar: hidden balances keep the shape but drop the amounts.
(function () {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var root = document.documentElement;
  var figures = Array.prototype.slice.call(document.querySelectorAll('figure[data-chart]'));
  if (!figures.length) return;

  // -- helpers ----------------------------------------------------------------
  function svg(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function html(tag, cls, parent, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }
  function hidden(data) { return data.secret && root.classList.contains('masked'); }
  var DOTS = '••••••';

  function full(v, m) {
    var d = m.decimals;
    var s = Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
    return (v < -Math.pow(10, -d) / 2 ? '-' : '') + m.prefix + s + m.suffix;
  }
  function whole(v, m) { return full(v, {prefix: m.prefix, suffix: m.suffix, decimals: 0}); }
  function compact(v, m) {
    var a = Math.abs(v), s;
    if (a >= 1e6) s = (a / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
    else if (a >= 1e4) s = Math.round(a / 1e3) + 'K';
    else s = Math.round(a).toLocaleString('en-US');
    return (v < 0 ? '-' : '') + m.prefix + s + m.suffix;
  }
  function signed(v, m) { return (v > 0.005 ? '+' : '') + full(v, m); }

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function isMonth(x) { return /^\d{4}-\d{2}$/.test(x); }
  function shortX(x) { return isMonth(x) ? MONTHS[+x.slice(5) - 1] + ' ' + x.slice(2, 4) : x; }
  function longX(x) { return isMonth(x) ? MONTHS[+x.slice(5) - 1] + ' ' + x.slice(0, 4) : x; }

  // Round tick values covering lo..hi, about `count` of them.
  function ticks(lo, hi, count) {
    if (hi - lo < 1e-9) { hi = lo + 1; }
    var raw = (hi - lo) / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
    var step = (r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10) * mag;
    var out = [], start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
    for (var v = start; v <= end + step / 2; v += step) out.push(Math.abs(v) < step / 1e6 ? 0 : v);
    return out;
  }

  // Which x positions get a label: at most `room` of them, evenly; for long
  // monthly ranges, Januaries labelled with the year.
  function xLabels(x, room) {
    var n = x.length, out = [];
    if (n > 24 && isMonth(x[0])) {
      var jans = [];
      x.forEach(function (v, i) { if (v.slice(5) === '01') jans.push(i); });
      var every = Math.max(1, Math.ceil(jans.length / room));
      jans.forEach(function (i, k) { if (k % every === 0) out.push([i, x[i].slice(0, 4)]); });
      return out;
    }
    var stepN = Math.max(1, Math.ceil(n / room));
    for (var i = n - 1; i >= 0; i -= stepN) out.unshift([i, shortX(x[i])]);   // always label the latest
    return out;
  }

  // A bar from the baseline y0 to y1: 4px rounded at the data end, square at the baseline.
  function barPath(x, w, y0, y1, round) {
    var up = y1 < y0, h = Math.abs(y1 - y0), r = round ? Math.min(4, h, w / 2) : 0;
    if (h < 0.5) return '';
    if (up) {
      return 'M' + x + ',' + y0 + 'V' + (y1 + r) + 'Q' + x + ',' + y1 + ' ' + (x + r) + ',' + y1 +
             'H' + (x + w - r) + 'Q' + (x + w) + ',' + y1 + ' ' + (x + w) + ',' + (y1 + r) + 'V' + y0 + 'Z';
    }
    return 'M' + x + ',' + y0 + 'V' + (y1 - r) + 'Q' + x + ',' + y1 + ' ' + (x + r) + ',' + y1 +
           'H' + (x + w - r) + 'Q' + (x + w) + ',' + y1 + ' ' + (x + w) + ',' + (y1 - r) + 'V' + y0 + 'Z';
  }

  // -- the frame shared by line, bars and stack ------------------------------
  // Builds the svg with a y axis (gridlines + labels) for lo..hi, and returns
  // the geometry the kinds draw with.
  function frame(fig, data, lo, hi, opts) {
    var width = Math.max(260, fig.clientWidth - 2 * 16);
    var height = width < 520 ? 210 : 260;
    var m = data.money, mask = hidden(data);
    var tv = ticks(Math.min(lo, hi), Math.max(lo, hi), height < 230 ? 4 : 5);
    var big = Math.max(Math.abs(tv[0]), Math.abs(tv[tv.length - 1])) >= 1e4;
    var label = function (v) { return mask ? '' : (big ? compact(v, m) : whole(v, m)); };
    var left = mask ? 12 : Math.max.apply(null, tv.map(function (v) { return label(v).length; })) * 6.6 + 12;
    var g = {width: width, height: height, top: 10, bottom: height - 24, left: left, right: width - (opts.right || 8)};
    g.lo = tv[0]; g.hi = tv[tv.length - 1];
    g.y = function (v) { return g.bottom - (v - g.lo) / (g.hi - g.lo) * (g.bottom - g.top); };
    var s = svg('svg', {width: width, height: height, viewBox: '0 0 ' + width + ' ' + height, role: 'img',
                        'aria-label': fig.getAttribute('aria-label') || ''});
    tv.forEach(function (v) {
      var y = Math.round(g.y(v)) + 0.5;
      svg('line', {x1: g.left, x2: g.right, y1: y, y2: y, 'class': v === 0 ? 'base' : 'grid'}, s);
      var t = svg('text', {x: g.left - 6, y: y + 4, 'class': 'tick', 'text-anchor': 'end'}, s);
      t.textContent = label(v);
    });
    g.svg = s;
    return g;
  }

  function xAxis(g, x, pos) {
    var room = Math.max(2, Math.floor((g.right - g.left) / 64));
    xLabels(x, room).forEach(function (p) {
      var t = svg('text', {x: pos(p[0]), y: g.height - 6, 'class': 'tick', 'text-anchor': 'middle'}, g.svg);
      t.textContent = p[1];
    });
  }

  // -- read-out (tooltip) -----------------------------------------------------
  function tip(fig) {
    var t = fig.querySelector('.chart-tip');
    if (!t) { t = html('div', 'chart-tip', fig); t.setAttribute('role', 'status'); t.hidden = true; }
    return t;
  }
  function showTip(fig, title, rows, px, py) {
    var t = tip(fig);
    t.textContent = '';
    html('div', 'tip-title', t, title);
    rows.forEach(function (r) {
      var row = html('div', 'tip-row', t);
      html('span', 'key ' + r.key, row);
      html('strong', null, row, r.value);
      html('span', 'muted', row, r.name);
    });
    t.hidden = false;
    var fw = fig.clientWidth, tw = t.offsetWidth;
    var left = px + 14 + tw > fw ? px - 14 - tw : px + 14;
    t.style.left = Math.max(4, left) + 'px';
    t.style.top = Math.max(4, py) + 'px';
  }
  function hideTip(fig) { var t = fig.querySelector('.chart-tip'); if (t) t.hidden = true; }

  // Pointer and arrow keys pick an x index; `pick(i)` draws the hover state.
  function track(fig, g, n, indexAt, pick) {
    var hit = svg('rect', {x: g.left, y: g.top, width: g.right - g.left, height: g.bottom - g.top, 'class': 'hit'}, g.svg);
    function at(ev) {
      var r = g.svg.getBoundingClientRect();
      return indexAt(ev.clientX - r.left);
    }
    hit.addEventListener('pointermove', function (ev) { pick(at(ev)); });
    hit.addEventListener('pointerdown', function (ev) { pick(at(ev)); });
    hit.addEventListener('pointerleave', function (ev) { if (ev.pointerType === 'mouse') pick(null); });
    fig.onkeydown = function (ev) {
      var cur = fig.dataset.idx === undefined ? n : +fig.dataset.idx;
      if (ev.key === 'ArrowLeft') cur = Math.max(0, cur - 1);
      else if (ev.key === 'ArrowRight') cur = Math.min(n - 1, cur === n ? n - 1 : cur + 1);
      else if (ev.key === 'Escape') { pick(null); return; }
      else return;
      ev.preventDefault();
      pick(cur);
    };
    fig.onblur = function () { pick(null); };
  }

  // -- legend -----------------------------------------------------------------
  // Toggle buttons for series that can be hidden; plain keys otherwise.
  function legend(fig, items, onToggle) {
    var box = html('div', 'chart-legend', null);
    items.forEach(function (it) {
      var el;
      if (onToggle) {
        el = html('button', 'legend-item' + (it.off ? ' off' : ''), box);
        el.type = 'button';
        el.setAttribute('aria-pressed', it.off ? 'false' : 'true');
        el.addEventListener('click', function () { onToggle(it.name); });
      } else {
        el = html('span', 'legend-item', box);
      }
      html('span', 'key ' + it.key, el);
      el.appendChild(document.createTextNode(it.name));
    });
    return box;
  }

  // -- kinds ------------------------------------------------------------------
  function drawLine(fig, data, state) {
    var on = data.series.filter(function (s) { return !state.off[s.name]; });
    var vals = [];
    on.forEach(function (s) { vals = vals.concat(s.values); });
    if (!vals.length) vals = [0];
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var pad = (hi - lo) * 0.05 || Math.abs(hi) * 0.05 || 1;
    var wide = fig.clientWidth >= 560 && on.length <= 4;
    var names = on.map(function (s) { return s.name.length; });
    // Pad the range a little, but never below zero when nothing is negative.
    var g = frame(fig, data, lo >= 0 ? Math.max(0, lo - pad) : lo - pad, hi + pad, {right: wide ? Math.max.apply(null, names.concat([4])) * 6.8 + 16 : 8});
    var n = data.x.length;
    var x = function (i) { return n === 1 ? (g.left + g.right) / 2 : g.left + i * (g.right - g.left) / (n - 1); };
    xAxis(g, data.x, x);
    var cross = svg('line', {y1: g.top, y2: g.bottom, 'class': 'cross', visibility: 'hidden'}, g.svg);
    on.forEach(function (s) {
      var d = s.values.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + g.y(v).toFixed(1); }).join('');
      svg('path', {d: d, 'class': 'ln k' + s.slot}, g.svg);
    });
    // Direct labels at the line ends, only when none collide (else the legend does it).
    if (wide && n > 1) {
      var ends = on.map(function (s) { return {s: s, y: g.y(s.values[n - 1])}; }).sort(function (a, b) { return a.y - b.y; });
      var clear = ends.every(function (e, i) { return !i || e.y - ends[i - 1].y >= 13; });
      if (clear) ends.forEach(function (e) {
        var t = svg('text', {x: g.right + 8, y: e.y + 4, 'class': 'endlabel'}, g.svg);
        t.textContent = e.s.name;
      });
    }
    var dots = on.map(function (s) { return svg('circle', {r: 4, 'class': 'dot k' + s.slot, visibility: 'hidden'}, g.svg); });
    track(fig, g, n, function (px) {
      return Math.max(0, Math.min(n - 1, Math.round((px - g.left) / ((g.right - g.left) / Math.max(1, n - 1)))));
    }, function (i) {
      if (i === null) { delete fig.dataset.idx; cross.setAttribute('visibility', 'hidden'); dots.forEach(function (d) { d.setAttribute('visibility', 'hidden'); }); hideTip(fig); return; }
      fig.dataset.idx = i;
      cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
      on.forEach(function (s, k) {
        dots[k].setAttribute('cx', x(i)); dots[k].setAttribute('cy', g.y(s.values[i])); dots[k].setAttribute('visibility', 'visible');
      });
      var rows = on.slice().sort(function (a, b) { return b.values[i] - a.values[i]; }).map(function (s) {
        return {key: 'line k' + s.slot, name: s.name, value: hidden(data) ? DOTS : full(s.values[i], data.money)};
      });
      showTip(fig, longX(data.x[i]), rows, x(i), g.top);
    });
    return {svg: g.svg, legend: legend(fig, data.series.map(function (s) {
      return {name: s.name, key: 'line k' + s.slot, off: !!state.off[s.name]};
    }), function (name) { state.off[name] = !state.off[name]; render(fig); })};
  }

  function band(g, n) {
    var bw = (g.right - g.left) / n;
    return {w: bw, bar: Math.max(3, Math.min(24, bw * 0.62)), mid: function (i) { return g.left + bw * (i + 0.5); },
            index: function (px) { return Math.max(0, Math.min(n - 1, Math.floor((px - g.left) / bw))); }};
  }

  function drawBars(fig, data) {
    var v = data.bars.values, avg = data.line.values, n = data.x.length;
    var all = v.concat(avg, [0]);
    var g = frame(fig, data, Math.min.apply(null, all), Math.max.apply(null, all), {});
    var b = band(g, n);
    xAxis(g, data.x, b.mid);
    var hl = svg('rect', {y: g.top, height: g.bottom - g.top, width: b.w, 'class': 'bandhl', visibility: 'hidden'}, g.svg);
    v.forEach(function (val, i) {
      var d = barPath(b.mid(i) - b.bar / 2, b.bar, g.y(0), g.y(val), true);
      if (d) svg('path', {d: d, 'class': 'bar ' + (val < 0 ? 'neg' : 'pos')}, g.svg);
    });
    var y0 = Math.round(g.y(0)) + 0.5;
    svg('line', {x1: g.left, x2: g.right, y1: y0, y2: y0, 'class': 'base'}, g.svg);
    if (n > 1) {
      svg('path', {d: avg.map(function (a, i) { return (i ? 'L' : 'M') + b.mid(i).toFixed(1) + ',' + g.y(a).toFixed(1); }).join(''),
                   'class': 'ln avg'}, g.svg);
    }
    var m = data.money;
    track(fig, g, n, b.index, function (i) {
      if (i === null) { delete fig.dataset.idx; hl.setAttribute('visibility', 'hidden'); hideTip(fig); return; }
      fig.dataset.idx = i;
      hl.setAttribute('x', g.left + b.w * i); hl.setAttribute('visibility', 'visible');
      var rows = [{key: 'box ' + (v[i] < 0 ? 'neg' : 'pos'), name: data.bars.name, value: signed(v[i], m)},
                  {key: 'line avg', name: data.line.name, value: signed(avg[i], m)}];
      (data.extra || []).forEach(function (e) { rows.push({key: 'none', name: e.name, value: full(e.values[i], m)}); });
      showTip(fig, longX(data.x[i]), rows, b.mid(i), g.top);
    });
    return {svg: g.svg, legend: legend(fig, [{name: data.bars.name + ' (saved / overspent)', key: 'box split'},
                                             {name: data.line.name, key: 'line avg'}])};
  }

  function drawStack(fig, data, state) {
    var on = data.series.filter(function (s) { return !state.off[s.name]; });
    var n = data.x.length, ups = [], downs = [];
    for (var i = 0; i < n; i++) {
      var u = 0, dn = 0;
      on.forEach(function (s) { if (s.values[i] > 0) u += s.values[i]; else dn += s.values[i]; });
      ups.push(u); downs.push(dn);
    }
    var all = ups.concat(downs, data.net, [0]);
    var g = frame(fig, data, Math.min.apply(null, all), Math.max.apply(null, all), {});
    var b = band(g, n);
    xAxis(g, data.x, b.mid);
    var hl = svg('rect', {y: g.top, height: g.bottom - g.top, width: b.w, 'class': 'bandhl', visibility: 'hidden'}, g.svg);
    for (i = 0; i < n; i++) {
      var x0 = b.mid(i) - b.bar / 2, up = 0, down = 0;
      var pos = on.filter(function (s) { return s.values[i] > 0.005; });
      var neg = on.filter(function (s) { return s.values[i] < -0.005; });
      pos.forEach(function (s, k) {
        var a = g.y(up), z = g.y(up + s.values[i]);
        // 2px surface gap between touching segments: trim 1px off each shared edge.
        var d = barPath(x0, b.bar, a - (k ? 1 : 0), z + (k < pos.length - 1 ? 1 : 0), k === pos.length - 1);
        if (d) svg('path', {d: d, 'class': 'bar k' + s.slot}, g.svg);
        up += s.values[i];
      });
      neg.forEach(function (s, k) {
        var a = g.y(down), z = g.y(down + s.values[i]);
        var d = barPath(x0, b.bar, a + (k ? 1 : 0), z - (k < neg.length - 1 ? 1 : 0), k === neg.length - 1);
        if (d) svg('path', {d: d, 'class': 'bar k' + s.slot}, g.svg);
        down += s.values[i];
      });
    }
    var y0 = Math.round(g.y(0)) + 0.5;
    svg('line', {x1: g.left, x2: g.right, y1: y0, y2: y0, 'class': 'base'}, g.svg);
    data.net.forEach(function (v, k) { svg('circle', {cx: b.mid(k), cy: g.y(v), r: 4, 'class': 'netdot'}, g.svg); });
    track(fig, g, n, b.index, function (k) {
      if (k === null) { delete fig.dataset.idx; hl.setAttribute('visibility', 'hidden'); hideTip(fig); return; }
      fig.dataset.idx = k;
      hl.setAttribute('x', g.left + b.w * k); hl.setAttribute('visibility', 'visible');
      var mask = hidden(data), m = data.money;
      var rows = [{key: 'dot', name: data.net_name, value: mask ? DOTS : signed(data.net[k], m)}];
      on.filter(function (s) { return Math.abs(s.values[k]) > 0.005; })
        .sort(function (a, c) { return c.values[k] - a.values[k]; })
        .forEach(function (s) { rows.push({key: 'box k' + s.slot, name: s.name, value: mask ? DOTS : signed(s.values[k], m)}); });
      showTip(fig, longX(data.x[k]), rows, b.mid(k), g.top);
    });
    var items = data.series.map(function (s) { return {name: s.name, key: 'box k' + s.slot, off: !!state.off[s.name]}; });
    var box = legend(fig, items, function (name) { state.off[name] = !state.off[name]; render(fig); });
    var net = html('span', 'legend-item', box);
    html('span', 'key dot', net);
    net.appendChild(document.createTextNode(data.net_name));
    return {svg: g.svg, legend: box};
  }

  function drawHbar(fig, data) {
    var items = data.items, m = data.money;
    var width = Math.max(260, fig.clientWidth - 2 * 16), rowH = 26, bar = 14;
    var labelW = Math.min(150, Math.round(width * 0.36)), valueW = 84;
    var height = items.length * rowH + 4;
    var s = svg('svg', {width: width, height: height, viewBox: '0 0 ' + width + ' ' + height, role: 'img',
                        'aria-label': fig.getAttribute('aria-label') || ''});
    var max = Math.max.apply(null, items.map(function (it) { return it.value; }).concat([1]));
    var total = items.reduce(function (a, it) { return a + it.value; }, 0);
    var x0 = labelW + 8, span = width - x0 - valueW;
    var chars = Math.floor(labelW / 6.8);
    items.forEach(function (it, i) {
      var y = i * rowH + 2, w = Math.max(2, it.value / max * span);
      var name = it.name.length > chars ? it.name.slice(0, chars - 1) + '…' : it.name;
      var t = svg('text', {x: labelW, y: y + rowH / 2 + 4, 'class': 'tick name', 'text-anchor': 'end'}, s);
      t.textContent = name;
      // A horizontal bar: square at the baseline (left), 4px round at the data end.
      var r = Math.min(4, w / 2), yb = y + (rowH - bar) / 2;
      svg('path', {d: 'M' + x0 + ',' + yb + 'H' + (x0 + w - r) + 'Q' + (x0 + w) + ',' + yb + ' ' + (x0 + w) + ',' + (yb + r) +
                      'V' + (yb + bar - r) + 'Q' + (x0 + w) + ',' + (yb + bar) + ' ' + (x0 + w - r) + ',' + (yb + bar) + 'H' + x0 + 'Z',
                   'class': 'bar k1'}, s);
      var v = svg('text', {x: x0 + w + 6, y: y + rowH / 2 + 4, 'class': 'tick value'}, s);
      v.textContent = whole(it.value, m);
      var hit = svg('rect', {x: 0, y: y, width: width, height: rowH, 'class': 'hit'}, s);
      hit.addEventListener('pointermove', function () {
        showTip(fig, it.name, [{key: 'box k1', name: Math.round(it.value / total * 100) + '% of spending', value: full(it.value, m)}],
                x0 + w, y + rowH);
      });
      hit.addEventListener('pointerleave', function () { hideTip(fig); });
    });
    svg('line', {x1: x0 + 0.5, x2: x0 + 0.5, y1: 0, y2: height, 'class': 'base'}, s);
    return {svg: s, legend: null};
  }

  // -- render / re-render -------------------------------------------------------
  var KINDS = {line: drawLine, bars: drawBars, stack: drawStack, hbar: drawHbar};

  function render(fig) {
    var data = fig._data, out;
    if (!data || !KINDS[data.kind]) return;
    fig._state = fig._state || {off: {}};
    Array.prototype.slice.call(fig.querySelectorAll('svg, .chart-legend, .chart-tip')).forEach(function (e) { e.remove(); });
    delete fig.dataset.idx;
    out = KINDS[data.kind](fig, data, fig._state);
    if (out.legend) fig.appendChild(out.legend);
    fig.appendChild(out.svg);
  }

  figures.forEach(function (fig) {
    var src = fig.querySelector('script.chart-data');
    try { fig._data = JSON.parse(src.textContent); } catch (e) { return; }
    render(fig);
  });

  // Redraw on resize and when the eye hides or shows balances.
  var last = {};
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(function (entries) {
      entries.forEach(function (en) {
        var fig = en.target, w = Math.round(en.contentRect.width);
        if (last[fig._id] !== w) { last[fig._id] = w; render(fig); }
      });
    });
    figures.forEach(function (fig, i) { fig._id = i; last[i] = Math.round(fig.getBoundingClientRect().width); ro.observe(fig); });
  }
  new MutationObserver(function () { figures.forEach(render); }).observe(root, {attributes: true, attributeFilter: ['class']});
})();
