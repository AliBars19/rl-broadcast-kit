/* ==========================================================================
   EDIT THIS FILE ONLY. Every overlay reads its text from here.
   After saving, right-click a Browser Source in OBS > Refresh (or
   tick "Refresh browser when scene becomes active").
   Do NOT put stream keys or passwords here. Nothing in this folder needs them.
   ========================================================================== */
window.STREAM = {
  name: "YOUR NAME",                        // shown big on the lower third and stream info
  channel: "YOUR CHANNEL",                  // channel / brand name (Up Next bar, lower bar, intro credit)
  mark: "Y",                                // ONE letter shown in the square brand mark
  tagline: "YOUR LEAGUE | ROCKET LEAGUE",
  event: "YOUR EVENT",                      // top-left label on the full-screen scenes
  game: "Rocket League",

  // Leave a handle empty ("") to hide that platform everywhere.
  handles: {
    twitch:  "",
    youtube: "@yourchannel",
    tiktok:  "@yourchannel"
  },

  // Starting Soon countdown, in minutes. You can also override it per
  // source by adding ?min=10 to the end of the file path in OBS.
  startingMinutes: 5,

  // Shown on the Ending screen
  nextStream: "Next stream: Thursday 19:00",

  // Scrolling ticker on the full-screen scenes
  ticker: [
    "Rocket League with YOUR CHANNEL",
    "Follow so you do not miss match nights",
    "Be nice in chat",
    "!socials for links"
  ],

  // Lower third: appears 4 s after the source loads, stays showSeconds,
  // then comes back every everyMinutes. Set everyMinutes: 0 to show once.
  lowerThird: { showSeconds: 12, everyMinutes: 15 },

  // Boost panels (boost.html). "me" = your in-game name, highlighted in amber.
  boost: { me: "YOUR NAME", relay: "ws://127.0.0.1:49124" },

  // Stream-info panel: seconds between rotating social handles
  infoRotateSeconds: 8,

  // Scoreboard (scoreboard.html): team bars either side of Rocket League's own
  // score boxes. abbr = short text on the logo plate (leave "" to hide it).
  // The names and abbr below are the fallback whenever the game sends no names.
  scoreboard: {
    blue:   { name: "BLUE TEAM",   abbr: "BLU", wins: 0 },   // wins: set by hand after each game
    orange: { name: "ORANGE TEAM", abbr: "ORG", wins: 0 },   // wins: set by hand after each game
    autoNames: true,   // true: names come from the match data when the game provides them; false: always use the names above
    bestOf: 3,         // 3, 5 or 7; sets how many pips show
    tab: "YOUR EVENT | MATCHDAY 1 | SERIES 1 OF 3",   // free text under the clock, edit it any time
    idleText: ""   // centre panel text while no match is live; "" = the event label above
  },

  // ---- Program screens (program/*.html) ----------------------------------
  // OPTIONAL sponsor. Leave this as null and every "PRESENTED BY" spot disappears.
  // To use one, replace null with an object (paths are relative to this folder):
  //   sponsor: {
  //     name: "Sponsor Name",              // shown as text if there is no logo
  //     logo: "program/assets/sponsor.svg",    // wordmark, small spots (a light logo reads best on the blue field)
  //     lockup: "program/assets/sponsor-big.svg"  // optional larger version for the big spots; falls back to logo
  //   },
  sponsor: null,

  // Promo cards on the program screens, one at a time, every 8 seconds.
  // tag = small gold label, text = headline, sub = small line, logo = optional image path.
  promos: [
    { tag: "STATS",   text: "yourstats.example.com",  sub: "Standings and match stats" },
    { tag: "YOUTUBE", text: "@yourchannel",           sub: "Highlights and full matches" },
    { tag: "TIKTOK",  text: "@yourchannel",           sub: "Clips from every game" },
    { tag: "SHORTS",  text: "NEW SHORTS EVERY MATCHDAY", sub: "" }
  ],

  // stage = amber chip under the event wordmark (the dock overrides it live); minutes = countdown when no timer is set.
  // eventName / eventSub = top-centre wordmark and the line under it.
  programDefaults: {
    stage: "MATCHDAY 1",
    minutes: 5,
    eventName: "YOUR EVENT 26",
    eventSub: "YOUR LEAGUE | ROCKET LEAGUE"
  }
};
