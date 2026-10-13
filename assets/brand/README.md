# UnderStory identity

- `logo.svg`: supplied UnderStory silhouette, unchanged path geometry, transparent background and `currentColor` fill.
- `app-icon.svg`: editable vector master, #74c184 (青芽) background and centered white silhouette.
- Background: an independently calculated superellipse, |(x-256)/256|^4.2 + |(y-256)/256|^4.2 = 1, sampled at 512 points. Its curve is produced by `scripts/draw-icon-background.py`; no third-party template contour is embedded.
- `app-icon.png`: 1024px export. `app-icon.ico`: 16, 24, 32, 48, 64, 128 and 256px images for Windows.
- `preview.png`: actual-size comparisons and monochrome marks on light/dark surfaces.

Export with `node scripts/build-brand-assets.mjs <installed-sharp-module-path>` (omit the argument when sharp is locally installed).

To regenerate the background first, run `python scripts/draw-icon-background.py`. The book/tree silhouette and its placement are retained unchanged.

The device's green preference keeps the stable `green` key. Brand color is #74c184; light-theme emphasis uses #347645, and white-label button fills use #367b46 for legibility. Page backgrounds remain neutral.
