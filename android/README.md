# NEURAVEX Android

A thin control + execution client for your self-hosted NEURAVEX backend.

## What this app does and does not do

Does:
- Shows your account overview, open positions, and the AI's reasoning for
  every decision (the same data as the web dashboard)
- Lets you set which coins NEURAVEX is allowed to trade and a single
  balance-floor stop — enforced by the backend's risk engine on every
  trade, not just a UI suggestion
- Polls your backend for trades the risk engine has already approved, and
  executes them itself: on Binance using an exchange API key, or on-chain
  via Uniswap (Polygon) using a wallet private key — both held on-device
  via Android Keystore-backed encryption (see
  `security/SecureCredentialStore.kt`)
- Reports your real wallet balance (wallet mode) back to the dashboard

Does not:
- Never sends your exchange API key/secret or wallet private key to the
  NEURAVEX backend or anywhere else
- Never trades outside the limits you set in Settings — those limits live
  server-side in the risk engine, not just in this app
