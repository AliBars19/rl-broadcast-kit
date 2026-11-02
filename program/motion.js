// Deterministic timeline for the stinger and intro graphics.
// Pages register paused Web Animations with tl.add(); the renderer opens the page as
// page.html?t=<seconds> and every animation is scrubbed to that time, so a frame depends
// only on t. Motion used: fade, slide, scale, translate, wipe (clip-path) and stroke draw.
(function () {
  var EASE = {
    out: 'cubic-bezier(.16,1,.3,1)',
    inOut: 'cubic-bezier(.65,0,.35,1)',
    in: 'cubic-bezier(.5,0,.9,.4)',
    slam: 'cubic-bezier(.2,.9,.25,1)',
    linear: 'linear'
  };
  var anims = [];

  function find(target) {
    var el = typeof target === 'string' ? document.querySelector(target) : target;
    if (!el) { throw new Error('tl.add: no element for ' + target); }
    return el;
  }

  window.tl = {
    ease: EASE,
    // frames: Web Animations keyframes; start and dur in seconds; ease: key of EASE or CSS easing.
    add: function (target, frames, start, dur, ease) {
      var a = find(target).animate(frames, {
        duration: dur * 1000, delay: start * 1000, easing: EASE[ease] || ease || 'linear', fill: 'both'
      });
      a.pause();
      anims.push(a);
      return a;
    }
  };

  // One animation per element property set: stops = [{t: seconds, p: {css props}, e: ease}].
  // The value holds before the first stop and after the last; `e` shapes the segment that
  // starts at that stop. Use it instead of several tl.add() calls on the same element, because
  // overlapping animations on one property fight each other.
  window.tl.track = function (target, stops) {
    var total = stops[stops.length - 1].t;
    if (stops[0].t > 0) { stops = [{ t: 0, p: stops[0].p, e: 'linear' }].concat(stops); }
    var frames = stops.map(function (s) {
      var f = { offset: total > 0 ? s.t / total : 0, easing: EASE[s.e] || s.e || 'linear' };
      Object.keys(s.p).forEach(function (k) { f[k] = s.p[k]; });
      return f;
    });
    return window.tl.add(target, frames, 0, total, 'linear');
  };

  // Slanted wipes (clip-path). Four-point polygons so they interpolate: reveal from the left
  // edge to the right (hidden -> shown), clear off to the right (shown -> gone).
  window.tl.WIPE = {
    hidden: 'polygon(0% 0%, 0% 0%, -5% 100%, -5% 100%)',
    shown: 'polygon(0% 0%, 110% 0%, 105% 100%, -5% 100%)',
    gone: 'polygon(110% 0%, 110% 0%, 105% 100%, 105% 100%)'
  };

  // wipe(target, inStart, inEnd, outStart, outEnd): reveal then (optionally) clear.
  window.tl.wipe = function (target, inStart, inEnd, outStart, outEnd) {
    var W = window.tl.WIPE;
    var stops = [{ t: inStart, p: { clipPath: W.hidden }, e: 'out' }, { t: inEnd, p: { clipPath: W.shown } }];
    if (outStart !== undefined) {
      stops.push({ t: outStart, p: { clipPath: W.shown }, e: 'in' }, { t: outEnd, p: { clipPath: W.gone } });
    }
    return window.tl.track(target, stops);
  };

  // Concentric thin arcs (the curved lines of the program look) inside an <svg>.
  window.tl.arcs = function (svg, cfg) {
    var ns = 'http://www.w3.org/2000/svg';
    for (var i = 0; i < cfg.n; i++) {
      var c = document.createElementNS(ns, 'circle');
      var gold = i === cfg.gold;
      c.setAttribute('cx', cfg.cx);
      c.setAttribute('cy', cfg.cy);
      c.setAttribute('r', cfg.r0 + i * cfg.step);
      c.setAttribute('fill', 'none');
      c.setAttribute('stroke', gold ? '#f5b83d' : '#f1f4fa');
      c.setAttribute('stroke-width', gold ? 5 : 2);
      c.setAttribute('stroke-opacity', gold ? 1 : 0.2);
      svg.appendChild(c);
    }
  };

  window.setT = function (seconds) {
    anims.forEach(function (a) { a.currentTime = seconds * 1000; });
  };

  window.addEventListener('load', function () {
    var t = parseFloat(new URLSearchParams(location.search).get('t') || '0');
    document.fonts.ready.then(function () { window.setT(isNaN(t) ? 0 : t); });
  });
})();
