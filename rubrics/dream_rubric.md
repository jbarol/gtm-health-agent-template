# Dream Session Rubric

## Memory Review
- All open questions from prior sessions are acknowledged and addressed (carried forward, investigated, or explicitly deprioritized with rationale)
- Tracked metrics are listed with their current value, prior value, and trend direction
- Resolved questions from prior sessions are NOT re-investigated

## Hypothesis Generation
- At least 5 hypotheses generated
- Each hypothesis includes:
  - A specific claim about what might be happening (not vague — "partner leads converting at <10%" not "lead quality might be low")
  - Why it matters: estimated dollar impact or operational risk
  - How to test it: specific SOQL queries or analysis approach with field names
  - Which domain it belongs to: pipeline, sales_process, or post_sales
- At least 3 hypotheses are supported by a data-based rationale (prior metrics, trends, or cross-domain signals)

## Novel Thinking
- At least 1 hypothesis is NOT derived from open questions — a genuinely new angle
- At least 1 hypothesis connects signals across two or more domains (e.g., correlating a pipeline signal with a post-sales outcome)

## Prioritization
- Hypotheses are ranked by expected impact multiplied by testability
- Top 3 are marked as "must investigate" with clear justification
- Any hypotheses that depend on data availability (fields that may not exist) are flagged

## Output Format
- Plan is written to /mnt/session/outputs/dream_plan.json with this schema:
  - hypotheses: array of {id, claim, domain, impact_rationale, test_approach, priority, novel: bool}
  - memory_context: summary of what was read from memory
  - metric_snapshot: current tracked metric values carried forward
- Memory update written to /mnt/session/outputs/memory_update.json
