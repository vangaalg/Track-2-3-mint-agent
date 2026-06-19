"""Feeds layer — assembles ONE market snapshot for an instrument.

A snapshot bundles the three Read-Engine layers' raw inputs:
  * chart   — multi-timeframe OHLCV (1m·3m·15m·1h·day·week·month) + indicators
  * oi      — option-chain summary (PCR, walls, max-pain)   [feeds.oi]
  * macro   — the morning-scorecard feeds (GIFT, crude, ...) [feeds.macro]

Every sub-feed degrades gracefully: missing creds/network → that piece is None
with a note, the rest of the snapshot still builds (mirrors the loaders' skip-on-
missing-creds behaviour). Live pulls run on the user's machine; in the sandbox
the sub-feeds are exercised with injected/mock fetchers.
"""
