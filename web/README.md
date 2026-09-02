# web

The Phase 6 frontend: Vite + React + TypeScript, with its API types **generated** from the
query API's OpenAPI document rather than hand-written.

```bash
npm install
npm run generate:api    # needs the API running; writes src/api/schema.ts
npm run dev             # proxies /api to localhost:8080
npm run build
```

`GIELINOMICS_API` overrides the API origin for both the dev proxy and the generator:

```bash
GIELINOMICS_API=http://localhost:5199 npm run generate:api
```

## Why the types are generated

The hiscores endpoint sends no CORS headers, so a browser cannot call the upstream APIs
directly — everything goes through the query API, and that API's OpenAPI document is the
single description of the contract. `src/api/schema.ts` is generated output; edit the C#
endpoints and re-run `generate:api` rather than editing it.

Every endpoint is annotated with `.Produces<T>()` on the server for exactly this reason. An
endpoint returning an anonymous object produces a schema that says it returns *nothing*, and a
generated client that types every response as `unknown` — which is worse than no generation,
because it looks like it worked.

## Charts

Hand-rolled SVG rather than a charting library: three chart forms, none of which needs a
100 kB dependency, and full control of the accessibility details.

The palette is the validated default from the data-viz reference — the two categorical slots
in use (blue / orange) clear the lightness band, chroma floor, all-pairs colour-vision
separation, normal-vision floor and 3:1 surface contrast in **both** light and dark. Dark is a
selected set of steps for the dark surface, not an automatic inversion.

Rules the charts hold to:

- **One y-axis, never two.** Both price series are gp, so they share a scale — and the distance
  between them *is* the spread, which a dual axis would render meaningless.
- **Gaps break the line.** A window with no trades is drawn as a break, not interpolated across.
  Drawing a confident straight line through data the platform does not have is precisely the
  claim `ingest_runs` exists to stop anyone making.
- **Identity is never colour alone.** Every multi-series chart carries a legend *and* direct
  labels at the line ends; deltas carry an arrow glyph and a sign; feed status carries an icon
  and a word.
- **Volume is measured from zero**, because a bar length only means anything against a zero
  baseline. Price is not, because an item trading between 800k and 810k would otherwise be a
  flat line against a zero axis, hiding the only movement there is.

## Wiki data in the UI

Two views exist only because of the cross-source join, and both had the same failure mode on
first render: the wiki carries near-duplicate rows that crowd out anything useful.

- **Gear** ranks equipment by a stat against its price. The wiki has a cosmetic, beta or Last
  Man Standing variant of most notable weapons with identical bonuses and no price — four rows
  of "Elder maul" ahead of anything buyable — so untradeable variants are hidden by default.
- **Monsters** prices a drop table into gp per kill. Identical drops appearing under several
  versions of one monster collapse to a single row; summing them reported a kill as worth
  roughly twice what it is.

Both surface what they could not price rather than quietly rounding it away: a kill's value is
labelled a floor, and the rows that contribute nothing to it are counted in the caption.

## State

The watchlist and the theme choice live in `localStorage`, per browser. Both reads and writes
are wrapped — a private window or a browser blocking site data should not take the page down.
Nothing here is shared state; that would need accounts the platform does not have.
