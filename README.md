# insta-otomasyon

A **local-first content studio and Instagram publishing engine**, written for Turkish-speaking creators (the UI and docs are in Turkish).

Type a topic, press one button, get a ready-to-post package: **title options, caption, hashtags, SEO keywords, analysis, scene-by-scene script and a cover image**. Then pick stock footage or images for each scene, and (once your Meta account is connected) queue Reels and Stories for publishing.

Everything runs on your own computer. There is no server, no account, no telemetry. **You bring your own API keys; whatever they cost is yours, not ours.**

## What works today

| | Status |
|---|---|
| **Content Studio** (topic → title, caption, hashtags, SEO, analysis, script, cover) | Working. Tested live with the `claude` CLI. |
| **Trend / hashtag check** against real Google + YouTube search suggestions | Working. Marks each hashtag *verified* or *model suggestion*. It never invents volume numbers. |
| **Stock media search** per scene: Pexels, Pixabay (your own free API key) | Working against mocks and real error paths; a successful call with a real key is **not yet verified**. |
| **Keyless image sources**: Wikimedia Commons, NASA, The Met, Art Institute of Chicago, Cleveland Museum of Art | Working, **verified live** (search + download). License filter drops NC/ND/non-free; CC BY / CC BY-SA attribution lines are generated. |
| **Free video-tool directory** (what each site's free tier really offers, with sources and dates) | Working. Only what the official pricing pages state. |
| **Instagram publishing engine** (drop a file in a folder → Reels / Story via the Graph API) | Implemented and unit-tested against a fake API. **Never run against real Meta yet.** |
| Video assembly (script + voice + footage + captions → mp4) | Not built yet. |
| Instagram login / one-click publish from the panel | Not built yet. |
| `codex` CLI and Anthropic API as writing engines | Implemented, **untested** on a real machine. |

## Quick start

Requires Python 3.12+ (developed and tested on Windows 11). The writing engine is whatever you already have: the logged-in `claude` CLI (`claude auth login`), the `codex` CLI, or an `ANTHROPIC_API_KEY`.

```bash
pip install -r requirements.txt
python -m playwright install chromium     # once, for cover images
python -m src.icerik                      # serves the panel at http://127.0.0.1:8000
```

The panel binds to `127.0.0.1` only. Packages are written to `paketler/`. Details: [ICERIK-STUDYOSU.md](ICERIK-STUDYOSU.md). The Instagram publishing engine is documented in [BASLA.md](BASLA.md); the roadmap is in [URUN-PLANI.md](URUN-PLANI.md).

## Safety and honesty notes

- **No Instagram passwords, ever.** Publishing uses Meta's official OAuth/Graph API only. Scripting a logged-in browser violates Instagram's terms and risks the account.
- **API keys** live in `config/anahtarlar.json` on your disk, in plain text (git-ignored, never echoed back by the panel or written to errors/logs). Environment variables take precedence.
- **Licenses matter.** Images that need attribution (CC BY, CC BY-SA) are flagged, and the exact credit line goes to `krediler.txt` next to the package. This is not legal advice; check the source page.
- The local panel is hardened against other websites (host/origin/`Sec-Fetch-Site` checks, strict CSP with no inline script, SSRF-safe downloads, per-source download allow-lists). See the tests.
- A new, empty Instagram account that auto-posts at volume looks like spam to Instagram. Default to a few posts a day with a preview step.

## Tests

```bash
python -m pytest testler -q
```

The suite (~670 tests) uses recorded real API responses in `testler/fikstur/`. Security-critical checks were mutation-tested: breaking the guard must make a test fail.

## Credits

The SSRF guard, license filter and keyless-source lessons are ported to Python from [Otopilot](https://github.com/wj-sidiss/otopilot) (MIT, same author).

## License

[MIT](LICENSE)
