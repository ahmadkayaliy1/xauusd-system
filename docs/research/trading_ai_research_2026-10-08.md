# Research brief: discretionary trading process, gold intraday, and LLM decision systems (2026-10-08)

Purpose: evidence base for improving the XAUUSD LLM desk (Sonnet 5 via CLI, ~28k-token JSON + 6 charts, deterministic risk gate).
Method: web research on 2026-10-08. For most academic sources I read the abstract or a summary page, not the full text; numbers are as stated there. One source (an SSRN preprint on ICT/SMC) could not be opened and is reported second-hand (marked).
Evidence grades: **strong** = replicated or peer-reviewed with clear design, official documentation, or plain mathematics; **moderate** = single peer-reviewed study, credible preprint, vendor-measured result; **weak** = single preprint, news, blog, short or unvalidated backtest; **opinion** = practitioner judgement or book.

## 0. The ten findings that matter most

1. **Your evaluation, not your prompt, is the weakest link.** At ~1.5R per-trade standard deviation, 100 trades give a standard error of ~0.15R, so 79 stored moments cannot rank models or prompts (arithmetic in section 4). Run-to-run agreement of 65 % means an A/A test (same config twice) is mandatory.
2. **Leakage is likely in model comparisons.** Official docs list Sonnet 5's knowledge cutoff as Jan 2026 and Sonnet 5.5 / Opus 5.5 / Fable 5.1 as Jun 2026 ([Sonnet 5](https://platform.claude.com/docs/en/models/sonnet-5/overview), [overview](https://platform.claude.com/docs/en/models/overview)) (strong). Any stored moment before those dates may be partly memorised; models with different cutoffs are not comparable on the same old window. Published LLM trading agents lost 50-72 % of return after the cutoff ([Profit Mirage](https://arxiv.org/html/2510.07920v1)) (moderate).
3. **LLM trading edges shrink under longer, broader tests** ([FINSABER](https://arxiv.org/abs/2505.07078), [StockBench](https://arxiv.org/abs/2510.02209)) (moderate). Treat every backtest gain as a hypothesis.
4. **Voting beats debate.** Majority voting explains most of multi-agent debate's gains ([Debate or Vote, NeurIPS 2025](https://arxiv.org/abs/2508.17536)) (moderate-strong, NLP tasks). Your planned 2-of-3 vote is the evidence-backed direction; bull/bear debate agents are not.
5. **Verbalized confidence is overconfident; agreement across samples is the better confidence signal** ([Xiong et al., ICLR 2024](https://arxiv.org/abs/2306.13063)) (moderate-strong).
6. **Gold: macro releases move price fast; stop clusters at round numbers are documented; SMC order-block/FVG claims have no peer-reviewed support** (details sections 2-3).
7. **Technical-rule edges decay and vanish after costs; trend-following evidence is at daily-monthly horizons**, not 5-minute ones (section 3).
8. **15 % risk per trade exceeds Kelly unless the edge is large** and unproven edge argues for much less (section 1). Risk settings are user-owned; this is information, not a change request.
9. **Sonnet 5 follows prompts literally.** Vague words ("be conservative", "only high-conviction") get over-applied; concrete thresholds work better ([Sonnet 5 guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5)) (strong, official).
10. **Self-review from single trades is a noise amplifier.** LLMs do not reliably self-correct without external feedback ([Huang et al.](https://arxiv.org/abs/2310.01798)), and one trade is weak feedback. Keep the journal factual and process-focused.

---

## 1. Professional discretionary trading process

- **Think in R and expectancy, not win rate.** Expectancy = average R per trade; a 40 % win rate at 2R payoff is profitable, a 60 % rate at <1R is not ([Tharp, *Trade Your Way to Financial Freedom*](https://www.mheducation.com/highered/mhp/product/financial-freedom-super-tradermktgprg.html)) (opinion for the framing; the maths is strong). Report every A/B in R with a confidence interval.
- **Sizing.** Kelly fraction for win prob p and payoff b: f* = p - (1-p)/b ([Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion)) (strong, mathematics). Betting more than ~2x Kelly lowers long-run growth; practitioners use half or quarter Kelly because p is estimated with error. Example (my arithmetic): 40 % wins at 2R gives f* = 10 %; at a true 35 % it is 2.5 %. A 15 % risk is 1.5x-6x Kelly. Five consecutive losers at 15 % cost 55.6 % of equity, and with a 40 % win rate several such runs are expected in 100 trades.
- **Hard loss limits are industry norm.** A well-known prop firm uses 5 % daily and 10 % total loss limits, counting floating P&L ([FTMO rules summary](https://tradingfunder.com/ftmo-rules/)) (weak-moderate: secondary summary of commercial rules). Your 30 % daily limit is 6x that norm; the deterministic gate is the right place for it.
- **Most active retail traders lose; overtrading is a documented cause.** Of Brazilian day traders who persisted 300+ days, 97 % lost money ([Chague et al., "Day Trading for a Living?"](https://ssrn.com/abstract=3423101), summarised [here](https://www.financemagnates.com/forex/analysis/new-study-shows-just-how-unprofitable-day-trading-is/)); in Taiwan, aggregate day-trader performance was negative and learning was poor ([Barber, Lee, Liu, Odean](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/day%20trading%20and%20learning%20110217.pdf)); the most active US households earned 11.4 % a year versus 17.9 % for the market ([Barber and Odean 2000](https://ideas.repec.org/a/bla/jfinan/v55y2000i2p773-806.html)) (strong, equities). Implication: selectivity and cost control beat activity; 30-40 calls/day is fine only if few become trades.
- **Journaling and review.** Writing and reflecting on what was learned lifted test scores 23 % in a field study ([Di Stefano, Gino, Pisano, Staats](https://www.hec.edu/en/knowledge/instants/power-reflection-work-insight-hec-professor-giada-di-stefano)) (moderate, not trading). Steenbarger's trader-coaching view: review process and what worked, not only mistakes, and extract a lesson to apply next time ([BabyPips summary](https://www.babypips.com/trading/make-the-most-out-of-your-trading-journal); book: [*Enhancing Trader Performance*](https://www.goodreads.com/book/show/701037.Enhancing_Trader_Performance)) (opinion).
- **Checklists** cut surgical deaths and complications by more than a third in a multi-site study ([WHO checklist, NEJM 2009](https://news.harvard.edu/gazette/story/2009/01/surgical-safety-checklist-drops-deaths-and-complications-by-more-than-one-third/)) (strong in surgery; transfer to trading is opinion). Short pass/fail checklists at decision points suit an LLM too.
- **How desks judge traders:** risk-adjusted return, drawdown discipline, consistency, rule adherence more than raw P&L ([overview](https://ecommercefastlane.com/why-modern-prop-firms-use-structured-assessment-models/)) (weak, industry marketing).

**Actionable principles:** (a) measure in R, with intervals; (b) a trade must have a named invalidation level; (c) default is no trade; (d) cap new trades per day/regime; (e) review process quality (was the plan followed, was the stop at invalidation) separately from outcome; (f) never change rules from one or two trades.

---

## 2. Gold-specific intraday knowledge

| Topic | What the evidence says | Grade |
|---|---|---|
| Macro releases | 8:30 ET US releases (payrolls, durable goods) had the largest effects on gold/silver/copper futures; reaction is swift; positive US surprises lower gold ([Elder, Miao, Ramchander 2012](https://mountainscholar.org/handle/10217/206884)). In FX, bad news moves price more than good news ([Andersen et al. 2003](https://scholars.duke.edu/publication/712364)). | strong (2002-08 data) |
| Why the blackout is sound | Volume and volatility jump after releases (same Elder paper); spreads widen (microstructure norm). Whether to trade the post-release move is untested here. | moderate |
| Drivers | Real rates and gold had a -0.82 correlation 1997-2012 ([Erb and Harvey](https://www.nber.org/papers/w18706)), but the link has decoupled since late 2022 with central-bank buying ([T. Rowe Price](https://troweprice.com/en/us/insights/what-is-driving-gold-prices-to-all-time-record-highs)). The WGC attribution model uses four themes: economic expansion, risk/uncertainty, opportunity cost, momentum (incl. ETF flows) at monthly frequency ([WGC](https://www.gold.org/goldhub/gold-focus/2023/07/the-drivers-behind-golds-june-and-h1-performance)). | moderate; no intraday evidence |
| Session structure | Gold volume is n-shaped, peaking 11:00-17:00 GMT when Europe and the US overlap; modest volatility rise ~12-14 GMT ([Batten et al. 2017](https://pmc.ncbi.nlm.nih.gov/articles/PMC5407636/)). Informed trading dominates New York, uninformed trading Tokyo ([Iwatsubo et al.](https://www.rieti.go.jp/en/publications/summary/17110007.html)). FX has strong intraday periodicity ([Andersen and Bollerslev 1998](https://projects.nber.org/papers/w5783)). | moderate |
| Overnight vs day | Gold earned positive overnight and negative day-session returns in a multi-instrument study ([Blose et al. 2018](https://ideas.repec.org/a/spr/jecfin/v42y2018i3d10.1007_s12197-017-9403-0.html)). Possibly arbitraged since; for a 24 h CFD the "open" is ambiguous. | moderate-weak |
| London fix | PM-fix trading leaked information before publication ([Caminschi and Heaney 2014](https://ideas.repec.org/a/wly/jfutmk/v34y2014i11p1003-1039.html)); the fixings were later replaced by electronic auctions ([UWA](https://www.news.uwa.edu.au/201410067021/research/uwa-research-finds-gold-and-silver-price-setting-open-exploitation)). Old fix-time patterns should not be assumed to persist. | moderate |
| Stop runs / round numbers | Stop-loss orders cluster at round numbers; trends accelerate through such clusters, and take-profit orders cluster at round numbers and cause reversals ([Osler, NY Fed](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr150.pdf)). FX evidence; gold is plausible but not shown. This is the only academic support for "liquidity pool" thinking. | moderate |
| Asia range / London sweep base rates | No peer-reviewed study found. Treat your own base rates as unvalidated; check stability out-of-sample on a rolling basis. | weak/none |
| COT, ETF flows, GVZ | Managed-money positions followed returns rather than driving gold in one study ([thesis](https://theses.lib.polyu.edu.hk/handle/200/7971?mode=full)); no intraday predictive evidence found for COT, ETF flows or GVZ. Volatility clustering in gold is robust (GARCH literature, [example](https://doaj.org/article/f2f793112b9a4a4ab2a582d615af1543)), so GVZ/ATR is a regime variable, not a direction signal. | weak (direction), strong (vol clustering) |
| Intraday momentum | First-half-hour return predicts last-half-hour return in US ETFs, stronger on volatile and news days ([Gao et al. 2018](https://profiles.wustl.edu/en/publications/market-intraday-momentum/)) (strong, equities); same sign in four Chinese commodity futures incl. copper ([Jin et al. 2020](https://ideas.repec.org/a/wly/jfutmk/v40y2020i4p632-650.html)); weak or absent in much of Asia-Pacific ([Limkriangkrai et al. 2023](https://ideas.repec.org/a/eee/pacfin/v80y2023ics0927538x2300152x.html)); in RUB-USD FX it reflects liquidity providers' overnight risk aversion ([Elaut et al. 2018](https://ideas.repec.org:443/a/eee/finmar/v37y2018icp35-51.html)). That mechanism needs a market close; spot gold CFD has none. | moderate; weak transfer to XAUUSD |
| Technical rules on intraday gold | Standard moving-average parameters had no predictive power; some longer parameter sets did in gold but not silver, after a parameter search ([Batten et al. 2018](https://ideas.repec.org/a/eee/intfin/v52y2018icp102-113.html)). | moderate-weak (data-mined) |

---

## 3. Strategy families: what has evidence, what is folklore

- **Time-series (trend) momentum:** positive for 1-12 months across 58 futures ([Moskowitz, Ooi, Pedersen](https://papers.ssrn.com/abstract=2089463)); robust over 1880-2016 across 67 markets, with a diversified Sharpe of roughly 0.76 after costs and fees as summarised ([Hurst, Ooi, Pedersen](https://www.TrendFollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf)) (strong). Horizon is days-months. Your trend-month versus chop-month pattern matches the known weakness: trend followers give back in ranges.
- **Technical rules in general:** 58 of 92 modern studies positive, but with data-snooping and cost problems ([Park and Irwin](https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html)); FX rule profits were genuine in the 1970s-80s and gone for common rules by the early 1990s ([Neely, Weller, Ulrich](https://files.stlouisfed.org/files/htdocs/wp/2006/2006-046.pdf)); on 1897-2011 Dow data, an investor could not pick the future winners ex ante and costs wiped out in-sample gains ([Bajgrowicz and Scaillet](https://ideas.repec.org:443/a/eee/jfinec/v106y2012i3p473-491.html)) (strong). Lesson: edges are small, unstable, cost-sensitive.
- **Opening-range breakout:** one paper reports a 2.4 Sharpe on US stocks "in play" ([Zarattini et al., SSRN 4729284](https://ssrn.com/abstract=4729284)); an independent replication on five indices found gross results reproduced and net of costs indistinguishable from zero ([mql5 blog](https://www.mql5.com/en/blogs/post/776235)) (weak overall; not gold). Your session-range features are a cousin of this idea; test, don't assume.
- **Range fade / mean reversion:** no robust peer-reviewed evidence found for intraday gold. The Osler take-profit result gives a mechanism at round numbers only (moderate, FX).
- **Volatility regime filters and sizing:** scaling exposure inversely to recent variance produced large alphas and substantially higher Sharpe ratios for equity factors and the currency carry trade ([Moreira and Muir](https://www.nber.org/papers/22208)) (strong, monthly, equities/FX carry). Plausible for gold risk sizing, untested intraday.
- **News-driven:** news moves gold (section 2) (strong); whether a retail-spread strategy can profit from the move is open (weak).
- **SMC / ICT:** I found no peer-reviewed test of order blocks, fair value gaps, BOS/CHoCH or liquidity sweeps. One unreviewed 2026 SSRN preprint (Mahadzva, "Smart Money or Costly Folklore?") is reported to code six ICT/SMC rules and test them with walk-forward, bootstrap and permutation on four FX pairs, finding no evidence they work as taught and that swept liquidity predicted continuation, not reversal ([second-hand summary](https://thortradecopier.com/blog/does-ict-smart-money-concepts-work); I could not open [SSRN 7430998](https://doi.org/10.2139/ssrn.7430998)) (weak). A commercial-blog simulation found 83.4 % of bullish FVGs were "filled" within 20 bars versus 83.5 % of random levels at the same distance, i.e. the base rate explains the folklore (weak, but logically sound: always compare with a random-level baseline). Honest position: the concepts are a vocabulary for levels and stops; Osler supports level clustering, nothing supports the specific rules. Your own ablation is the only real evidence.

---

## 4. Integrating LLMs into trading decisions

**Architecture**
- **Single agent plus code gates is the defensible default.** Anthropic recommends the simplest workable pattern, naming parallel voting for higher-confidence results and human checkpoints/guardrails ([Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)) (moderate, vendor guidance).
- **Multi-agent frameworks have thin evidence.** TradingAgents reports Sharpe 5.6-8.2 on three stocks over 3 months (Jan-Mar 2024) without significance testing and with 11 LLM calls plus 20+ tool calls per prediction ([paper](https://arxiv.org/html/2412.20138v4)) (weak). Debate adds little over voting ([Debate or Vote](https://arxiv.org/abs/2508.17536)) (moderate-strong).
- **Self-consistency:** sampling several reasoning paths and taking the majority lifted GSM8K by 10.6 points ([Wang et al.](https://arxiv.org/pdf/2203.11171v2)) (strong, math/commonsense; not shown for trading). Arithmetic: if each call picks the right side with probability 0.55 independently, a 2-of-3 majority is right 57.5 % (3p^2 - 2p^3); errors from the same model are correlated, so real gains are smaller. The larger benefit is as a filter: disagreement flags low-confidence moments, and agreement beats verbalized confidence ([Xiong et al.](https://arxiv.org/abs/2306.13063)). Anthropic itself lists best-of-N consistency checks as a hallucination control ([docs](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)) (moderate).
- **Reflection/memory:** Reflexion improved agents by 22 points (AlfWorld, 12 trials) ([Shinn et al.](https://neurips.cc/virtual/2023/poster/70114)) (strong, repeatable tasks with clear success signals). FinMem/FinAgent report gains in backtests ([FinMem](https://arxiv.org/abs/2311.13743), [FinAgent](https://arxiv.org/abs/2402.18485)) but were among the agents whose returns fell 50-72 % post-cutoff in Profit Mirage (moderate). Without external feedback LLMs often fail to self-correct and can get worse ([Huang et al.](https://arxiv.org/abs/2310.01798)); models also make more mistakes when earlier errors sit in their context ([Illusion of Diminishing Returns](https://arxiv.org/pdf/2509.09677)) (moderate). Implication: a journal that shows the model its own past calls and "what price did after" can anchor and propagate errors; trading outcomes are noisy and non-stationary, unlike Reflexion's tasks (opinion).
- **Live results are sobering but tiny.** In Alpha Arena Season 1 (6 models, ~16 days, real crypto), four of six lost money; Claude Sonnet 4.5 ended -42 %, Qwen3-Max +22 % ([report](https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/)) (weak: news, tiny sample, mostly variance). Use it only as an argument for deterministic risk limits.

**Evaluation**
- **Leakage:** returns collapse beyond the knowledge cutoff, and models answer 85-93 % of historical financial questions from memory ([Profit Mirage](https://arxiv.org/html/2510.07920v1)); in-sample, anonymised headlines predicted returns better with GPT sentiment, which the authors attribute mostly to a "distraction" effect rather than look-ahead ([Glasserman and Lin](https://arxiv.org/abs/2309.17322)); a token-probability test (Lookahead Propensity) flags memorised prompts ([paper](https://arxiv.org/html/2512.23847v1)) (moderate). Gold's 2025-26 path is likely in training data, so prices and dates in old payloads are identifying. Mitigations: test only on moments after the model's cutoff; for older moments, normalise prices (e.g. as ATR multiples from a reference), strip dates and event names, and compare to a post-cutoff slice.
- **Statistics:** backtests need correction for the number of configurations tried ([Deflated Sharpe](https://papers.ssrn.com/abstract=2460551), [PBO](https://papers.ssrn.com/abstract=2326253), [minimum backtest length](https://www.davidhbailey.com/dhbpapers/overfit-tools-at.pdf)); new discoveries should clear t > 3 ([Harvey, Liu, Zhu](https://www.nber.org/papers/w20592)) (strong). Power (my arithmetic, standard formula): with per-trade SD ~1.5R, detecting a 0.2R expectancy difference with 80 % power needs ~880 trades per independent arm, or ~440 pairs if both arms are run on the same moments with outcome correlation 0.5. 79 moments is about one fifth of that, and every extra prompt variant tried raises the false-discovery risk.
- **Noise floor:** even at temperature 0 one open model gave 80 distinct outputs in 1,000 identical runs because of batch-dependent kernels ([Thinking Machines](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/)) (moderate). Sonnet 5 rejects non-default temperature/top_p/top_k with a 400 error ([docs](https://platform.claude.com/docs/en/models/sonnet-5/overview)), so you cannot turn the noise down; you can only average it (voting) and measure it (A/A).

**Known LLM weaknesses relevant here**
- Irrelevant context lowers accuracy; explicit "ignore irrelevant information" and self-consistency help ([Shi et al., ICML 2023](https://arxiv.org/abs/2302.00093)) (strong).
- Pretrained LLMs add nothing over simple models on numeric time-series forecasting ([Tan et al., NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/hash/6ed5bf446f59e2c6646d23058c86424b-Abstract.html)) (moderate); the Alpha Arena organiser also said LLMs handle numeric series poorly (weak). Keep arithmetic (RR, sizing, distances) in code.
- Cognitive-bias effects such as anchoring appear in LLM decisions and can be reduced by prompt-level debiasing ([Echterhoff et al., EMNLP 2024](https://arxiv.org/abs/2403.00811v3)) (moderate). Your "history of recent decisions" block is an anchoring risk.
- Formatting changes alone move results: up to 40 % on one task for GPT-3.5, less for stronger models ([He et al.](https://arxiv.org/abs/2411.10541)); large spreads from spurious format features ([Sclar et al.](https://arxiv.org/abs/2310.11324)) (moderate). Re-test after any payload reformat.

**Cost/latency:** three parallel calls keep wall-clock near 50 s but triple quota. Cache reads cost 10 % of input price (5 % on Sonnet 5.5) ([models overview](https://platform.claude.com/docs/en/models/overview)), so keep the static prefix (system prompt, brief, legend) byte-identical and first (official; applies to API, check if your CLI path caches).

---

## 5. What data to give the LLM and how

- **Position and length.** Accuracy is highest when relevant facts are at the start or end of a long context and drops in the middle ([Lost in the Middle](https://arxiv.org/abs/2307.03172)) (strong); degradation grows with input length even on simple tasks across 18 models ([Chroma, Context Rot](https://trychroma.com/research/context-rot)) (moderate, vendor report). 28k tokens is small versus the 1M window, so the issue is attention and distraction, not capacity. No published optimum exists for trading payloads: ablate.
- **Anthropic guidance:** put long data first and the question/instructions last (up to ~30 % better in their tests), wrap each document in tags with source metadata ([best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)) (moderate, vendor-measured). Place images before text and label them ("Image 1: 4h") ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)).
- **JSON vs prose vs tables:** no format wins universally; stronger models vary less ([He et al.](https://arxiv.org/abs/2411.10541)). Claude is trained on XML-tagged documents. Pre-computed features beat raw candles for numeric reasoning (Tan et al., above), but give the last ~20-30 raw candles of the trigger timeframe so the model can check the features.
- **Chart images:** on non-financial tasks, plots beat raw numbers up to 120-150 % and cut API cost up to 90 % ([Google, Plots Unlock](https://arxiv.org/abs/2410.02637)) (moderate). For candlesticks specifically, a 2026 benchmark found VLMs do well only in persistent up/down trends, weakly in sideways or volatile markets, with no consistent advantage over a feature baseline ([Do VLMs Truly Read Candlesticks?](https://arxiv.org/abs/2604.12659)) (weak-moderate, single preprint). Anthropic reports that giving the model a crop/zoom tool reads dense charts more accurately than raising effort ([Sonnet 5.5 guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5)) (moderate, vendor). Token cost: ceil(w/28) x ceil(h/28) per image, capped at 1,568 tokens (standard tier) or 4,784 (high-resolution tier, Claude 4.7+) ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)), so six charts cost ~9k-29k tokens, i.e. comparable to the whole JSON. Whether they add value over the JSON is an empirical question for your ablation.
- **Ordering/summarising (opinion, consistent with the above):** lead with the decision-relevant items (price vs nearest levels, news clock, open position, account), then structure/zones, then indicators, then history; cap zone lists to the nearest few per side; drop fields the model never cites (check its notes).

---

## 6. Prompt engineering for high-stakes decisions (Anthropic guidance, applied)

Source for all bullets unless noted: [Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices), [Sonnet 5 guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5), [Sonnet 5.5 guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5) (official; effect sizes are vendor-measured).

- **Layout:** stable system prompt (persona, rules, legend, desk brief) -> `<documents>` with the payload (each `<document index source>`; images first) -> short `<task>` at the end (decision procedure, schema, the one question). Keep static text first so it caches.
- **Be literal and concrete (Sonnet 5):** it does not infer unstated scope and follows qualitative bars strictly; a review harness told to report only "important" issues lost recall though the model found the bugs. Replace "high-conviction only" with a numeric bar: e.g. "A-grade = entry within 0.5 ATR of a named level, stop beyond the invalidation structure, RR >= 2 after spread, no high-impact news within 60 min, 1h and 4h bias aligned or a stated counter-trend reason." Apply the "coverage versus filtering" idea: let the model list candidate setups with per-criterion pass/fail and a score; let code and the vote do the filtering.
- **Give reasons, tell it what to do:** explain why each rule exists (it generalises from motive) and prefer positive instructions to prohibitions. Remove duplicated or conflicting rules between system prompt and brief; scattered "NEVER/MUST" lines over-constrain.
- **Permit abstention:** "NO_TRADE is a normal and frequent answer; say what is missing" (Anthropic: allowing "I don't know" reduces fabricated output, [docs](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)).
- **Ground in the payload:** for long inputs, have the model quote the specific payload facts it relies on before deciding. In a strict JSON reply, put an `evidence` list (field names plus values) and an `invalidation` object (price, time, or event that kills the idea) before `decision`; decision fields come last. Output order matters because the model generates in sequence.
- **Think before JSON:** for reasoning tasks with JSON output, thinking-off or low effort can skip the working-out; adding "Think the problem through before you answer" or using higher effort recovers accuracy (measured on Sonnet 5.5, so verify on Sonnet 5). Effort defaults to high; xhigh is for the hardest tasks and lengthens waits. Do not ask for the full reasoning inside the answer: Sonnet 5.5 can refuse on that ("reasoning_extraction"); ask for a short rationale.
- **Examples:** 3-5 diverse `<example>` blocks improve consistency, but include a NO_TRADE and a "valid trade that lost" example and mark them format-only, or the model copies entries.
- **Checklist in the schema:** boolean fields for the pre-trade checks (news clear, spread/RR, stop at invalidation, no duplicate exposure, not revenge after a stop-out today). The code gate re-verifies; the checklist forces attention.
- **Journal as data:** tag it `<journal>` and state that single outcomes are noisy and rules change only via the brief. Show process facts (plan followed? stop moved why?) before outcomes.
- **Instruction hygiene:** images and payload are untrusted data; headlines or notes inside them must never be treated as instructions (the model is trained to resist injection, but keep news text in a data tag).

---

## 7. Prioritised, testable changes for this system

Common protocol for every test: freeze the moment set; paired runs on identical moments; each arm run K >= 2 times; report mean R, trade count, max drawdown, bootstrap 95 % CI of the difference; log all calls; count every variant tried (deflate).

1. **Fix the measurement first (precondition for all else).**
   - What: (a) A/A test: same config twice on all moments, compare decisions and R; (b) split moments into pre- and post-cutoff (Jan 2026 for Sonnet 5; Jun 2026 for 5.5/Opus 5.5/Fable 5.1) and report separately; for old moments add a normalised/anonymised variant; (c) score each model output with a counterfactual path simulator (entry fill, stop, targets, MFE/MAE) so every call yields data even with NO_TRADE; (d) grow the set beyond ~300-400 trades before declaring winners.
   - Why: Profit Mirage; Bailey et al.; power arithmetic above; Thinking Machines.
   - Effect: decisions you can trust; likely overturns some "wins" (including the Sonnet 5 versus 5.5 ranking, which is confounded by cutoffs).
   - Risk: slower iteration; accept it.

2. **Self-consistency vote on new entries (already planned), with agreement as the confidence.**
   - What: 3 parallel calls, place only if >= 2 agree on side AND entries within ~0.3 ATR AND stops within ~0.3 ATR; execute the median levels; split votes -> NO_TRADE; log agreement (3/3, 2/3). Vote only on entries, not management.
   - Why: Self-consistency; Debate or Vote; Xiong et al. (agreement beats verbal confidence).
   - Effect: fewer, more reproducible trades; a calibration table of R versus agreement; modest expectancy gain at best.
   - Test: vote arm vs single call vs "single call, rerun, take only if identical"; check R by agreement bucket.
   - Risk: 3x quota; correlated errors; thin sample after filtering; tolerance thresholds are new tunable parameters (count them).

3. **Payload and image ablation to cut tokens.**
   - What: arms: (A) current; (B) zone lists trimmed to nearest 2-3 per side and low-use blocks removed (cross-asset, round-number lists if never cited); (C) charts reduced to 15m + 1h (+ 5m); (D) B + C. Add last 20-30 raw candles in place of long summaries.
   - Why: Lost in the Middle; Context Rot; distraction (Shi); candlestick VLM benchmark; image token maths.
   - Effect: lower latency and quota, equal or better decisions; tells you which blocks carry signal.
   - Test: same moments; also count how often the model's notes cite each block.
   - Risk: removing context the desk brief relies on; evidence here is generic, not trading-specific.

4. **Prompt restructure per Anthropic guidance.**
   - What: stable prefix first; payload in tagged documents; task last; evidence-first JSON (`evidence`, `invalidation`, `checks`, then `decision`); numeric A-grade bar; explicit NO_TRADE permission; 3-5 examples including NO_TRADE. Optionally test higher effort for new-entry calls only.
   - Why: Anthropic long-context and Sonnet 5 literalism guidance; hallucination guide; JSON-reasoning note.
   - Effect: fewer low-quality entries, more reproducible output (agreement above 65 %).
   - Test: A/B and agreement rate; latency.
   - Risk: pure prompt effects are noisy and format-sensitive (He, Sclar); examples can anchor.

5. **Deterministic regime gating of style and size.**
   - What: compute ATR percentile, ADX, session and news clock in code; define 3 regimes (trend / range / event) that change allowed setup types, minimum RR, and size multiplier (e.g. reduce size when ATR percentile is high or within N minutes after a release); show the regime to the model as a fact, not a suggestion.
   - Why: Moreira and Muir (vol-managed sizing); gold volatility clustering; FINSABER (regime-aware controls); your own trend/chop results.
   - Effect: smaller losses in chop, fewer low-edge entries.
   - Test: regime gate on/off, same moments; limit to 2-3 parameters.
   - Risk: overfitting regimes to the same months; evidence is monthly equity factors, not intraday gold.

6. **Regime-conditioned management done in code, with the model's discretion limited.**
   - What: trend regime -> hold to structure/trail; chop regime -> break-even at 1R plus time stop. Compare with model-managed and fixed 2R on the same entries.
   - Why: your measured asymmetry (early stop moves cut trend winners but helped in chop); trend-following literature says to let winners run, at longer horizons.
   - Effect: capture the better of both; reduces dependence on LLM noise in management.
   - Test: three-arm A/B on identical entries across trend and chop months.
   - Risk: regime misclassification; extra parameters.

7. **Journal and self-review hygiene.**
   - What: keep the 24 h journal factual (entry, stop, why, exit, R); put outcome after-the-fact ("what price did after") in a separate clearly-labelled block or remove it; limit lessons to <= 3 standing items, each requiring >= N supporting trades to enter the brief and expiring after M days; score self-review on process tags, not P&L.
   - Why: Huang et al.; self-conditioning; anchoring (Echterhoff); Di Stefano (reflection helps humans); Reflexion needs repeatable tasks.
   - Effect: less overreaction after a stop-out; fewer revenge or avoidance biases.
   - Test: your queued journal on/off, plus lesson-expiry arm.
   - Risk: removing real benefit; effect likely small and hard to detect (power).

8. **Caching and call budget.**
   - What: byte-identical static prefix; measure cache hits if the CLI path supports them; use a lower effort for routine management/no-trade checks and higher for new entries.
   - Why: official cache pricing; effort guidance.
   - Effect: lower quota burn to pay for the 3x vote.
   - Risk: effort changes can invalidate cache; verify behaviour.

9. **(User decision, not an agent change) risk fraction.**
   - Information only: until a post-cutoff, multi-hundred-trade interval excludes zero expectancy, Kelly arithmetic argues for far smaller per-trade risk, and scaling risk by vote agreement (3/3 full, 2/3 half) is a testable middle path.

### What NOT to do
- Do not add bull/bear debate or more agents; evidence favours voting, and cost rises (TradingAgents used 11 LLM + 20 tool calls per prediction).
- Do not use the model's stated confidence for sizing or filtering.
- Do not pick a model, prompt, or window from 79 moments, or from a window inside a model's training period; do not tune and evaluate on the same moments.
- Do not add more indicators or SMC variants without a random-level baseline (FVG example) and an ablation.
- Do not hard-code "real yields up means gold down" (decoupled since 2022) or rely on London-fix timing patterns.
- Do not let the model compute RR, lot size, or distances; keep that in the gate.
- Do not feed ever more history of the model's own decisions; cap it (anchoring, self-conditioning).
- Do not try temperature tuning on Sonnet 5 (rejected with 400); do not read a short live sample (like Alpha Arena) as evidence for or against any model.

---

## Sources

Trading process
- Tharp: https://www.mheducation.com/highered/mhp/product/financial-freedom-super-tradermktgprg.html
- Kelly criterion: https://en.wikipedia.org/wiki/Kelly_criterion
- FTMO rules summary: https://tradingfunder.com/ftmo-rules/
- Chague et al. Brazil day traders: https://ssrn.com/abstract=3423101 ; summary https://www.financemagnates.com/forex/analysis/new-study-shows-just-how-unprofitable-day-trading-is/
- Barber, Lee, Liu, Odean: https://faculty.haas.berkeley.edu/odean/papers/day%20traders/day%20trading%20and%20learning%20110217.pdf
- Barber and Odean 2000: https://ideas.repec.org/a/bla/jfinan/v55y2000i2p773-806.html
- Di Stefano et al.: https://www.hec.edu/en/knowledge/instants/power-reflection-work-insight-hec-professor-giada-di-stefano
- Steenbarger: https://www.babypips.com/trading/make-the-most-out-of-your-trading-journal ; https://www.goodreads.com/book/show/701037.Enhancing_Trader_Performance
- WHO surgical checklist: https://news.harvard.edu/gazette/story/2009/01/surgical-safety-checklist-drops-deaths-and-complications-by-more-than-one-third/
- Prop-firm assessment overview: https://ecommercefastlane.com/why-modern-prop-firms-use-structured-assessment-models/

Gold and strategies
- Elder, Miao, Ramchander: https://mountainscholar.org/handle/10217/206884
- Andersen et al. 2003: https://scholars.duke.edu/publication/712364 ; Andersen and Bollerslev 1998: https://projects.nber.org/papers/w5783
- Erb and Harvey: https://www.nber.org/papers/w18706 ; T. Rowe Price: https://troweprice.com/en/us/insights/what-is-driving-gold-prices-to-all-time-record-highs ; WGC: https://www.gold.org/goldhub/gold-focus/2023/07/the-drivers-behind-golds-june-and-h1-performance
- Batten et al. 2017: https://pmc.ncbi.nlm.nih.gov/articles/PMC5407636/ ; Batten et al. 2018: https://ideas.repec.org/a/eee/intfin/v52y2018icp102-113.html
- Iwatsubo et al.: https://www.rieti.go.jp/en/publications/summary/17110007.html
- Blose et al.: https://ideas.repec.org/a/spr/jecfin/v42y2018i3d10.1007_s12197-017-9403-0.html
- Caminschi and Heaney: https://ideas.repec.org/a/wly/jfutmk/v34y2014i11p1003-1039.html ; UWA: https://www.news.uwa.edu.au/201410067021/research/uwa-research-finds-gold-and-silver-price-setting-open-exploitation
- Osler: https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr150.pdf
- COT thesis: https://theses.lib.polyu.edu.hk/handle/200/7971?mode=full ; gold GARCH: https://doaj.org/article/f2f793112b9a4a4ab2a582d615af1543
- Gao et al.: https://profiles.wustl.edu/en/publications/market-intraday-momentum/ ; Jin et al.: https://ideas.repec.org/a/wly/jfutmk/v40y2020i4p632-650.html ; Limkriangkrai et al.: https://ideas.repec.org/a/eee/pacfin/v80y2023ics0927538x2300152x.html ; Elaut et al.: https://ideas.repec.org:443/a/eee/finmar/v37y2018icp35-51.html
- Moskowitz et al.: https://papers.ssrn.com/abstract=2089463 ; Hurst et al.: https://www.TrendFollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf
- Park and Irwin: https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html ; Neely et al.: https://files.stlouisfed.org/files/htdocs/wp/2006/2006-046.pdf ; Bajgrowicz and Scaillet: https://ideas.repec.org:443/a/eee/jfinec/v106y2012i3p473-491.html
- Zarattini et al.: https://ssrn.com/abstract=4729284 ; ORB replication blog: https://www.mql5.com/en/blogs/post/776235
- Moreira and Muir: https://www.nber.org/papers/22208
- SMC second-hand summary: https://thortradecopier.com/blog/does-ict-smart-money-concepts-work ; SSRN (not opened): https://doi.org/10.2139/ssrn.7430998

LLM and evaluation
- Profit Mirage: https://arxiv.org/html/2510.07920v1 ; Lookahead test: https://arxiv.org/html/2512.23847v1 ; Glasserman and Lin: https://arxiv.org/abs/2309.17322
- TradingAgents: https://arxiv.org/html/2412.20138v4 ; FinMem: https://arxiv.org/abs/2311.13743 ; FinAgent: https://arxiv.org/abs/2402.18485
- FINSABER: https://arxiv.org/abs/2505.07078 ; StockBench: https://arxiv.org/abs/2510.02209 ; Alpha Arena report: https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/
- Debate or Vote: https://arxiv.org/abs/2508.17536 ; Self-consistency: https://arxiv.org/pdf/2203.11171v2 ; Reflexion: https://neurips.cc/virtual/2023/poster/70114
- Huang et al.: https://arxiv.org/abs/2310.01798 ; Illusion of Diminishing Returns: https://arxiv.org/pdf/2509.09677 ; Xiong et al.: https://arxiv.org/abs/2306.13063
- Bailey/Lopez de Prado: https://papers.ssrn.com/abstract=2460551 ; https://papers.ssrn.com/abstract=2326253 ; https://www.davidhbailey.com/dhbpapers/overfit-tools-at.pdf ; Harvey et al.: https://www.nber.org/papers/w20592
- Thinking Machines: https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/
- Shi et al.: https://arxiv.org/abs/2302.00093 ; Tan et al.: https://proceedings.neurips.cc/paper_files/paper/2024/hash/6ed5bf446f59e2c6646d23058c86424b-Abstract.html ; Echterhoff et al.: https://arxiv.org/abs/2403.00811v3 ; He et al.: https://arxiv.org/abs/2411.10541 ; Sclar et al.: https://arxiv.org/abs/2310.11324
- Lost in the Middle: https://arxiv.org/abs/2307.03172 ; Context Rot: https://trychroma.com/research/context-rot
- Plots Unlock: https://arxiv.org/abs/2410.02637 ; Candlestick VLM benchmark: https://arxiv.org/abs/2604.12659

Anthropic official
- Prompting best practices: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
- Sonnet 5 guide: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5 ; Sonnet 5.5 guide: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5
- Sonnet 5 overview (cutoff Jan 2026): https://platform.claude.com/docs/en/models/sonnet-5/overview ; Models overview (cutoff Jun 2026, cache pricing): https://platform.claude.com/docs/en/models/overview
- Vision: https://platform.claude.com/docs/en/build-with-claude/vision ; Reduce hallucinations: https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations ; Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
