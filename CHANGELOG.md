# Changelog

Every node release, newest first. The version is the node release recorded in
`RELEASE`, which is the only number describing the whole; component versions
live in their own changelogs under `companions/`.

A release is not finished until its entry exists here. That is enforced rather
than promised: the release path refuses to cut a release whose version has no
section below, and names the missing version when it stops.

1.14.0 is the first public release. Earlier versions were developed privately
and are not listed here.

## [Unreleased]

## [1.14.1] - 2026-11-09

Documentation only; nothing on a node changes.

- The quick-start install command in the README and in
  `docs/install-from-github.md` now passes `--node-fqdn`, `--node-domain` and
  `--no-vpn`. Without a node domain Studio has no address, and a clean install
  that followed the old command ended with an error after every check passed.
- The README says where questions and ideas go: GitHub Discussions.

## [1.14.0] - 2026-11-08

Upgrade notes: [docs/upgrade/1.14.0.md](docs/upgrade/1.14.0.md). A node installed as `office` is
not moved by the update; move it by hand first, as that note shows.

- Source is English only. Message and task search stem words with the English
  dictionary for every query (Russian stemming is gone), the failed-turn notice
  is always in English, and voice language aliases are the English and native
  Latin-script names.
- The end-user app is called Studio everywhere, inside as well: the component
  is `companions/studio` (`studio.sh`, `studio.manifest.json`), the service
  `smart-node-studio` in `/opt/smart-node/studio`, logs in
  `/var/log/smart-node/studio`, Traefik files `70-studio.yml` and
  `71-studio-internal.yml`, and the voice setting `SN_STUDIO_URL`. The interface
  says Studio where it said Workspace. Data keys follow: `studio_user` in
  message metadata and the desk API, mesh sources `studio`, `studio-upload`,
  `studio-gdrive`, `studio-chat`, session reason `studio_manual_clean`, and
  the browser draft key `sn-studio-drafts`.
- No compatibility with the office name is kept: `office.<domain>` no longer
  redirects, and a node installed under the old name is not migrated by an
  update. Reinstall such a node, or move it by hand.
- Removed the unused office prototypes `public/next/` and `public/mock/`, the
  old Studio shell (`public/index.html`, `public/js/`, `public/css/`), which
  the server had stopped serving, and the unlinked `public/tasks.html`. The
  tests that read the old shell now read the one that is served
  (`public/next2/`), and describe how it does things: tabs per agent, Start
  clean on the agent page, agent state read on live events, tool words from
  the registry. Studio has no button to start a voice call yet; `/api/me`
  still carries `voiceUrl` for one.
- `studio.sh --uninstall` no longer ends on a helper that does not exist
  (`stage_clear`); a test checks that installers call only defined helpers.
- Removed `companions/screenbox/`: a compose file for a desktop companion whose
  installer and images were never part of this repository.
- `companions/README.md` lists the companions that exist, from their manifests.
  READMEs for core-api, hitl, hitl-upstream, tools, tracking and `_shared`.
  NOTICE lists the approvals service and adapter.
- The memory demo GIF is half the size (3.3 MB).
- The web installer installs what was chosen: Simple, Full or Custom reached the page but
  not the installer, which installed every module. A test now checks the arguments it gets.
- Running the installer again over a container it created says how to continue the install
  in it (`--provider existing --force`), instead of only offering to delete it.
- The source-install runbook says how to put a node on your own computer, WSL included.
- Removed `companions/mesh-image/` and `companions/planner-image/`: memory and
  tracking run as native services and load no image. The update runbook and
  planner's VENDORED no longer send you to copy them.
- CONTRIBUTING says how to run the whole source suite without a node (Linux,
  PostgreSQL binaries, jq, two Python environments, the variables that point
  the tests at them); run that way, all of `tests/run-source.sh` passes.
  Studio's `npm test` runs on Node 22 as well as 20.
- `tools/revendor.sh` no longer defaults to a machine-local source path;
  `--from DIR` is required.
