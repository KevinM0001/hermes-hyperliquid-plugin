# TradingView Lightweight Charts™ (bundled)

The dashboard tab draws its price chart with TradingView Lightweight Charts™, bundled locally.
Nothing is loaded from a CDN or any other remote source.

| | |
| --- | --- |
| Package | `lightweight-charts` |
| Version | 5.2.1 |
| Source | https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz |
| npm integrity (of the .tgz) | `sha512-IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ==` |
| License | Apache License 2.0 (`LICENSE` in this folder, copied unchanged from the package) |
| Notice | `NOTICE` in this folder (the attribution notice the package's README asks for) |

## What is bundled

`lightweight-charts.standalone.development.js` in this folder is the package's
`dist/lightweight-charts.standalone.development.js`, unchanged: the readable (not minified)
standalone build, which defines the global `LightweightCharts`. Its `/*! @license */` header is kept.

- sha256: `d7b74180a63bde3d282e78f8b0bee5a648a85777c21dfa8a5090593a6db8d84a`

`scripts/build_dashboard.py` checks that hash, then writes `dashboard/dist/index.js` as this file
verbatim, a separator comment, and `dashboard/src/app.js` verbatim. No minification, no other
transformation; the output is deterministic.

## Attribution shown to users

The package's README (License section) asks for TradingView to be credited as the product
creator, for the NOTICE text, and for a link to https://www.tradingview.com/. The tab does both:

1. The chart keeps `layout.attributionLogo: true`, so the TradingView logo link is drawn on the
   chart itself.
2. A visible text line under the chart reads:
   "Charts by TradingView Lightweight Charts™ · Copyright (с) 2025 TradingView, Inc. · https://www.tradingview.com/"
   (the NOTICE text, with the link).

The package also incorporates parts of tslib ((c) Microsoft Corporation) under the BSD Zero
Clause License, as its README states.
