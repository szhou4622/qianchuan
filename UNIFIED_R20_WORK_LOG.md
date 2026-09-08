# Unified r20 repair work log

Objective: 修复学员的问题，保证开发者本人和学员运行同一套完整 Windows 软件，不依赖开发者机器私有配置。

Baseline: 028b52f83859ffa0634854dab306382a2d2ce24f (production r19).
Working tree: D:/项目开发/qcsckp-unified-r20.

## Required outcomes

- Same Windows artifact and software defaults for teacher/student; no special read-only student edition or hidden developer bypass. Personal accounts, license and strategies remain local, not bundled.
- Fix known student startup loading/duplicate error failures, bounded pagination and its evidence, authorization generations/caches, current-vs-history error reporting.
- Preserve plan-level metrics without account report overwrites; let control metrics refresh independently of material report failures.
- Bound stalled collection/revoke old commits; startup rollback and meaningful worker progress health.
- One verifiable ZIP + SHA256 + release notes + reproducible source/build identity. Verify clean-environment defaults and existing-profile compatibility using that package.
- Preserve r19 and production data. No real ad create/pause/disable/delete/budget/duration testing. Do not publish external services until explicitly included in the release request.
- Do not claim student-side acceptance or full completion without matching evidence.

## Work allocation

- root: collection integration/fencing, plan metric attribution, failure report/UI, release defaults parity, packaging and end-to-end validation.
- pagination agent: qianchuan_open_api/client.py + errors.py + pagination/context helpers + focused tests.
- authorization agent: token_provider.py + configuration.py + account-cache portion of service.py + OAuth handlers/tests.
- startup/runtime agent: startup_bootstrap.py + runtime_supervisor.py + minimal GUI lifecycle hooks + focused tests.

## Progress

- 2026-09-08: Baseline and both student report identities verified. Main source has only unrelated historical .test-updater-logging-home/ untracked; preserved. New worktree created.
- Scope note: earlier read-only student test-package proposal is superseded by the unified functional artifact objective. Tests remain isolated/mocked; production behavior derives from ordinary user settings.

## Evidence and remaining gates

- Initial data: student 9/5 and 9/7 failure reports in C:/Users/EDY/Downloads; help_message over-redacted; official pages material=100/report=200 valid.
- Runtime resource exhaustion was observed on teacher computer, not attributed to student.
- Implemented: bounded pagination / native managed worker startup, OAuth generations and owner+App cache/quota isolation, detailed sanitized API evidence, plan-scoped material metrics only, independent control commit and stop freshness, guarded ancillary writes, startup terminal latch/rollback, public frozen configuration policy and critical managed DLL manifests.
- Targeted tests passed in their isolated runs: 30 pagination/worker; 16 independent control; authorization/catalog/backoff 60 + subsequent 27 including log recovery; 15 report/UI including actual Node rendering; startup/runtime focused and legacy groups. Counts overlap and are not a full-suite total.
- First integration run: 1064 tests, 3 failures/2 errors. Old first-page-rescan, account-report overlay, removed executor and history lease fixtures are being aligned with the new verified contracts. New real SQLite tests cover ancillary scope/fence and coordinator cancellation. No failed run is treated as release acceptance.
- Second full isolated regression: 1080 tests, two legacy UI test failures. Both corrected with the original equal-height/scroll and tray safety requirements preserved; additionally fixed real failure-to-dispatch-hide fallback.
- FINAL frozen-code regression: 1092 tests in 284.855 seconds, OK. Evidence: qcsckp-desktop/output/qa/r20-final-regression.txt. Latest real SQLite collection/control/recovery integration: 40 tests in 35.277 seconds, OK. Python syntax (154 files at that checkpoint) and git diff --check passed.
- Packaging skill + maintenance review used the existing isolated-home/separate-EXE smoke workflow. Agent-skills-standard normative/spec and scripts guidance read; skill validator passed using bundled Python UTF-8. No skill-source update is justified: the project-specific missing-dependency/configuration guards now live in project build code and regression tests; the skill already requires manifest/privacy/isolated verification. No production interpreter packages were installed for skill validation.
- Source freeze ready for local commit and one production-channel r20 ZIP. No external release or server publication requested in this turn.
- Pending gates: final frozen-code full tests; ZIP/privacy/manifest; clean same-package startup and public defaults; distribution handoff. Current r19 and production data remain intact; no actual student-side acceptance claimed.

## r21 correction: activation succeeded but entry failed

- User reopened the r20 artifact, then reported being unable to enter. At 2026-09-08 18:54, license/device/status GET was HTTP 200 and metadata was active/permanent; enterLicensedApplication failed on ModuleNotFoundError: utils.sqlite_prune_scheduler. The original unactivated-window smoke did not cover this path; r20 is marked do-not-distribute while retaining its exact bytes/hash.
- Final r20 EXE/PYZ was read independently: 11 declared runtime modules, exactly the prune module missing. The other ten modules and their parent packages were present and decodable.
- Fix: explicit imports remain inside the lazy service registry resolver; startup order, stop callbacks and watched flags remain unchanged. Add final-EXE archive validation before ZIP creation. GUI returns authorized=true/runtime_start_failed for local runtime failure and offers a separate entry retry; do not reset or re-enter license codes.
- Add loaded-page basename evidence and a real licensed-install smoke using a fresh business home and only three same-machine license DPAPI ciphertext files. No license activation/unbind, no advertising credentials/DB/strategies copied; zero ad requests expected and source ciphertext must remain unchanged.
- Related targeted test combinations passed (124 runtime/GUI/license and 31 archive/packaging). Full r21 frozen-source regression: 1122 tests in 254.562 seconds, OK; output/qa/r21-full-regression.txt. Changed Python syntax and diff whitespace checks pass. Source and delivered package must not be declared accepted until post-license runtime and index.html load are verified.
- Packaging skill maintenance now explicitly requires both final PYZ module completeness and authorized main-page smoke; added references/licensed-pyinstaller-acceptance.md and validation passed. No secret or one-off credential was stored in the skill.
