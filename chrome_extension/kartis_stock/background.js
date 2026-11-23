// Click the toolbar button on any event page → read the show (and, on
// Ticketmaster, what hook.js captured) → open /stock#add=… on Kartis, which
// adds or fills in the show and writes today's tickets-left per section.

const DEFAULT_BASE = 'https://kartis.homes';

// Runs inside the event page (MAIN world). Must be self-contained.
function collectFromPage() {
  var ev = null;
  document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
    try {
      var q = [].concat(JSON.parse(s.textContent));
      while (q.length) {
        var x = q.shift();
        if (!x || typeof x !== 'object') continue;
        if (x['@graph']) q = q.concat(x['@graph']);
        var t = [].concat(x['@type'] || []).join(' ');
        if (!ev && (/Event\b/.test(t) || (x.startDate && x.location))) ev = x;
      }
    } catch (e) {}
  });
  function meta(p) { var m = document.querySelector('meta[property="' + p + '"],meta[name="' + p + '"]'); return m ? m.content : ''; }
  var loc = ev ? [].concat(ev.location || [])[0] || {} : {}, ad = loc.address || {};
  var img = ev && ev.image; img = [].concat(img || [])[0]; if (img && img.url) img = img.url;
  var out = {
    link: meta('og:url') || location.origin + location.pathname,
    name: (ev && ev.name) || (meta('og:title') || document.title).split(/ (?:Tickets|[-|]) /)[0],
    venue: loc.name || '', city: (typeof ad === 'object' && ad.addressLocality) || '',
    event_date_iso: ev && ev.startDate ? String(ev.startDate).slice(0, 10) : '',
    image_url: meta('og:image') || img || ''
  };

  // Ticketmaster: tickets left per section from the page's own availability data.
  var st = window.__kartisStock;
  function priceOf(ids) {
    var p = null;
    (ids || []).forEach(function (id) { var o = st.offers[id]; if (o && o.inventoryType !== 'resale' && o.totalPrice != null && (p === null || o.totalPrice < p)) p = o.totalPrice; });
    return p;
  }
  // Seated shows: the "facets" feed counts per section (and says primary vs
  // resale). Resale listings aren't the box office's stock, so only primary
  // counts are kept.
  if (st && !Object.keys(st.sections).length && st.facets && st.facets.length) {
    var bySec = {};
    st.facets.forEach(function (f) {
      var secs = [].concat(f.sections || f.section || []);
      if (!secs.length || typeof f.count !== 'number') return;
      var inv = [].concat(f.inventoryTypes || f.inventoryType || []);
      if (inv.length && inv.indexOf('primary') === -1) return;
      var key = String(secs[0]);
      var row = bySec[key] = bySec[key] || { name: key, remaining: 0, price: null };
      row.remaining += f.count;
      var p = priceOf(f.offers);
      if (p !== null && (row.price === null || p < row.price)) row.price = p;
    });
    out.sections = Object.keys(bySec).sort(function (a, b) { return a.localeCompare(b, undefined, { numeric: true }); }).map(function (k) { return bySec[k]; });
    out.captured_at = st.at;
  }
  if (st && Object.keys(st.sections).length) {
    var rows = [];
    Object.keys(st.sections).forEach(function (key) {
      var s = st.sections[key];
      var offers = (s.offers || []).map(function (id) { return st.offers[id]; }).filter(Boolean);
      var primary = offers.filter(function (o) { return o.inventoryType !== 'resale'; });
      var pool = primary.length ? primary : offers;
      var price = null;
      pool.forEach(function (o) { if (o.totalPrice != null && (price === null || o.totalPrice < price)) price = o.totalPrice; });
      // GA rows are named after the ticket ("General Admission - T4" → "General Admission")
      // so a new release tier keeps landing on the same row; seated rows keep the section.
      var name = key;
      if (s.seating === 'general' && pool.length) name = pool[0].name.replace(/\s*-\s*T(?:ier)?\s*\d+\s*$/i, '');
      rows.push({ name: name, remaining: typeof s.count === 'number' ? s.count : null, price: price,
                  resale_only: !primary.length && offers.length > 0 });
    });
    out.sections = rows;
    out.captured_at = st.at;
  }
  return out;
}

chrome.action.onClicked.addListener(async (tab) => {
  let data;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', func: collectFromPage });
    data = res && res.result;
  } catch (e) {
    data = { link: tab.url, name: tab.title };
  }
  const { base } = await chrome.storage.sync.get({ base: DEFAULT_BASE });
  const url = String(base || DEFAULT_BASE).replace(/\/+$/, '') + '/stock#add=' + encodeURIComponent(JSON.stringify(data || {}));
  chrome.tabs.create({ url, index: tab.index + 1 });
});
