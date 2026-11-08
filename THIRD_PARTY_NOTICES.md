# Third-party notices

## TradingView Lightweight Charts™ 5.2.1 (bundled)

```
TradingView Lightweight Charts™
Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/
```

- License: Apache License 2.0. The full text is in `dashboard/vendor/lightweight-charts/LICENSE`, and the NOTICE is in `dashboard/vendor/lightweight-charts/NOTICE`.
- What is bundled: `dist/lightweight-charts.standalone.development.js` from the npm package `lightweight-charts@5.2.1` (https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz, integrity `sha512-IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ==`). It is copied verbatim, readable and unminified, with its `@license` header, at the top of `dashboard/dist/index.js`.
- The upstream package says it "incorporates several parts of tslib (https://github.com/Microsoft/tslib, (c) Microsoft Corporation) that are covered by BSD Zero Clause License."
- Attribution: the price chart keeps the library's on-chart TradingView attribution logo (`layout.attributionLogo: true`), which links to https://www.tradingview.com/. A text line under the chart repeats the attribution notice and the link, as the upstream README asks.

No other third-party code is bundled. The Python runtime dependency `websockets` (BSD-3-Clause) is installed by Hermes from PyPI, not vendored.
