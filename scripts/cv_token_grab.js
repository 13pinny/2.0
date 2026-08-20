// Paste this into the DevTools Console on https://www.crowdvolt.com while
// logged in (any page). It prints the two lines that go in .env.crowdvolt.
//
// Why this exists: Turnstile guards the CrowdVolt LOGIN, and an Xvfb Chrome
// with CDP attached rarely clears it. api.crowdvolt.com itself is not
// IP-gated, so the box only needs the session — minted here, where Turnstile
// passes, and carried over. See crowdvolt_sales.py.
(() => {
  const cookie = (document.cookie.split('; ')
      .find(c => c.startsWith('stytch_session=')) || '').split('=')[1];
  if (!cookie) {
    console.error('No stytch_session cookie on this page. Are you logged in ' +
                  'to crowdvolt.com in THIS tab? (Check the address bar is ' +
                  'www.crowdvolt.com, then reload and re-run.)');
    return;
  }
  const fp = localStorage.getItem('fingerprint_id') || '';
  const out = `KARTIS_CV_TOKEN=${cookie}\nKARTIS_CV_FINGERPRINT=${fp}`;
  console.log('%c--- copy everything between the lines ---', 'font-weight:bold');
  console.log(out);
  console.log('%c-----------------------------------------', 'font-weight:bold');
  if (!fp) console.warn('fingerprint_id was empty — that is fine, the sales ' +
                        'endpoints did not require it.');
  try {
    copy(out);               // DevTools console helper
    console.log('%cCopied to clipboard.', 'color:green;font-weight:bold');
  } catch (e) {
    console.log('(Select the two lines above and copy them manually.)');
  }
  return out;
})();
