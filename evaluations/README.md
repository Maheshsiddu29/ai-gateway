# Evaluation datasets

Synthetic cases written for this repository. They contain no customer data, and
their company names, invoice numbers, and amounts are invented.

| File | Dataset | Version | Task | Method | Cases |
| --- | --- | --- | --- | --- | --- |
| `datasets/support-ticket-category-v1.jsonl` | `support-ticket-category` | 1 | classification | `exact_label` | 44 |
| `datasets/invoice-fields-v1.jsonl` | `invoice-fields` | 1 | extraction | `field_f1` | 40 |

`support-ticket-category` asks for one of four categories (account, billing,
shipping, technical). A few tickets are deliberately close to two categories. The
system prompt includes a precedence rule for them: a request about money is
billing.

`invoice-fields` asks for a JSON object with five fields from invoice text in
several languages and number formats. Some invoices do not state a date. Those
fields are absent from `expected`, and a `null` answer for them is not penalized.

Never edit a released file in place. Changing a case, prompt, or expected answer
requires a new file with a new `dataset_version`. The SHA-256 recorded with each
evaluation run then identifies exactly which version produced a result. See
[docs/evaluation.md](../docs/evaluation.md) for the format and scoring rules.

These datasets are small. A model that passes every case has shown that it handles
these 40-odd examples. That says little about general accuracy, so routing compares
thresholds with a confidence bound rather than with the raw pass rate.
