# Contributing

This is a hobby project that was built for one streamer's setup and then made generic. Please be realistic about what to expect:

- There is no support guarantee and no response-time promise. Issues and pull requests may sit for a while or go unanswered.
- It has only been tested on Fedora Linux with the Flatpak build of OBS. Reports from other systems are welcome, but fixes there are on a best-effort basis.
- Small, focused pull requests are the easiest to review: one fix or one feature, with a test where the code has tests.

Before sending a change:

1. Run the tests (commands are in the README) and keep them passing.
2. Keep the Node and Python code dependency-free (standard library only).
3. Keep brand, names and sponsors in `config.js`. Do not hard-code a channel, team, player or sponsor into a page or tool.
4. Never commit secrets (OBS password, stream keys), personal paths, game assets or footage.
5. Copy rules: no em or en dashes, no letter-spacing, no glow or spotlight effects (the design tests check this).
6. Commit messages use `<type>: <description>`, for example `fix: keep the series text on reset`.

By contributing you agree that your contribution is licensed under the MIT licence of this repository.
