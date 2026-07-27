# Markov-method explanation guide

Use this guide when explaining the `markov-method` analyst output. The Markov
skill is a regime filter, not a valuation model and not a standalone trading
system.

## Required inputs from the report

Read only the sanitized report fields:

- `status`
- `signal`
- `missing_metrics`
- `limitations`
- `observations`
- `citations`

Do not fetch prices or rebuild the transition matrix.

## Narrative order

1. State the current regime bias: bullish, neutral, bearish, or not assessed.
2. Explain `markov_signal` as bull next-step probability minus bear next-step
   probability.
3. Explain stationary bull/bear/sideways probabilities as long-run regime mix.
4. Explain persistence metrics as regime stickiness.
5. Mention partial status when there are fewer than the recommended training
   bars.

## Regime code translation

| Code | Regime |
|---:|---|
| 0 | Bear |
| 1 | Sideways |
| 2 | Bull |

## Example wording

```markdown
The Markov regime filter is neutral. The current transition matrix does not show
a strong imbalance between the probability of a bull next step and a bear next
step.

The practical read is that this analyst is not providing a strong directional
filter. Use it as context alongside fundamentals, technical trend, and risk
checks rather than as a standalone conclusion.
```
