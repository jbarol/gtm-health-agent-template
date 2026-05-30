# Investigation Session Rubric

## Hypothesis Testing
- Each "must investigate" hypothesis from the dream plan was tested with at least one SOQL query
- Each hypothesis has an explicit verdict: confirmed, refuted, or inconclusive (with reason)
- Confirmed hypotheses include quantified evidence (absolute numbers AND percentages)
- Refuted hypotheses explain what was found instead

## Quantification
- Every finding includes absolute numbers AND percentages AND period-over-period comparison
- Benchmark comparisons are included where applicable (with the benchmark tier stated)
- Time frames are explicit on every metric (no undated numbers)
- Small samples (n < 30) are flagged with [LOW] confidence

## Domain Coverage
- All three domains (pipeline, sales process, post-sales) produced findings
- Each domain ran data quality checks before analysis
- Each domain reported at least 2 metrics with trend data (current vs prior period)

## Cross-Domain Synthesis
- At least 1 finding connects signals across two or more domains
- The executive summary leads with the most impactful cross-domain finding (if one exists)
- Contradictions between domains are called out (e.g., "pipeline looks healthy but win rate is declining")

## Root Cause Depth
- Findings go beyond surface metrics to root causes
- At least 2 findings include a segmentation that reveals the driver (e.g., "win rate declined because partner-sourced deals grew from 15% to 40%")
- "Why" is answered at least twice for critical findings (5-whys lite)

## Remediation Plan
- Every finding with severity "critical" or "watch" has a remediation recommendation
- Each recommendation is classified: data_fix, process_change, coaching, or strategic
- Data fixes include draft SOQL scripts or cleanup specifications
- Recommendations include expected impact (quantified where possible)

## Slack Notifications
- All critical findings were sent to Slack during the investigation (not just in the report)
- End-of-run info summary was sent with finding counts

## Output Quality
- Weekly report written to /mnt/session/outputs/weekly_report.md with correct structure:
  1. Executive Summary
  2. Key Metrics Table
  3. Critical Findings
  4. Domain Findings
  5. Remediation Plan
  6. Open Questions
- Data export written to /mnt/session/outputs/findings_data.xlsx (if xlsx skill available) or .csv
- Remediation scripts (if any) written to /mnt/session/outputs/scripts/
- Memory update written to /mnt/session/outputs/memory_update.json with:
  - Updated tracked metrics
  - Resolved questions with evidence
  - New open questions for next dream
  - Trend data (weeks improving/declining)

## Evidence Trail
- Every finding cites the exact SOQL query that produced it
- Confidence tags ([HIGH], [MEDIUM], [LOW], [DATA GAP]) are present on every finding
