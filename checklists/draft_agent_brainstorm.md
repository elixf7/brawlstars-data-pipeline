# Draft Agent Brainstorm

## Goal
Build a draft helper for Brawl Stars ranked mode that recommends the next best pick given:
- My current partial team
- Opponent's known picks
- Mode and map
- Skill tier

Confidence in each recommendation is a first-class requirement — the agent should know when it doesn't know.

---

## Context and Constraints

- Data source: ranked match API — final team compositions only, no pick order, no ban info
- One row per set (best-of-3); `record` encodes game-level outcomes (e.g. T1-T2-T1)
- ~2.7M rows available for season 42; constraining to a single season avoids meta drift
- Elo drifts within a season (early season is inflated; late season is stratified)
  - `skill_ns` (time-local ECDF normalization) was built to address this and should be used throughout

---

## Overall Architecture: MCTS + Calibrated Evaluator

The draft is a sequential game with alternating picks. The right framework is **Monte Carlo Tree Search (MCTS)** because:
- Each node = a partial draft state (my picks + opponent picks + whose turn it is)
- Each action = picking a brawler
- Terminal nodes = complete 3v3 compositions
- Opponent turns are modeled explicitly — the search accounts for how the opponent can respond to each pick

This handles the key failure of a "set completion" approach: picking a strong brawler in isolation may look good, but MCTS will simulate the opponent countering it and correctly discount its value.

### What Went Wrong Before

The original approach was MCTS + deep NN evaluator + RL policy/value networks. The problems:
1. **Deep NN evaluator** — poorly calibrated probabilities, opaque confidence, sensitive to data imbalance
2. **RL policy/value networks** — required training on sequential draft data we don't have (API only gives final states); hard to train stably
3. **Compounding error** — MCTS amplifies small errors in the evaluator across rollouts
4. **Confidence** — bolted-on heuristic on top of a black-box model, never reliable

The fix is not to abandon MCTS — it's to simplify what's inside it.

---

## Component 1: The Matchup Database

Build empirical statistics directly from the match data, per mode, per map, per skill tier (binned `skill_ns`).

**What to compute:**
- Pairwise counter matrix: `P(win | brawler X on my team, brawler Y on opponent team)`
- Pairwise synergy matrix: `P(win | brawler X and Y on same team)` vs. baseline
- Individual pick/win rates per brawler per mode/map

**Strengths:**
- Directly grounded in data, no training required
- Confidence is intrinsic: each cell has a sample count. Low n = uncertain, high n = reliable
- Interpretable: you can inspect why a brawler is recommended

**Drawbacks:**
- Pairwise statistics don't capture 3-way interactions (e.g. a specific triple synergy)
- Sparse cells for rare brawlers or niche mode/map/skill combinations
- Needs Bayesian smoothing (e.g. Beta-Binomial / Dirichlet-Multinomial) to avoid extreme estimates from small samples

---

## Component 2: Terminal State Evaluator (Replaces Deep NN)

Given a complete 3v3 composition + mode + map + skill tier, estimate win probability for team 1.

### Option A: Bradley-Terry Model
Each brawler has a latent strength parameter. Team strength = sum of member strengths.
`P(T1 wins) = T1_strength / (T1_strength + T2_strength)`

**Strengths:**
- Simple, fast, well-calibrated probabilities
- Each brawler gets an interpretable strength score
- Can be fit per mode/map/skill tier
- Training is stable and fast (convex optimization)

**Drawbacks:**
- Purely additive — doesn't model synergies or counters between specific brawlers
- May underfit complex team interactions

### Option B: Logistic Regression on Matchup Features
Features = sum of pairwise counter advantages (all 9 cross-team pairs) + synergy scores (3 intra-team pairs per side) + skill_ns delta.

**Strengths:**
- Incorporates the matchup database directly
- Slightly richer than Bradley-Terry (counters are explicit features)
- Still fast, calibrated, and interpretable

**Drawbacks:**
- Still limited to pairwise interactions, not higher-order
- Feature engineering depends on quality of matchup database

### General Evaluator Notes
- Both options are far simpler than a deep NN — faster at inference (critical for MCTS rollouts), more stable, better calibrated
- Train on final compositions from the DB; no pick-order data needed
- Validate by holding out a fraction of matches and checking calibration

---

## Component 3: MCTS

Standard MCTS (UCB1 selection, random rollout, backprop).

### Rollout Policy (Replaces RL Policy Network)
During simulations, instead of a learned policy:
- On my turn: sample remaining brawlers weighted by pick rate for this mode/map/skill tier
- On opponent's turn: sample weighted by pick rate *against my current team* (using counter data)

**Strengths:**
- No training required — uses pick-rate data we already have
- Still directional (not pure random), so rollouts are more realistic
- Much simpler than a policy network

**Drawbacks:**
- Not optimal — opponent won't always respond "realistically" since it's not learned from draft sequences
- Pick-rate weighting is a proxy; actual opponent pick decisions depend on their specific team

### Opponent Modeling
- **Minimax**: assume opponent always picks what maximizes their win rate (conservative, finds robust picks)
- **Stochastic**: opponent picks from a distribution (realistic, especially at lower skill)
- **Mixed**: blend by `skill_ns` tier — high-skill opponents modeled as more optimal

**Strengths:**
- Minimax finds picks that hold up even against a smart opponent
- Directly addresses the "tank that gets countered" problem

**Drawbacks:**
- Minimax can be overly pessimistic — real opponents don't always find the optimal counter
- Opponent model is still heuristic without actual opponent draft data

---

## Confidence: Two-Layered

### Layer 1: Evaluator confidence
When scoring a terminal state, track the harmonic mean of sample sizes for each pairwise matchup cell used. Low average n = low confidence in that evaluation. This propagates into the MCTS rollout scores.

### Layer 2: MCTS convergence confidence
After N simulations, look at the distribution of visit counts and win rates across candidate picks:
- One brawler clearly dominates → high confidence
- Top 3 candidates are clustered → low confidence, flag as "no clear best pick"

**Strengths:**
- Both layers are structural — no need for a separate confidence model
- Directly communicable to the user (e.g., "recommended with high confidence based on 4,200 similar matchups")

**Drawbacks:**
- MCTS convergence confidence doesn't account for whether the *simulations themselves* were realistic
- Sample-size-based confidence can be misleading if the matchup data is biased (e.g. certain brawlers are only picked in unusual contexts)

---

## The Missing Pick Order Problem

The API provides only final team compositions — no sequential draft states. This affects modeling in one specific way:

- **Terminal evaluator**: unaffected. It scores complete compositions, which is all it needs.
- **Rollout policy**: affected. Without sequential draft data, the rollout policy can't be learned from real draft sequences. Heuristic (pick-rate weighted) rollouts are used instead.
- **RL policy/value networks**: this is why they were hard to train — there's no ground truth for intermediate draft states. By removing RL entirely, this problem goes away.

The evaluator quality matters far more than rollout policy quality in MCTS. As long as the evaluator is well-calibrated, heuristic rollouts converge to reasonable recommendations.

---

## Skill Tier Handling

Segment all analyses by `skill_ns` bucket (e.g. 4 tiers: casual / mid / high / elite).

- Matchup database computed separately per tier
- Evaluator trained per tier (or with skill_ns as a feature)
- MCTS rollout policy uses tier-appropriate pick rates

**Why this matters:** A brawler that dominates at casual play may be actively bad at elite level. Mixing skill tiers in a single model produces muddled recommendations.

---

## Open Questions / Things to Resolve

- What is the exact pick order format in Brawl Stars ranked? (e.g. 1-2-2-1-1-2 or similar). This affects how MCTS alternates turns.
- Are bans exposed anywhere (third-party APIs, scraped data)? Bans significantly change draft strategy.
- How to handle brawler releases / reworks within a season? New brawlers have sparse data.
- How many brawlers are in the pool at any given time? This determines the branching factor for MCTS.
- Is there a meaningful distinction between game 1, game 2, and game 3 outcomes in the `record` column worth exploiting?

---

## Summary Table

| Component | Approach | Strength | Weakness |
|---|---|---|---|
| Win data | Empirical matchup DB per mode/map/skill tier | Grounded, fast, interpretable | Sparse for rare combos |
| Terminal evaluator | Bradley-Terry or logistic regression | Calibrated, fast, stable | Pairwise only, no deep synergies |
| Sequential reasoning | MCTS with UCB1 | Accounts for opponent responses | Rollout policy is heuristic |
| Opponent model | Minimax or stochastic by skill tier | Realistic, robust recommendations | Heuristic, not learned |
| Confidence | Evaluator sample size + MCTS variance | Structural, no extra model needed | Can be misleading with biased data |
| Skill handling | `skill_ns` bucket segmentation | Solves Elo drift problem | Thinner data per bucket |
