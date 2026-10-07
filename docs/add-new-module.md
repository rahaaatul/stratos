# How to add a new module to this repo

This guide helps you add a new Magisk/MMRL module to the `rahaaatul/stratos` modules repository. Use it when you want to publish a module (your own or a fork) so it shows up in the MMRL/MRepo app and is kept up to date automatically by CI.

## Prerequisites
- Push access to the `rahaaatul/stratos` repository.
- The upstream module must publish either:
  - an `update.json` manifest (preferred — CI can track multiple versions), or
  - GitHub release ZIP assets (CI can track a single latest release).
- `mmrl-util` installed locally if you want to regenerate the index yourself before relying on CI (`pip install mmrl-util`).

## Steps
1. Pick a module `id` that matches the upstream module's folder/identifier (e.g. `my_module`). Create the directory `modules/my_module/`.
2. Create `modules/my_module/track.yaml` with the required fields:
   - `id`, `enable: true`, `categories`, `source`, `readme`, `changelog`, `license`, `last_update` (Unix timestamp), `update_to`.
   - Optional: `verified`, `icon`, `minApi`, `versions` (number of versions to keep).
3. Set `update_to` to one of:
   - An `update.json` URL (e.g. `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/update.json`) for `ONLINE_JSON` tracking, or
   - A direct GitHub release ZIP URL (e.g. `https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>.zip`) for ZIP tracking.
4. (Optional) Seed the first version manually by placing the ZIP and a changelog `.md` in `modules/my_module/`, named `<version>_<versionCode>.zip` and `<version>_<versionCode>.md`.
5. (Optional) If you want the latest release ZIP URL to be kept current automatically, add a target to `zip_track_targets.yaml`:
   ```yaml
   targets:
     - owner: <owner>
       repo: <repo>
       asset_pattern: "-Release.zip"
       file: modules/my_module/track.yaml
   ```
6. Commit the new `track.yaml` (and any seeded files) and push to `main`.
7. Verify the module appears in the index:
   - Locally: `mmrl-util index --push` (regenerates `json/modules.json` from the local `track.yaml` files).
   - Or wait for the next scheduled sync — the `sync-build-deploy` workflow runs every 6 hours and also on `workflow_dispatch` (set `Run Sync = Yes` to trigger immediately).

## Troubleshooting
- Module not showing in the app: confirm `json/modules.json` contains an entry with the module `id`. Run `mmrl-util index --push` locally or wait for the next sync, then check the workflow run's `versions_diff.md` summary for errors.
- `update_to` URL returns 404: verify the upstream path/branch/tag is correct and that the asset or `update.json` is publicly reachable (raw.githubusercontent.com for manifests, github.com release assets for ZIPs).
- Version not syncing: check the sync workflow log (`log/*.log`) for fetch errors; the upstream may be unreachable or the asset pattern may not match.
- `track.yaml` rejected by `mmrl-util sync`: ensure required keys (`id`, `enable`, `update_to`, `source`) are present and `last_update` is a numeric Unix timestamp.
- CI hasn't deployed to GitHub Pages yet: the Pages deploy only runs after a successful sync/index — it is not triggered by plain pushes. Use `workflow_dispatch` with `Run Sync = Yes` to force it.

## Related
- `mmrl-util` documentation: https://mmrl.dev/guide/mmrl-util
- Official template repository: https://github.com/MMRLApp/template-repository
- Sync workflow: `.github/workflows/sync_build_deploy.yml`
- ZIP auto-track workflow: `.github/workflows/update_zip_tracks.yml`
- Repo index consumed by apps: `json/modules.json`
- Per-module metadata: `modules/<id>/track.yaml`