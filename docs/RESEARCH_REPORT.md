# Upgrading a Hawkes/Avellaneda-Stoikov Bot to BTC Options Market Making: Publicly Known Models Ranked by Practicality for a Small Desk

*Saved from the owner's research project on 19 Nov 2026 so coding sessions can refer to it. The text is
the owner's report as pasted; only headings and the table were reformatted. Bracketed numbers refer
to the source list at the end.*

The upgrade with the most impact is not a more exotic model. It is changing what the bot counts as "inventory" and "fair value". Quote BTC options in implied-vol space off an arbitrage-free surface, and skew those quotes by portfolio vega and gamma, not by contract count. That is the Baldacci–Bergault–Guéant reduction of Avellaneda-Stoikov to options. Then extend the Hawkes toxicity engine across assets so that perp/futures order flow, Deribit block prints and DVOL moves can pull or widen option quotes before the screen gets picked off. Everything else is second-order next to these two changes, including deep hedging, RL, rough-vol pricing and transformer LOB models.

## TL;DR

- Top three upgrades by P&L per unit of effort:
  1. An SSVI/SVI fair-vol surface with vol-space quoting.
  2. Avellaneda-Stoikov inventory skew rewritten in vega/gamma buckets (Baldacci, Bergault & Guéant show the high-dimensional options problem reduces to a low-dimensional one in portfolio vega, scaling linearly in the number of options).
  3. Cross-asset toxicity gating (perp micro-price/Hawkes → option quote pulls), backed by Deribit's exchange-side Market Maker Protection (quantity, delta and vega limits per MMP group).
- Fees and venue structure decide where the edge is. Deribit charges 0.03% of the underlying to makers and takers alike, capped at 12.5% of premium. OKX charges 0.02% maker / 0.03% taker with a 7% cap. More than 50% of Deribit options volume printed as blocks in 2025 (Paradigm's Nicolas Quatravaux), and IBIT reached a record 52% of BTC options open interest in Jan 2026 (CoinDesk citing Checkonchain). A screen-only bot is therefore quoting the residual, more retail and more toxic part of the flow. Edge has to be measured in vol points net of fees, by tenor and moneyness, not in bps of notional.
- Small-desk fit: a single developer can build items 1–7 below in Python with Rust hot paths over 3–6 months on Deribit/OKX API access. Mass-quote binary APIs, LD4 colocation, deep hedging and RL market makers only pay once the vol-space core is profitable. The top firms' public material (SIG poker/decision training, Optiver "quote a surface", Jane Street's SEBI-documented cross-segment index-options trading) all says the same thing: edge comes from a better surface plus better reading of who you're trading with, not from speed alone.

## Key Findings

### Executive summary: ranked upgrades (expected P&L impact vs. effort)

| Rank | Upgrade | P&L impact | Effort (1 dev) | Small desk? | Bolts onto |
|---|---|---|---|---|---|
| 1 | Arbitrage-free SSVI/eSSVI fair-vol surface; quote in vol, convert to premium at send-time | Very high (foundation for everything else) | 3–5 weeks | Yes | Signal engine → alpha model |
| 2 | Greek-space inventory skew: A-S/GLFT with portfolio vega (bucketed by tenor) + delta-hedged underlying | Very high | 2–3 weeks once #1 exists | Yes | Alpha model (inventory risk) |
| 3 | Cross-asset toxicity: perp/futures micro-price + Hawkes intensity + block/DVOL marks gating option quotes | High (defensive; stops the biggest losses) | 3–4 weeks (reuses existing Hawkes code) | Yes | Signal engine → execution gate |
| 4 | Exchange-side protection: Deribit MMP (qty/delta/vega limits, MQQ), cancel-on-disconnect, OKX MMP | High tail-risk reduction | Days | Yes | Risk engine |
| 5 | Vega-weighted markouts by flow type (screen vs block, tenor, moneyness, size, venue) | High (tells you where to quote) | 1–2 weeks (extends existing markout module) | Yes | Research / P&L decomposition |
| 6 | Whalley-Wilmott no-transaction band for delta hedging on perps/futures | Medium (cuts hedge cost and slippage) | Days | Yes | Execution engine |
| 7 | Fee-cap-aware, venue-aware quoting (Deribit 12.5% cap vs OKX 7%) | Medium | Days | Yes | Alpha model / router |
| 8 | Regime overlays: VRP default bias, expiry-gamma/pin, weekend and liquidation-cascade skew rules | Medium | 2–4 weeks | Yes | Alpha model |
| 9 | Rough-vol (rBergomi-style power-law) prior for short-dated term structure and skew (0DTE–7D) | Medium for short tenors | 4–8 weeks | Partly | Surface engine |
| 10 | Deep hedging / RL quoting / transformer LOB models | Uncertain; research bet | Months | Mostly no | Later stage |

## Details

### 1. Fair-vol surface: SVI/SSVI, quote in vol

**What it is.** Fit every expiry slice with SVI, and the whole surface with SSVI/eSSVI (Gatheral & Jacquier, "Arbitrage-free SVI volatility surfaces", arXiv 1204.0646).[1] Every quote is then a vol number read off the surface, plus a skew from inventory and toxicity. It is converted to premium at the moment of sending.

**Why it beats the current approach.** Today the bot's fair value is effectively a single mid. In options you are quoting dozens to hundreds of instruments, most of which barely trade. A surface lets a trade in one strike update fair value in its neighbours, and the trading-school summaries of Optiver/IMC/SIG-style desks frame competition as "whose surface is better", not "where is fair value".[2][3][4]

**Key equations.**

- SSVI total variance: w(k, θ_t) = (θ_t/2)·{1 + ρφ(θ_t)k + √[(φ(θ_t)k + ρ)² + 1 − ρ²]}, where k = log-moneyness and θ_t = ATM total variance.
- Sufficient no-butterfly-arbitrage conditions (Gatheral-Jacquier Thm 4.2): θφ(θ)(1+|ρ|) < 4 and θφ(θ)²(1+|ρ|) ≤ 4.[5]
- No calendar arbitrage requires ∂_tθ_t ≥ 0, plus a monotonicity condition on the skew.[6]
- With the power-law φ(θ) = η/(θ^γ(1+θ)^(1−γ)), the surface is completely free of static arbitrage provided η(1+|ρ|) ≤ 2.[1]
- eSSVI (Hendriks & Martini, 2019) lets ρ vary by maturity.[7][8] This matters in BTC, where front-end skew flips sign between call-skew rallies and put-skew crashes.

**Real-time fitting.**

- Fit the surface to mid vols of liquid strikes, weighted by inverse bid-ask width in vol.
- Warm-start from the previous fit and refit only θ (ATM level per expiry) on every perp tick; refit ρ and η on a slower clock (seconds) or on trades.
- With 3 parameters per slice this is a sub-millisecond least-squares problem in Rust, and it is already practical in numba.
- Between refits, move the surface with the underlying using a sticky rule estimated from data. Do not assume one.
- The practical approach is a minimum-variance delta: Δ_MV = Δ_BS + ν·∂σ_imp/∂S, with ∂σ/∂S estimated by regressing ATM IV changes on log-returns per tenor (Hull & White, "Optimal delta hedging for options", 2017). BTC's spot-vol correlation changes sign across regimes, so re-estimate it on a rolling window.

**Deribit-specific pitfalls.**

- BTC options on Deribit are inverse: the payoff is settled in BTC as max(S−K,0)/S.[9]
- The BTC-denominated delta of a call is therefore Δ_USD − C/S. Hedging with the USD Black-Scholes delta leaves a systematic premium-delta error.
- USDC-settled linear options, added in August 2025, do not have this issue.[10] Keep the two books' greeks separate.

**Data.** Deribit/OKX option L1/L2 plus the index and perp, from tardis.dev historical data or self-recorded WebSocket feeds.

**Small desk:** yes. This is the core.

### 2. Greek-space inventory: Avellaneda-Stoikov → Baldacci-Bergault-Guéant

**What it is.** Baldacci, Bergault & Guéant, "Algorithmic market making for options" (arXiv 1907.12433), take a book of N options on one underlying under a one-factor stochastic-vol model (e.g. Heston).[11][12] They assume continuous delta hedging, so the only residual risk is vega.[12][13] Under a constant-vega approximation, "the problem faced by an option market maker is characterized by a low-dimensional functional equation that can be solved numerically using a Euler scheme along with interpolation techniques, even for large portfolios", and "our method scales linearly in the number of options."[12][14]

The optimal quotes for each option are a function of portfolio vega V_t^π = Σ_i q_t^i V^i, not of the individual position vector.[12] The authors note the method extends to two-factor Bergomi-type models at the cost of one extra dimension.[12]

Earlier single-option work:

- Stoikov & Sağlam, "Option market making under inventory risk" (Review of Derivatives Research, 2009).[13][15]
- El Aoud & Abergel: a single delta-hedged option under stochastic vol, with model-misspecification risk.[12][13]

High-dimensional extension: arXiv 2009.00907, "An approximate solution for options market-making in high dimension". It relaxes the constant-vega assumption by linearising the value function around the initial vegas.[16]

**Why it beats the current approach.** The current bot skews by unit inventory (1 BTC limit). In options, 10 contracts of a 2-day 25-delta put and 10 of a 3-month ATM call have completely different risk. The vega/gamma book is what blows up.

**Practical bolt-on (heuristic consistent with BBG, not their exact solution).**

- Keep the GLFT closed-form approximation you likely already use for A-S: δ^{b,a}(q) ≈ (1/γ)ln(1+γ/k) ± ((2q+1)/2)·√[(σ²γ/(2kA))·(1+γ/k)^{1+k/γ}].
- Replace the inventory/variance term with a vega-space penalty:
  - V = vector of portfolio vega in K tenor buckets (e.g. 0–2D, 3–10D, 11–45D, 45D+).
  - Σ_v = covariance of daily or intraday changes in ATM IV per bucket. Optionally use 3 PCA factors: level, term slope, skew.
  - For a candidate fill in option i with vega vector ν_i, the marginal inventory cost is ≈ γ·h·ν_iᵀΣ_vV + (γh/2)·ν_iᵀΣ_vν_i, where h is the expected holding horizon.
  - Shift the bid/ask vol by that cost divided by option vega.
- Add a gamma term the same way, using realised-variance covariance, for short tenors where gamma dominates.
- Delta is hedged separately (item 6), so it does not enter the quote skew except through hedge-cost expectations.

**Data.** Historical surface fits, for Σ_v and fill-intensity curves Λ(δ) per tenor/moneyness bucket. The fill intensity can be fitted with your existing Hawkes machinery, with δ measured in vol points.

**Pitfalls.**

- Constant vega fails near expiry. For tenors under about 2 days, switch to a gamma/theta-based penalty.
- Fill intensities in thin options books are sparse, so pool across strikes by delta bucket.

**Small desk:** yes. This is mostly a rewrite of the existing alpha model's inventory term.

### 3. Cross-asset toxicity: extend the Hawkes gate across assets

**What it is.** In BTC options most short-horizon adverse selection comes from the underlying moving before you re-quote, not from option flow itself. The fix is a multivariate Hawkes model in which perp/futures trades (by side), option trades, and block-trade prints excite option-quote risk. Pair it with a micro-price fair value for the underlying.

**Evidence.**

- Order-flow features carry information on sub-second horizons in BTC. In Albers, Cucuringu, Howison & Shestopaloff, "Fragmentation, Price Formation, and Cross-Impact in Bitcoin Markets" (arXiv 2108.09750), a transformed trade-flow imbalance raised R² on the Deribit perpetual from 0.0149 to 0.0744, an increase by a factor of 7.3.[17]
- Stoikov's micro-price ("The micro-price: a high-frequency estimator of future prices", Quantitative Finance 18(12), 2018) is "a martingale by construction" and empirically a better short-term predictor than the mid or the weighted mid.[15][18] Use it as the S that feeds option fair values.
- Informed option flow exists on Deribit. Alexander et al., "Net Buying Pressure and the Information in Bitcoin Option Trades" (arXiv 2109.02776), find informed trading in Deribit options. It operates "at all points along the term structure", with more informed trading on volatility information than on direction.[19][20]
- A 2026 study of 56,168 hourly Deribit chain snapshots (Jan 2020–Jun 2026) found that ATM IV and term-structure slope forecast realised vol beyond a HAR benchmark, but "no signal robustly predicts return direction."[21]

**Algorithm.**

- Make the existing 6-dimensional Hawkes model cross-asset. Dimensions: perp buy/sell trades, perp best-quote depletions, option buy/sell aggressions (pooled by tenor), and block prints. Marks are size and |delta|·size.
- Define a toxicity score as the predicted 1–5 s absolute micro-price move plus the conditional intensity of same-side aggressions.
- Gate per tenor bucket:
  - Above threshold 1, widen by f(score).
  - Above threshold 2, pull the side exposed to the move.
- On a perp micro-price jump greater than x·σ_1s, cancel all quotes whose delta exposure loses, before re-pricing.
- The research finding that about 2/3 of BTC/USDT trades are endogenous carries directly over: endogenous bursts are predictable, so pull at burst onset.

**Block flow.**

- In 2025, block trades were more than 50% of Deribit options volume, against roughly a third historically (Nicolas Quatravaux, Paradigm head of EMEA, in his essay "2025 was the biggest year ever for crypto options", which also notes Deribit "still accounts for more than 80% of centralized crypto options volume").
- Paradigm's April 2022 newsletter reported "70.2% of all Deribit BTC option… DRFQ transactions on Paradigm were priced better than the top of book" and "49% of all option DRFQs were multi-leg."[22]
- Block prints are informed and large. Treat a block print in an expiry as a high-weight Hawkes mark that shifts that expiry's θ and ρ immediately.
- In the 30 Oct 2026 Binance theft, Paradigm's Nicolas Quatravaux told CoinDesk that the first block (250 × $78k puts expiring 31 Oct, about $20M notional) printed at 20:38 UTC, before public news, and that about $590M of put-heavy BTC blocks over 8 hours was "close to half the total block volume on Deribit." This is a vivid example of block flow leading the screen; treat it as a single reported episode, not a statistical regularity.

**Small desk:** yes in Python/numba. A sub-millisecond reaction needs Rust plus colocation (see Infrastructure).

### 4. Exchange-side protection: MMP, mass quotes, cancel-on-disconnect

**What Deribit documents:**

- MMP lets clients set a monitoring interval, a frozen time, and quantity, delta and vega limits, plus a Maximum Quote Quantity (MQQ) that caps total open MMP size.[23]
- MMP is "available upon request and requires a one-time manual activation by Deribit staff." Mass Quote must be requested separately.[24]
- Mass quotes:
  - Each mass quote is tied to an MMP group, and only "one quote per side per instrument per MMP group" is allowed. N book layers need N groups.[25]
  - FIX MassQuote "requires Cancel On Disconnect enabled and MMP."[26]
  - The Starbase binary API supports "mass quotes (up to 15 double-sided quotes)" per message.[27]
- Starbase is the recommended lowest-latency path. The legacy SBE feed is scheduled for deprecation at end-2026.[27]

**How to set it up:**

- Set the MMP vega and delta limits at roughly 1–2× the inventory-risk budget from item 2. If your software gate fails, the exchange freezes you.
- Treat MMP triggers as Hawkes events in their own right: a trigger means a sweep.

**Small desk:** yes. This is the cheapest risk reduction available.

### 5. Vega-weighted markouts by flow type

Extend the existing fill-markout and fill-conditional toxicity modules:

- Measure markouts in vol points × vega (and in delta-adjusted USD) at 1 s, 10 s, 1 m, 5 m and 1 h.
- Split by venue (Deribit screen, Deribit block/RFQ, OKX), tenor bucket, delta bucket, size, time-of-day/weekend, aggressor type, and pre-fill toxicity score.

This is how SIG/Optiver-style desks decide where to be tight. It replaces "average 10–15 bps per trade" with an edge map. Expect the edge map to show:

- positive edge in small retail-sized short-dated OTM flow;
- negative edge on screen fills that immediately precede perp moves or block prints.

Quote tight only where the 1-minute markout net of fees is positive.

### 6. Delta hedging with a no-transaction band (Whalley-Wilmott)

**Formula.** Hedge only when the portfolio delta leaves the band Δ* ± H, where H = [3·e^{−r(T−t)}·λ·S·Γ²/(2γ)]^{1/3}. Here λ is the proportional cost (fee plus half-spread plus impact) and γ is risk aversion (Zakamouline's EFMA 2005 review).[28][29]

**Properties:**

- Rebalance to the band edge, not to Δ*.[30]
- Use portfolio gamma: netting across strikes shrinks Γ and widens the band appropriately.[31]
- The asymptotic formula underestimates the optimal band when costs are large.[32] That makes it a conservative starting point.
- Zakamouline also discusses the Barles-Soner volatility adjustment that shifts the band's centre.[28]

**Neural version.** The "No-Transaction Band Network" (Imaki et al., arXiv 2103.01775) learns the band and uses Whalley-Wilmott as its benchmark.[33] It is a sensible first deep-hedging experiment (library: pfhedge).

**Execution.** Hedge passively on the perp when the band allows. Cross only on breach, and route to the venue with the best maker fee/rebate.

**Small desk:** yes. Days of work, and an immediate saving on hedge costs.

### 7. Fee-cap- and venue-aware quoting

**Deribit:**

- Options cost 0.03% of the underlying index for makers and takers alike, capped at 12.5% of the option price. Delivery is 0.015%, also capped at 12.5%.[10][34]
- Because makers pay the same as takers, there is no passive rebate to earn on Deribit options.[34] Your edge must cover a full fee on every fill.

**OKX:**

- 0.02% maker / 0.03% taker, capped at 7% of premium.[35]
- CoinCodeCap's "Deribit Review 2026" compares fees on a $200 premium at $100,000 notional: $25.00 at Deribit, $14.00 at OKX and $8.26 at Delta Exchange (3.5% cap).

**Implications:**

- For ATM and near-dated options, the fee is a fixed ~3 bps of the underlying per side. On a 1-day ATM option worth about 1–1.5% of spot, that is 2–3% of premium. Your required half-spread in vol points therefore rises sharply as tenor shortens.
- Compute the minimum half-spread per instrument as max(fee_i, toxicity cost_i) + inventory cost_i, all in premium. Route each quote to the venue where that minimum is lowest.
- OKX's lower maker fee and 7% cap make it structurally cheaper for cheap wings.

### 8. Regime overlays: VRP, skew, expiry gamma, weekends

**Variance risk premium.** Alexander & Imeraj built a Bitcoin VIX from more than 7 million Deribit option prices. Its short-maturity reading exceeded 200% after the COVID shock.[36][37] Almeida, Grith, Miftachov & Wang estimate a Bitcoin variance risk premium of 0.14 (annualised variance units), against roughly 2% for the S&P 500 in Bollerslev-Tauchen-Zhou.[38] Unlike in equities, the BTC VRP is driven mainly by upside states, and both the return premium and the VRP are lower in low-vol regimes.[39]

Practical use:

- Let inventory sit slightly short vega by default (a smaller penalty for short-vol fills).
- Do not short call wings cheaply in rallies, because the VRP decomposes toward upside risk.

**Expiry and gamma.**

- Weiss et al., "Bitcoin option expiration, gamma exposure, and intraday price reversals" (Finance Research Letters vol. 107, 2026), covering 1,059 Deribit expiry days from Jan 2021 to Dec 2023, find statistically and economically significant intraday BTC return reversals around Deribit expiries, with "annual wealth transfers of approximately USD 50 million between option writers and holders." They are strongest on days with high ATM open interest and when cumulative gamma exposure is negative, consistent with market makers hedging net short gamma. Activity also rises on the spot venues that feed the settlement index.
- Conflicting view: Lachowicz (SSRN 5782822) argues aggregate gamma is too small relative to volume to move price.[40]
- Reconcile the two by using GEX only as a conditioning variable for the near-expiry window, not as a directional signal.
- Deribit expiries are at 08:00 UTC, with settlement on an averaged index. Widen and size-limit the 0DTE strikes nearest spot in the final hour; pin risk sits in the front strike.

**Weekends and liquidation cascades.** Spot depth is thin on weekends and cascades are self-exciting, which is exactly your Hawkes finding. Add a regime state (weekend, funding extreme, open-interest-driven liquidation intensity) that:

- raises γ;
- steepens put-wing ρ in the surface prior;
- tightens MMP limits.

**IBIT.**

- IBIT options took 45% of BTC options open interest against Deribit's 41.9% in Sep 2025 (about $38B vs $32B, per CoinDesk), and reached a record 52% (roughly $33B) in Jan 2026, per CoinDesk citing Checkonchain.
- CoinDesk, citing Checkonchain, reported on 21 May 2026 that Deribit had regained the lead ($31.3B vs $27B), and the lead now alternates by expiry cycle.
- US-hours IBIT flow, including systematic covered-call selling, pressures the BTC term structure. Include the IBIT-implied ATM vol (converted to BTC terms) as a lead-lag input to θ during US hours.

**Small desk:** yes, with daily recalibration.

### 9. Rough volatility for the short end

**Evidence:**

- Rough Bergomi (Bayer-Friz-Gatheral) calibrated to 30 Deribit surfaces (May 2022–Mar 2025) gives Hurst H ≈ 0.01–0.06 in most regimes. Calibration error scales with ATM IV (Pearson r = 0.89) (Caruso, arXiv 2608.27575).[41]
- An SSRN study finds H ≈ 0.07, and reports that rBergomi delta hedging outperforms Black-Scholes and Heston.[42]

**How to use it:** you do not need Monte Carlo rBergomi in the quoting loop. Use the implication that ATM skew scales like τ^{H−1/2}, i.e. nearly τ^{−0.45} to τ^{−0.5}. Use that as the prior for SSVI ρ·φ across daily and weekly expiries, which stabilises fits in illiquid front expiries.

Joint calibration à la quadratic rough Heston (SPX/VIX) maps to BTC options plus DVOL futures. It is research-grade and not a near-term P&L item.

### 10. Deep hedging, RL market making, LOB deep learning

- **Deep hedging.** Buehler, Gonon, Teichmann & Wood ("Deep hedging", Quantitative Finance 19(8), 2019) optimise convex risk measures under costs and outperform complete-market hedges in a Heston simulation.[43][44] The JPM team's later work (Deep Bellman hedging; "learning to remove the drift") addresses simulator drift.[45] The real cost is a credible market simulator for BTC; with a poor one, the network learns artefacts.
- **RL market makers.** Guéant & Manziuk applied deep RL to high-dimensional multi-asset market making, cited by BBG.[13]
- **DeepLOB-style models.** These need L3/L2 depth plus GPUs, and their alpha horizon is the one where you compete with HFTs.

Recommendation: defer all three until items 1–8 are live. The first ML step should be a gradient-boosted fill-toxicity classifier on your own markout data. It is cheaper than transformers and directly improves item 3.

## What the top firms publicly reveal (facts vs. speculation)

**Documented: SIG.** SIG says it uses poker "to teach new traders about decision making under uncertainty" and gives traders formal game-theory training.[46] Todd Simkin, longtime head of trader education, describes mock trading and poker as a "controlled model of asymmetric information."[47] The operating principles are:

- size bets relative to edge;[48]
- update on what the counterparty's action reveals;[48]
- judge decisions, not outcomes.[49]

In your terms: markouts by counterparty type (item 5) and quote-size as a function of edge/variance.

**Documented: Optiver.** Optiver's own Ready Trader Go competition charges a 2 bp taker fee and pays a 1 bp maker fee, and advises "never use a market order".[50] Its "Volatility views" notes show desk thinking in vol terms: Robby Knopp, co-head of the S&P options desk, said 2025 "saw longer-lasting moves with better liquidity, allowing hedges to actually realize P&L," in contrast to August 2024.[51] The Optiver/Kaggle realised-volatility competition (2021) signals that short-horizon RV forecasting is a core input. Optiver says its Amsterdam office alone has captured "1 PB of market data".[52]

**Documented: Jane Street (regulatory and court record).**

- SEBI's interim order of 3 Jul 2025 (WTM/AN/MRD/MRD-SEC-3/31516/2025-26):
  - Jane Street entities made total profit of ₹36,502.12 crore from 1 Jan 2023 to 31 Mar 2025, including ₹43,289.33 crore from index options, offset by losses in stock futures, index futures and cash.[53]
  - On 17 Jan 2024, Jane Street bought ₹4,370.03 crore of BANKNIFTY constituents and futures in the morning while holding "effectively INR 32,114.96 crores of bearish positions" in options, 7.3 times the size of the cash/futures longs. It then reversed the stock and futures position, losing ₹61.6 crore on that leg, on its most profitable day (₹734.93 crore).[53][54]
  - SEBI impounded ₹4,843.57 crore. These are prima facie allegations; Jane Street called its activity "basic index arbitrage trading".[55][56]
- Jane Street v. Millennium (SDNY 24-cv-2783):
  - The redacted amended complaint describes signals that "enabled Jane Street to reliably predict future market activity", refined with "algorithmic and machine learning techniques," and a team that grew from 5 to about 25.[57]
  - The $1B 2023 profit figure came out at a hearing, not in the complaint.[58]
  - The case settled on undisclosed terms in Dec 2024.[58]
- The lesson for you is structural, not tactical. Where option notional dwarfs the underlying's liquidity (SEBI: BANKNIFTY options delta-equivalent turnover was 353× constituent cash turnover that day), the underlying's settlement mechanics drive option P&L.[53] Deribit's index-averaged expiry is the crypto analogue to watch defensively. Do not try to replicate the strategy: it is the subject of manipulation proceedings.
- Jane Street's podcast shows only tooling culture: options "are weird because there are so many of them."[59]

**Documented: crypto-native firms.**

- Paradigm data shows the institutional flow is in blocks, mostly multi-leg.
- FalconX, after integrating Arbelos, is described as the largest options liquidity provider on Paradigm. One market maker, quoted unnamed in Quatravaux's essay "2025 was the biggest year ever for crypto options", reported $73B of bilateral OTC options plus $109B of Paradigm blocks in 2025.
- QCP, Wintermute and others publish desk commentary framing BTC through dealer gamma and strike clusters (e.g. "$60k put cluster on Deribit the next magnet").[60][61]
- No substantive public papers on options market making methods from GSR, Galaxy, Amber or Akuna were found.

**Speculation (labelled as such).** It is plausible, but not publicly documented, that the large crypto options market makers run vol-space surfaces with block-flow-aware updates and venue-specific MMP settings much like items 1–4. There is no public evidence of production RL quoting at these firms.

## Infrastructure: small desk vs HFT

**Small desk (Python → Rust, API access):**

- items 1–8;
- hftbacktest for underlying hedging simulation (queue position, feed and order latency; its authors advise trading tiny size live and calibrating the queue model to live fills);[62][63]
- tardis.dev data;
- py_vollib/QuantLib for checks (but write your own vectorised Black-76 for inverse options).

hftbacktest models L2 queue position probabilistically.[62] Thin options books make queue modelling less critical than latency modelling for options; perps are the reverse.

**Needs HFT infrastructure:** Starbase binary mass quoting at scale, Deribit LD4 colocation, multicast feeds, sub-millisecond cross-asset pulls, and DeepLOB-type models.

## Recommendations

- **Weeks 1–5:** build the SSVI surface plus vol-space quoting, and the inverse-greek library. Shadow-quote it against the Deribit mark and against your own fills.
- **Weeks 4–8:** port the A-S inventory term to bucketed vega/gamma (item 2). Enable MMP, mass quotes and cancel-on-disconnect (item 4). Add the Whalley-Wilmott hedge band (item 6).
- **Weeks 6–12:** make the Hawkes model cross-asset with block and DVOL marks (item 3), and run vega-weighted markouts by flow type (item 5). Only then widen quoting to more strikes and tenors.
- **Quarter 2:** add the regime overlays (VRP, expiry, weekend, IBIT hours), fee-cap routing to OKX, and the rough-vol term-structure prior. Port hot paths to Rust.
- Do not start with deep hedging, RL or transformers. Revisit them once you have at least six months of your own fill and markout data to train on.
- **Kill switch.** Combine an exchange MMP trigger, a software vega/gamma limit breach, a daily drawdown limit, and loss of the perp feed. Any one of them pulls all quotes.

## Caveats

- The fee tiers cited are base-tier and change often. Verify them on the venues' own fee pages before modelling.
- The 2026 market-share and block-flow figures (IBIT vs Deribit, the Binance-theft blocks) come largely from CoinDesk and aggregator re-reporting.
- The SEBI figures are interim, ex-parte allegations under appeal.[64]
- The vega-space skew formula in item 2 is a practical heuristic consistent with the BBG framework, not their exact numerical solution.
- Evidence on GEX price effects in BTC conflicts. Treat GEX as a conditioning variable only.
- The simulated $102/day Sharpe 2.27 result on BTC/USDT will not transfer to options as is. Options fills are rarer and fees are fixed per underlying notional. Rebuild the backtest with option-level fees, inverse settlement and latency before trusting any P&L projection.

## Sources

1. Arbitrage-free SVI volatility surfaces, Jim Gatheral, Antoine Jacquier
2. Options Market Making
3. Options Market Makers: A Comprehensive Guide to Liquidity Provision Mechanisms
4. Options Market Making: Quoting in Volatility, Hedging - Tradermath
5. Gatheral SSVI Volatility Surface Calculator
6. Arbitrage-free SVI volatility surfaces, Jim Gatheral, Antoine Jacquier
7. eSSVI Surface Calibration
8. Robust calibration and arbitrage-free interpolation of SSVI slices
9. Pricing and Calibration of Bitcoin Inverse Options via the Rough Bergomi Model
10. Bitcoin Options Exchanges: Deribit vs CME vs Binance Compared
11. [PDF] Algorithmic market making: the case of equity derivatives
12. Algorithmic market making for options
13. Algorithmic market making for options
14. Algorithmic market making for options, Bastien Baldacci, Philippe Bergault
15. Sasha Stoikov - Google Scholar
16. An approximate solution for options market-making in high dimension
17. Fragmentation, Price Formation, and Cross-Impact in Bitcoin Markets
18. The Micro-Price: A High Frequency Estimator of Future Prices
19. Net Buying Pressure and the Information in Bitcoin Option Trades
20. (PDF) Net Buying Pressure and the Information in Bitcoin Option Trades
21. Do hourly Bitcoin options forecast risk beyond a HAR benchmark? - ScienceDirect
22. Paradigm Monthly
23. private/set_mmp_config
24. Deribit MMP
25. Deribit Mass Quotes Specifications - Low-Latency Order Placement
26. Mass Quote(i) — Production FIX API - Deribit API Documentation
27. Deribit Starbase API Overview - High-Performance Binary Protocol
28. Optimal Hedging of Options with Transaction Costs, Valeri I. Zakamouline
29. (PDF) A Review and Implementation of Option Replication in Discrete Time with Transaction Costs
30. The best hedging strategy, Elizabeth Whalley and Paul Wilmott
31. Bridging Stochastic Control and Deep Hedging: Structural Priors for No-Transaction Band Networks
32. Bridging Stochastic Control and Deep Hedging: Structural Priors
33. No-Transaction Band Network: A Neural Network Architecture for Efficient Deep Hedging
34. Deribit Review 2026: Fees, Safety
35. GitHub - bzml995/okx-options-fees: OKX Options Fees Explained
36. The Bitcoin VIX and Its Variance Risk Premium
37. The Bitcoin VIX and its Variance Risk Premium, Carol Alexander, Arben Imeraj (SSRN)
38. Risk Premia in the Bitcoin Market, Caio Almeida, Maria Grith, Ratmir Miftachov
39. Option-Implied Risk Premia and Cryptocurrency Market Regimes, Maria Grith, Caio Almeida, Ratmir Miftachov, Zijin Wang (SSRN)
40. Do Gamma Walls Actually Move Bitcoin Prices at Deribit?, Pawel Lachowicz (SSRN)
41. [2608.27575] Pricing and Calibration of Bitcoin Inverse Options via the Rough Bergomi Model
42. Empirical Evidence of Rough Volatility in Bitcoin Options: A Rough Bergomi Approach, 寸杰 何 (SSRN)
43. Deep Hedging, Hans Buehler, Lukas Gonon, Josef Teichmann, Ben Wood (SSRN)
44. Deep Hedging with Market Impact
45. Deep Bellman Hedging
46. Susquehanna Interview Questions, Process and Assessment
47. Ethan Kho on X: "Inside Susquehanna International Group with Todd Simkin"
48. Market Making Games in Quant Interviews: How to Prepare
49. How to Get a Job at SIG / Susquehanna International Group
50. How to play - Ready Trader Go
51. Volatility views: Investors are rethinking longer-dated volatility
52. Optiver Launches First Data Science Competition on Realized Volatility With $100k Prize Money for Best Models
53. https://nsearchives.nseindia.com/content/circulars/INVG68921.pdf
54. Jane Street & SEBI: Formulating A Structured Approach To Regulatory Governance of Financial Markets
55. India's ban on Jane Street raises concerns over regulator role
56. SEBI UPDATE
57. https://www.sewkis.com/wp-content/uploads/Jane-Street_amended-complaint.pdf
58. The Biggest Lawsuits in Quant Finance History, From Jane Street to Renaissance Technologies
59. Signals and Threads
60. Bitcoin Volatility Near 2-Year Low Before Fed and $12B Options Expiry
61. Bitcoin could slide into the $50,000s as Fed rate hike bets build, Wintermute says
62. GitHub - mirkovicdev/HFTENGINE: Replay console for hftbacktest market-making backtests
63. Backtesting that accounts for feed latency, order latency, and queue position
64. The Jane Street-SEBI Dispute: A Legal Dissection Of Market Manipulation, Interim Disgorgement And The Right To Disclosure In The Indian Capital Markets » Lawful Legal
