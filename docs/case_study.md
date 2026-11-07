# ReconcileIQ case study

ReconcileIQ is a self-directed portfolio project that compares two transaction exports and gives reviewers a way to investigate every difference. The demonstration uses a synthetic ledger and settlement file with documented anomalies.

## Business problem

Finance and operations teams often receive transaction extracts from systems that use different column names and identifier formats. Comparing the grand totals can show a difference, but it leaves the reviewer to find the records behind it. Matching on transaction IDs alone can also multiply rows when an ID repeats, making both the match count and the money appear larger than they are.

The project asks a narrower question that a reviewer can answer with evidence. Which transactions reconcile under a defined rule, which need investigation, and where are the original rows?

## Solution and architecture

The Streamlit workspace summarizes the audit, lets a reviewer filter and inspect exceptions, and exports a seven-sheet Excel report. The report combines an executive summary with reconciliation results, exceptions, duplicate groups, data-quality findings, currency totals, and the settings used for the run.

| Layer | Responsibility |
| --- | --- |
| Input and mapping | Read CSV or XLSX with pandas and openpyxl, retain source row references, and map headers to keys, amounts, currencies, and statuses. |
| Reconciliation engine | Validate and normalize keys, group them with DuckDB, and compare amounts with Python `Decimal`. |
| Review workspace | Display counts and currency totals, filter exceptions by classification and flags, and show both source rows and their applied rules. |
| Audit outputs | Generate the XLSX workbook with openpyxl. The CLI also writes structured JSON and CSV from the same engine. |

The public demo uses the bundled synthetic fixtures. File upload and mapping are available when running the app locally or in a private environment.

## Reconciliation methodology

The reviewer defines a shared key from one or more fields. A key that appears once in both sources can be paired. A key present on just one side becomes a missing-counterpart exception. If a key repeats in either source, every row in that group is quarantined, including a unique row on the other side. This keeps each source row traceable without creating a many-to-many match.

The engine compares amounts in the same currency. The default tolerance is zero. A nonzero difference within a configured tolerance is tracked separately from an exact match. Each result has one primary outcome and can carry multiple issue flags, so an amount difference and a status difference can both remain visible on one record.

The match-rate denominator includes records eligible for review, including missing counterparts and mismatches. Duplicate groups and invalid keys are excluded and counted separately. Currency summaries distinguish all valid source amounts, amounts on comparable pairs, and amounts on reconciled pairs.

## Data quality and evidence

The audit flags missing or malformed keys, amounts, currencies, and statuses. The Exception Explorer shows original values, the file row or worksheet row, and any supplied source ID. The duplicate drilldown shows the full group across both sources. In the Excel report, decimal values that cannot be represented safely as numbers are written as exact text. Source text remains text even when it starts with a formula character.

## Testing and verification

The fixture generator starts with 10,000 matched transaction pairs and applies 14 documented anomaly scenarios. Automated tests exercise matching, tolerance boundaries, row conservation, input validation, the workspace, and the Excel audit. An independent verifier reads the fixture files and the exported JSON. It checks each pairing or standalone result, classifications, flags, source row coverage, match rate, and currency totals against the fixture data and the manifest. [Fixture manifest](../data/demo/ground_truth.json) · [Independent verifier](../scripts/verify_demo.py)

## Demo results

| Measure | Synthetic run |
| --- | ---: |
| Source rows | 20,001 |
| Audit results | 10,007 |
| Eligible results | 9,998 |
| Exact matches at zero tolerance | 9,986 |
| Eligible exceptions | 12 |
| Quarantined or excluded results | 9 |
| Match rate | 99.88% |

For example, rows `A-00003` and `B-00003` share a key and currency, but their amounts are −194.99 EUR and −195.00 EUR. The audit records an amount mismatch and an exact A-minus-B difference of +0.01 EUR. The reviewer can follow both IDs back to the source records.

## Business use cases and limitations

The same workflow can support ledger-to-settlement checks, payment-to-order matching, or validation before and after a data migration. It gives a reviewer a queue to investigate and a record of how each exception was classified.

The engine needs stable, shared transaction keys and compares two extracts per run. It performs no foreign-exchange conversion. Processing is in memory, and Excel numeric formulas skip amounts exported as exact text. The reported match rate measures the seeded fixture run. Throughput, exception rates, and business impact would depend on the data and environment of a real deployment.
