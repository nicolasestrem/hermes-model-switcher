# Changelog

## Unreleased

- Rewrite the README around profile selection, reviewed model changes, and reusable presets.
- Separate advanced routing and Dashboard integration details into a technical reference.
- Clarify community status, platform verification boundaries, credential handling, and guarded recovery.
- Include documentation and the design contract in the assembled plugin package.
- Validate package inputs before replacement and refuse unsafe output paths; require `--replace` for an existing package directory.
- Correct lab helper live-home checks, reject redirected plugin destinations, and preserve prior installations/recovery data during explicit replacement or uninstall.
- Scan README and staged Git blobs, fail closed on unreadable files, require private journal permissions before SQLite opens, and sanitize malformed public URLs.
- Refine Dashboard hierarchy, spacing, mobile scrolling, empty states, and accessible selection feedback; include synthetic-data screenshots.
- Fix Windows file-URL handling in build/fixture scripts and make isolation tests independent of the host temporary directory.
- Verify 402 Python and 69 browser tests against the recorded Linux Hermes revision; document private-API compatibility warnings and measured inventory scaling.
- Address PR #10: block recursive package destinations, preserve URL review visibility, scan deleted files from Git index, order journal symlink checks before chmod, and move lab backups outside plugin discovery with flexible --replace parsing.
- Package documentation only from an explicit approved-file manifest; exclude untracked and ignored local notes, exports, and raw captures.
