# Third-party material

## Fonts

The fonts in `fonts/` are bundled so the overlays work offline. All three families are licensed under the SIL Open Font License, Version 1.1. The full licence text for each family, fetched from the upstream Google Fonts repository, sits next to the font files.

| Family | Files | Copyright holder | Licence text |
|---|---|---|---|
| Chakra Petch | `ChakraPetch-500.woff2`, `-600`, `-700` | The Chakra Petch Project Authors | `fonts/OFL-ChakraPetch.txt` |
| IBM Plex Mono | `IBMPlexMono-400.woff2`, `-500`, `-600` | IBM Corp. (Reserved Font Name "Plex") | `fonts/OFL-IBMPlexMono.txt` |
| IBM Plex Sans | `IBMPlexSans-var.woff2` | IBM Corp. (Reserved Font Name "Plex") | `fonts/OFL-IBMPlexSans.txt` |

Upstream sources: <https://github.com/google/fonts/tree/main/ofl/chakrapetch>, `.../ibmplexmono` and `.../ibmplexsans`.

The `.woff2` files are web-format builds of these families. Keep the licence files with them if you redistribute the fonts.

## Rocket League

This project is unofficial and fan-made. It is not affiliated with or endorsed by Psyonix or Epic Games. Rocket League is a trademark of its owner. No game assets, logos, footage or replays are included. The optional relay only reads the game's own local Stats API, which the player enables themselves; see <https://www.rocketleague.com/en/developer/stats-api>.

## Dependencies

None. The Node and Python code uses only the standard libraries. External programs used when you run the render tools (not bundled): OBS Studio, ffmpeg, Google Chrome.
