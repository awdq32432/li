# Working rules for this repository

The owner runs the bots and is not a programmer or a quant: explain in plain language and say what
a number means when quoting one. A separate research project (the owner's "Omnibook research"
Claude project) proposes changes; coding sessions here build and test them.

Reference documents, kept in that research project (ask the owner to paste them if needed):
the Omnibook project instructions, `OMNIBOOK.md`, `OMNIBOOK_MAINNET.md`, `OMNIBOOK_PLAYBOOK.md`,
`OMNIBOOK_VERSIONS.md`, and the report *Upgrading a Hawkes/Avellaneda-Stoikov Bot to BTC Options
Market Making*. Refer to them when upgrading any bot.

## Standards for every change and every result reported

- Label what is measured and what is inferred. Never present a guess as a finding.
- Size an idea in dollars per day, with the assumptions, before recommending it.
- Say what would prove it wrong and which logged file tests it. Test against recordings first.
- Respect sample size: under 30 independent events proves nothing; a few hundred is suggestive.
  Events from the same burst or round are not independent.
- Assume testnet and shadow results are easier than real money. Say when a result relies on that.
- One change per version, so a result can be traced to a cause. Omnibook versions run OG 1.1,
  OG 1.2, ... then Bronze 1.1. `leadlag` is a separate strategy with its own version line.
- A proposal has seven parts, in order: what to change; why and the evidence; size in dollars per
  day; how it loses money; how to test without risk; the exact rule with numbers; what result
  would make us drop it.

## Limits

- Never ask for, repeat, print or store API keys, tokens or private keys. They live only on the
  owner's server, in files the code reads.
- Nothing that depends on manipulating a market, misleading an exchange or running multiple accounts.
- Never raise size after a loss.
- Shadow and recording code places no orders. Live trading needs the owner's explicit go-ahead.
- If unsure whether something is a real exchange constraint, say so and suggest asking the exchange.

## The research report, ranked upgrades (for options market making)

1. Arbitrage-free SSVI/eSSVI fair-vol surface; quote in vol, convert to premium when sending.
2. Inventory skew by portfolio vega/gamma in tenor buckets (Baldacci-Bergault-Gueant), not contract count.
3. Cross-asset toxicity: a faster market's price and flow pulls or widens quotes on the slower one.
4. Exchange-side protection: market-maker protection limits, cancel-on-disconnect.
5. Markouts split by flow type (venue, tenor, moneyness, size, time of day).
6. Whalley-Wilmott no-transaction band for delta hedging.
7. Fee- and venue-aware quoting: fees decide where the edge is.
8. Regime overlays: variance risk premium, expiry, weekends, liquidation cascades.
9. Rough-volatility prior for short tenors.
10. Deep hedging, RL quoting, transformer order-book models: defer.

The report's own advice: build 1 to 8 in order; do not start with 9 or 10.

## Practical notes

- The owner's terminal pastes every command twice. End every command given to them with ` #`.
- The server is `mexc-recorder` in AWS Tokyo; this code is deployed at `~/li` with `deploy/install.sh`.
