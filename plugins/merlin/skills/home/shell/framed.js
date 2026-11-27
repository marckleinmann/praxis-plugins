/* Merlin home, framed-page contract, version 1.
   A plugin page builds this script into itself, as it is. It does nothing at all unless the
   page's address carries merlin-home=1, which the Merlin home adds when it shows the page in
   a frame. Then it:
   - marks <html> with data-merlin-home="1", so the page's own CSS can hide its logo, its
     light and dark button and any "INTERNAL" band;
   - takes the look (light or dark) from the address (look=light|dark) and from the message
     {type:"merlin-home", v:1, look:"light"|"dark"}, accepted only from the parent window and
     only from an http://127.0.0.1 origin. The look is put on <html> as
     data-merlin-home-look and announced with a "merlin-home-look" event on window
     (event.detail.look). The page applies it over its own saved look and never saves it;
   - sets window.merlinHome = {framed: true, v: 1, look: <"light"|"dark"|null>}, so the page
     can change its own tabs with history.replaceState (or location.replace) and add no Back
     step. Without merlin-home=1, window.merlinHome is not set. */
(function () {
  var q;
  try { q = new URLSearchParams(window.location.search); } catch (e) { return; }
  if (q.get('merlin-home') !== '1') return;
  var root = document.documentElement;
  var state = { framed: true, v: 1, look: null };
  function setLook(look) {
    if (look !== 'light' && look !== 'dark') return;
    state.look = look;
    root.setAttribute('data-merlin-home-look', look);
    var ev;
    try { ev = new CustomEvent('merlin-home-look', { detail: { look: look } }); } catch (e) { return; }
    window.dispatchEvent(ev);
  }
  root.setAttribute('data-merlin-home', '1');
  window.merlinHome = state;
  setLook(q.get('look'));
  window.addEventListener('message', function (e) {
    if (window.parent === window || e.source !== window.parent) return;
    if (!/^http:\/\/127\.0\.0\.1(:\d{1,5})?$/.test(e.origin)) return;
    var d = e.data;
    if (!d || typeof d !== 'object' || d.type !== 'merlin-home' || d.v !== 1) return;
    setLook(d.look);
  });
})();
