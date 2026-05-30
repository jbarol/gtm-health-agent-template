# GTM Health Bot — How to Ask

By default, replies are executive-summary length. Use the prefixes below to change behavior.

## 1. Ask a question
Just type it. Examples:
- `How many leads did we get last week?` → quick lookup, replies in ~1 min
- `Why is conversion down?` → deeper investigation, ack first then full analysis in 5–30 min

Reply *in the thread* to continue the same session (faster, cheaper).

## 2. Get more detail (expand a response)
Prefix your follow-up with any of:
`expand:` `long:` `details:` `full:` `full version:` `verbose:`

Example: `expand: I want the regional split by month.`

## 3. Teach the bot a standing rule
Prefix with any of:
`remember` `always` `never` `from now on` `going forward` `note that` `keep in mind` `fyi` `correction:` `update:` `feedback:` `instruction:`

The rule is saved to this portco's memory and applied to every future session.

Examples:
- `remember our fiscal year starts October 1`
- `always include win rate %`
- `never include opps older than 2 years`

## 4. Cost reports
- `/cost` — today's spend, this portco
- `/cost week` — trailing 7 days
- `/cost month` — current month
- `/cost reconcile` — local estimate vs. Anthropic billed

## 5. What "executive default" means
Every finding has been adversarially reviewed and statistically validated before posting. Replies lead with the answer, then 2-3 supporting numbers, then a chart link if relevant. Methodology and raw query output are NOT in the default reply — use `expand:` if you need them.

## 6. List pulls
Ask for "the full list" or "every row where…" and the bot writes the file directly to disk and uploads the .xlsx. The reply contains aggregates + the file attachment, never inline rows.
