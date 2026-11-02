/* Shared helpers. Fills [data-bind] elements from config.js. No network calls. */
(function () {
  var S = window.STREAM || {};
  var H = S.handles || {};
  var PLATFORMS = [
    { key: "twitch",  label: "Twitch",  cls: "p-twitch"  },
    { key: "youtube", label: "YouTube", cls: "p-youtube" },
    { key: "tiktok",  label: "TikTok",  cls: "p-tiktok"  }
  ];

  window.OV = {
    S: S,
    platforms: PLATFORMS.filter(function (p) { return H[p.key]; })
      .map(function (p) { return { key: p.key, label: p.label, cls: p.cls, handle: H[p.key] }; }),
    param: function (k) { return new URLSearchParams(location.search).get(k); },
    pad: function (n) { return String(n).padStart(2, "0"); },
    esc: function (t) {
      return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    }
  };

  function bindAll() {
    document.querySelectorAll("[data-bind]").forEach(function (el) {
      var v = el.getAttribute("data-bind").split(".").reduce(function (o, k) { return o ? o[k] : undefined; }, S);
      if (v != null) el.textContent = v;
    });
    var run = document.querySelector(".ticker .run");
    if (run && Array.isArray(S.ticker)) {
      run.innerHTML = S.ticker.map(function (t) { return "<span>" + OV.esc(t) + "</span>"; }).join("");
    }
    var tiles = document.querySelector("[data-social-tiles]");
    if (tiles) {
      tiles.insertAdjacentHTML("beforeend", OV.platforms.map(function (p) {
        return '<div class="tile"><div class="label"><span class="platform-dot ' + p.cls + '"></span>' +
          p.label + '</div><div class="value">' + OV.esc(p.handle) + "</div></div>";
      }).join(""));
    }
    var clock = document.querySelector("[data-clock]");
    if (clock) {
      var tick = function () {
        var d = new Date();
        clock.textContent = OV.pad(d.getHours()) + ":" + OV.pad(d.getMinutes());
      };
      tick(); setInterval(tick, 10000);
    }
    var date = document.querySelector("[data-date]");
    if (date) {
      date.textContent = new Date().toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" }).toUpperCase();
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bindAll);
  else bindAll();
})();
