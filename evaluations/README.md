# Evaluation datasets

Synthetic cases written for this repository. They contain no customer data, and
their company names, invoice numbers, and amounts are invented.

| File | Dataset | Version | Partition | Task | Method | Cases |
| --- | --- | --- | --- | --- | --- | --- |
| `datasets/support-ticket-category-v1.jsonl` | `support-ticket-category` | 1 | development | classification | `exact_label` | 44 |
| `datasets/support-ticket-category-v1-holdout.jsonl` | `support-ticket-category` | 1 | holdout | classification | `exact_label` | 60 |
| `datasets/invoice-fields-v1.jsonl` | `invoice-fields` | 1 | development | extraction | `field_f1` | 40 |
| `datasets/invoice-fields-v1-holdout.jsonl` | `invoice-fields` | 1 | holdout | extraction | `field_f1` | 40 |

`manifest.toml` records each file's SHA-256 and case count. A unit test compares
the files with it, so a released file cannot be edited in place without failing
the build.

## Partitions

Results on a development partition can be recorded as routing evidence. Results
on a holdout partition are never routing evidence. The gateway ignores them, and
`tools/routing_experiment.py` refuses them as evidence. A holdout exists to
measure a routing policy on cases that played no part in choosing its models.

Both partitions of a version use the same system prompt, label set, and scoring
rules. `python -m gateway.evaluation check` verifies this and rejects a holdout
that shares a case ID with the development partition or repeats one of its cases.
A case counts as a repeat when at least 60% of the distinct words in its
non-system messages are shared (Jaccard similarity), with digits ignored so that
a copied case with changed numbers is still caught. The check catches copying,
not paraphrase: two differently worded tickets about the same problem both pass.

## Construction

`support-ticket-category` asks for one of four categories (account, billing,
shipping, technical). The system prompt includes a precedence rule: a request
about money is billing even when a delivery or product problem caused it. Both
partitions include tickets that sit near two categories so that the rule is
exercised. The holdout has 15 tickets per category. It adds informal and
misspelled tickets, and a few where the subject matter (a password reset link, a
checkout page) points to one category and the problem described (a page error)
belongs to another.

`invoice-fields` asks for a JSON object with five fields from invoice text. The
development partition covers several languages, date orders, and number formats.
The holdout uses languages and layouts the development partition does not
(Italian, Dutch, Portuguese, Czech, Turkish, Danish, Japanese dates, dot-leader
tables), invoice numbers that look like dates or amounts, replaced invoices,
partial payments, and currencies given only by symbol or local name (R$, Kč, zł,
TL, ₩). Some invoices do not state a date. Those fields are absent from
`expected`, and a `null` answer for them is not penalized.

## Known biases and limits

- One author wrote every case and assigned every label. The labels have not been
  checked by a second annotator, and the cases reflect what that author thought
  to include.
- Tickets are short, single-issue, and in English. Real tickets are longer, often
  raise several issues, and arrive in many languages.
- Invoice texts are one to six lines. Real invoices are PDFs or OCR output with
  line items, addresses, and noise.
- Holdout tickets are balanced across categories. Real traffic is not, so a
  model's success rate on live traffic can differ even if per-category accuracy
  is the same.
- 40 to 60 cases per partition give wide confidence intervals: a perfect score on
  60 cases has a 95% lower bound of about 0.94, and 54/60 has about 0.80. These
  datasets can show that a model handles these examples. They cannot show that
  two models are equivalent, or estimate production quality.

## Changing a dataset

Never edit a released file in place. Changing a case, prompt, or expected answer
requires new files with a new `dataset_version`, for both partitions, and new
`manifest.toml` entries. The SHA-256 recorded with each evaluation run then
identifies exactly which file produced a result. See
[docs/evaluation.md](../docs/evaluation.md) for the format and scoring rules.
