# Release security and dependency review

Reviewed November 7, 2026. This review covers the synthetic portfolio demo, the repository contents, and published Python package advisories.

## Dependency checks

The Cloud runtime targets Python 3.12. The requirements pin the 37 reviewed runtime versions, including Streamlit 1.67.0, Altair 6.3.0, DuckDB 1.5.6, openpyxl 3.1.5, and pandas 3.0.1. The test environment uses pytest 9.2.0 separately. [Runtime requirements](../requirements.txt)

- `pip check` reported no broken requirements in a clean Windows Python 3.12.14 environment installed from the release requirements and the test dependency.
- A pip dry run resolved 37 distributions for the CPython 3.12 Linux x86-64 wheel target. This checked dependency resolution and wheel availability. The actual Community Cloud runtime still needs its deployment smoke test. [Streamlit dependency documentation](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- PyPI release metadata showed zero published, non-withdrawn vulnerability records for the 37 pinned runtime versions and six additional local tooling distributions checked. Compatible CPython 3.12 x86-64 wheels were available for all 37 runtime versions on Debian 11. Published records can change. [PyPI JSON API](https://docs.pypi.org/api/json/)
- Streamlit’s Windows credential exposure advisory affects versions before 1.54.0. The demo pins 1.67.0. [Streamlit security advisory](https://github.com/streamlit/streamlit/security/advisories/GHSA-7p48-42j8-8846)

## Public demo boundary

- `demo_app.py` uses only the bundled synthetic inputs and their fixed mapping. It renders no uploader and accepts no user-specified file paths. Private mode has a separate entrypoint.
- The default Streamlit configuration sets the upload limit to zero bytes, caps Streamlit messages at 16 MB, and hides detailed browser errors. The local private launch opts into a higher upload limit on the loopback interface. [Streamlit configuration reference](https://docs.streamlit.io/develop/api-reference/configuration/config.toml)
- A local HTTP probe sent the same small synthetic CSV to both entrypoints. The public server rejected it with HTTP 413. The private server accepted it with HTTP 204.
- A process shares one synthetic audit and workbook cache. Per-session state holds the user’s navigation and filters. The server retains disconnected sessions for up to 60 seconds. The deployment host controls request quotas, concurrent sessions, and available memory.
- Community Cloud documents that it enables platform usage statistics and XSRF protection. The app does not require Cloud secrets. [Community Cloud configuration overrides](https://docs.streamlit.io/deploy/streamlit-community-cloud/status#configuration)
- Reconciliation uses fixed DuckDB SQL over in-memory tables. The workbook writes evidence as strings, including text beginning with formula characters. A generated demo workbook contained no external link or macro parts, and its XML contained no common local path patterns.

The repository ignore rules exclude local environments, caches, credential files, uploaded CSV/XLSX files, and generated reports. The release allowlist includes the synthetic fixtures and curated screenshots. Inspect the staged files and their visible screenshot contents before publishing. The local project is inside a larger workspace, so the dedicated release repository should start at the ReconcileIQ directory.

Use the [deployment checklist](deployment.md) for the hosted smoke test. Concurrent traffic and resource exhaustion on a public host remain outside the local review scope.
