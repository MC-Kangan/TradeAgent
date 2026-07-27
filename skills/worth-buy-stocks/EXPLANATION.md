# Worth-buy-stocks explanation guide

Use this guide when explaining the `worth-buy-stocks` analyst output to a user
or local agent. The full cross-skill explanation contract is in
`docs/EXPLAINING_SKILL_OUTPUTS.md`.

## Required inputs from the report

Read only the sanitized report fields:

- `status`
- `signal`
- `missing_metrics`
- `limitations`
- `observations`
- `citations`

Do not fetch new prices or recalculate the score.

## Narrative order

1. State whether the setup is buy-quality, watchlist-quality, avoid/reduce-risk,
   or not assessable.
2. Explain the composite score.
3. Explain the risk veto score.
4. Explain the entry class.
5. Mention missing benchmark data, insufficient history, or incomplete OHLCV if
   present.
6. Explain entry, stop, and target levels as model reference levels, not orders.

## Entry class translation

| Code | Phrase |
|---:|---|
| 0 | Trend broken |
| 1 | Overextended |
| 2 | Pullback without trigger |
| 3 | Trend continuation |
| 4 | Pullback reversal |
| 5 | Recovery reversal |

## Example wording

```markdown
The setup is watchlist-quality rather than a clean buy. The composite trend
score is positive, but the risk veto is elevated and the entry classifier does
not show a confirmed fresh-entry setup.

The practical read is that the stock may still deserve monitoring, but the model
does not yet show enough confirmation to upgrade the setup. A stronger relative
strength score, lower risk veto, or a confirmed pullback reversal would improve
the interpretation.
```
