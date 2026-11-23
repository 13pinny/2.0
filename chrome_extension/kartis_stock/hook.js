// Runs in the Ticketmaster page itself, before the page's own scripts.
// It only LISTENS: every availability response the page fetches for its
// seat map (services.ticketmaster.com/api/ismds/event/<id>/quickpicks and
// /facets) is copied into window.__kartisStock so a click on the extension
// can read it. It sends nothing anywhere and makes no requests of its own.
(function () {
  if (window.__kartisStock) return;
  const store = window.__kartisStock = { sections: {}, offers: {}, facets: [], at: null };
  const WANT = /\/api\/ismds\/event\/[^/]+\/(quickpicks|facets)/;

  function take(url, text) {
    if (!WANT.test(url || '') || !text) return;
    let j;
    try { j = JSON.parse(text); } catch (e) { return; }
    store.at = Date.now();
    const offers = (j._embedded && j._embedded.offer) || [];
    offers.forEach(function (o) {
      if (o && o.offerId) store.offers[o.offerId] = {
        name: o.name, totalPrice: o.totalPrice, listPrice: o.listPrice,
        inventoryType: o.inventoryType, priceLevelId: o.priceLevelId
      };
    });
    (j.sections || []).forEach(function (s) {
      if (!s || !s.section) return;
      store.sections[s.section] = {
        section: s.section, count: s.count, available: s.available, seating: s.seating,
        offers: s.offers || [], inventoryTypes: s.inventoryTypes || []
      };
    });
    if (Array.isArray(j.facets)) store.facets = j.facets;
  }

  const oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__kartisUrl = String(u); return oOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    const x = this;
    if (WANT.test(x.__kartisUrl || '')) x.addEventListener('load', function () {
      try { take(x.__kartisUrl, x.responseText); } catch (e) {}
    });
    return oSend.apply(this, arguments);
  };
  const oFetch = window.fetch;
  if (oFetch) window.fetch = function (input) {
    const p = oFetch.apply(this, arguments);
    try {
      const url = typeof input === 'string' ? input : (input && input.url);
      if (WANT.test(url || '')) p.then(function (r) { r.clone().text().then(function (t) { take(url, t); }); }).catch(function () {});
    } catch (e) {}
    return p;
  };
})();
