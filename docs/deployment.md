# Streamlit Community Cloud deployment

The [public ReconcileIQ demo](https://reconcileiq-demo.streamlit.app/) runs on Streamlit Community Cloud from [Chumardo/reconcileiq](https://github.com/Chumardo/reconcileiq). The local synthetic demo uses the same entry point. Use the checks below to verify release updates.

## Review the release contents

1. Keep reviewed release files at the root of the dedicated repository. Keep the parent workspace and its `.git` directory outside the release.
2. Review the files to be staged. Include the two app entry points, `.streamlit/config.toml`, `pyproject.toml`, `requirements.txt`, the engine, workspace modules, tests, scripts, docs, and the four bundled files in `data/demo/`. Keep virtual environments, caches, `.streamlit/secrets.toml`, local uploads, generated reports, and unrelated files out of the repository. `.gitignore` helps enforce those exclusions for untracked files. Inspect the staged filenames before committing.
3. Retain the [MIT License](../LICENSE) and review public commit metadata before publishing changes to `main`.

Community Cloud starts from the repository root and resolves the application environment from its dependency file. Keep `.streamlit/config.toml` and `requirements.txt` at that root alongside `demo_app.py`. The requirements pin the direct and transitive Python 3.12 runtime dependencies and install the ReconcileIQ package. [Community Cloud file organization](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization) · [Community Cloud dependency selection](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)

## Rehearse locally

Run the following from the repository root with a Python 3.12 virtual environment active:

```sh
python -m pip install -r requirements.txt
python -m pip check
python -m streamlit run demo_app.py --server.address 127.0.0.1
```

The `demo_app.py` entry point always uses the two bundled synthetic CSVs, with a 1 MiB and 12,000 data-row limit per file. It exposes no file uploader and requires no secrets or environment variables. The shared `.streamlit/config.toml` sets `server.maxUploadSize = 0` to reject nonempty uploads at the server. Keep that setting in the hosted environment. For local upload use, follow the separate private launch command in the [README](../README.md).

## Deploy after approval

1. Sign in to Streamlit Community Cloud, connect the approved GitHub account, and select the repository owner’s workspace. Community Cloud requires administrative access to the target repository. Review any GitHub access request before granting it. Choose **Create app**, then select the option for an existing app. [Community Cloud workspace](https://share.streamlit.io/) · [GitHub access requirements](https://docs.streamlit.io/deploy/streamlit-community-cloud/status#github-oauth-scope)
2. Enter `chumardo/reconcileiq` and branch `main`. Set **Main file path** to `demo_app.py`. If you want a custom app subdomain, confirm the name before deployment.
3. In **Advanced settings**, select Python **3.12**. Leave **Secrets** empty for this demo. Save the settings and deploy after the owner explicitly approves the hosted deployment. [Community Cloud deployment steps](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
4. Check the app’s **Sharing** setting. Community Cloud sets the default access from the repository’s visibility, so a deployment from a public repository is public by default. An approved private repository can be used for the hosted smoke test before a separate approval to make the demo public. [Community Cloud visibility settings](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)

## Hosted smoke test

1. Open a fresh session and check that the synthetic data label is visible and that Setup offers only **Load demo dataset**.
2. Load the demo. Overview should report **9,986 exact matches**, **12 eligible exceptions**, **9 quarantined records**, and **99.88% match rate** over **9,998 eligible records**.
3. Filter Exception Explorer to duplicate keys. Select a result and confirm that its group shows the related rows from both sources. Open Data Quality and confirm that the source-level counts and exclusions render.
4. Download the XLSX and reopen it. Confirm that all seven sheets are present and that the Executive Summary and Currency Summary agree with the workspace.
5. Repeat with a narrow browser window. Inspect navigation, overflow, and any displayed errors. Check the deployment logs for dependency or startup errors and confirm that viewer-facing screens reveal only synthetic information.

The app reuses one synthetic audit and workbook per process to bound repeated work. A process restart can delay the first request. There is no app-level execution timeout or visitor quota, and sustained concurrency has yet to be measured on Community Cloud. Review logs and resource use after deployment updates. [Community Cloud platform limits](https://docs.streamlit.io/deploy/streamlit-community-cloud/status) · [Project security review](security_review.md)
