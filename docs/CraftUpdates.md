# ArtCraft portable updates

Optimus 0.12.47 adds an opt-in Windows updater for an explicitly registered ArtCraft portable suite. It checks the publisher's stable GitHub releases during maintenance and replaces eligible closed apps. This integration is separate from WinGet package selection and does not install the suite, discover other copies or register a new installation.

## Supported suite and registration

The exact registered catalog contains all twelve apps below. Each source is the official `storytold` repository linked by [ArtCraft](https://getartcraft.com/).

| App | Official repository |
| --- | --- |
| PhotoCraft | [storytold/photocraft](https://github.com/storytold/photocraft) |
| VectorCraft | [storytold/vectorcraft](https://github.com/storytold/vectorcraft) |
| FilmCraft | [storytold/filmcraft](https://github.com/storytold/filmcraft) |
| LightCraft | [storytold/lightcraft](https://github.com/storytold/lightcraft) |
| PdfCraft | [storytold/pdfcraft](https://github.com/storytold/pdfcraft) |
| EffectCraft | [storytold/effectcraft](https://github.com/storytold/effectcraft) |
| DesignCraft | [storytold/designcraft](https://github.com/storytold/designcraft) |
| WordCraft | [storytold/wordcraft](https://github.com/storytold/wordcraft) |
| SoundCraft | [storytold/soundcraft](https://github.com/storytold/soundcraft) |
| DeckCraft | [storytold/deckcraft](https://github.com/storytold/deckcraft) |
| CADCraft | [storytold/cadcraft](https://github.com/storytold/cadcraft) |
| GridCraft | [storytold/gridcraft](https://github.com/storytold/gridcraft) |

Registration is per device and requires an existing verified portable installation. In the Optimus data folder, `craft-installation.json` must have schema `1`, the exact absolute suite `root`, `updates_authorized: true` from an explicit approval, and an `apps` list matching the catalog above in that order. The suite root must contain `Apps/<AppName>` directories and its original `download-manifest.json`.

The initial manifest is a list with an entry for every app. An entry records `Name`, `Status: "Ready"`, `AppDirectory`, stable three-part `Version`, original archive `DownloadPath`, GitHub `ExpectedDigest` in `sha256:<hex>` form, and byte `Size`. Keep the verified initial archives inside the registered suite root: the first update uses them to distinguish release files from local user files. Later updates retain their release-file inventory in `craft-update-state.json`. A checkbox alone cannot supply these prerequisites, and Optimus has no general registration wizard for arbitrary Craft installations.

## Enable maintenance and sign-in checks

In Software Updater, enable **Include the Software Updater stage** and **Keep my registered Craft apps up to date (closed apps only)**, then save the reviewed choices. Craft updates start off by default, including on new installations. Normal launch review, saved unattended approval and maintenance cadence still control when the main workflow runs. The Craft stage updates its registered scope independently of the WinGet review/selected/all-supported policy.

`Optimus.exe --craft-updates-only` is a narrow command for an explicitly configured sign-in task or a manual check. It does not open the interface, request administrator access or run cleanup, debloat or other maintenance. It reads the current saved settings and registration, so the main interface does not need to remain open for that command to finish. The checkbox and command do not create a sign-in task. The ordinary Optimus sign-in launch remains a separate setting.

The Craft-only command requires the saved Software Updater switch to be on. A saved `craft_updates_enabled: false` disables it. If that preference is absent, the exact registration's durable `updates_authorized` approval supplies the Craft choice; it never registers another suite or expands the app catalog. This supports a separately configured task that retains this updater while the main Optimus installation changes versions. Both the maintenance stage and Craft-only command share a lock to prevent concurrent Craft update runs. Turn off the Craft option or the Software Updater to stop future checks; disable any separately configured task if it is no longer wanted.

## Verification and replacement

Only newer stable three-part releases from the exact repositories above are eligible. Drafts, prereleases, downgrades, MSI installers, ARM64 archives and other asset names are excluded. The release must contain exactly one `<repo>-<version>-windows-x64-portable.zip` with the expected GitHub release URL, a published SHA-256 digest and a valid advertised size. HTTPS redirects stay on the supported GitHub hosts. The downloaded bytes are checked again before extraction; the digest comes from GitHub and is not independent code-signing verification.

Archives are bounded to 512 MiB downloaded and 2 GiB expanded, with path/link and expected Windows executable checks. At least 2.25 GiB of free space must remain before extraction. Replacements are staged under the registered suite's `.updates` directory. A running app, an uncertain matching process or an app opened during staging defers its replacement to a later check; Optimus does not terminate it or force a restart.

Portable `data`/`*CraftData` contents and non-release user files are copied into the staged app. Conflicting custom files stop that app's update. The complete previous app directory is retained under the suite's `Previous Versions` directory before the replacement is installed. These retained copies use disk space and are not automatically pruned by this updater. An interrupted swap is journaled and recovered before another Craft update proceeds; recovery can require closing the affected app. A recovery failure preserves the journal and prior version for investigation and stops the remaining Craft replacements.

`craft-updates-last.json` in the Optimus data folder records per-app versions, successes, deferrals and failures. Ordinary per-app failures allow other registered apps to be checked; cancellation or unresolved recovery stops the batch. Staging cleanup removes only that attempt's private work directory. Prior versions and recovery records remain. A successful update confirms the verified files were replaced; it does not certify the publisher's app behavior or document compatibility. Use these early-development apps with appropriate copies of important documents.
