# What a worktree really costs on disk

Measured 2026-10-25 for task #408, with 75 GiB free on the Data volume.

## Summary

- **The largest cost is outside the worktree:** Xcode's DerivedData, about 3.5 GB for each worktree that built
  ballup's iOS app. Xcode keys it by path, so it stayed after the worktree was removed. The janitor now removes
  these caches (`sweep_derived_data`). Today that frees 3.6 GB (the cache of `ballup-monorepo-wt/t343`, a
  worktree that no longer exists).
- **A plain ballup task worktree costs 0.2–1 GB of real disk**, not the 1–1.3 GB that `du` reports. The
  difference is the APFS clone of `node_modules`.
- **ballup's `deploy` worktree costs 3.7 GB, plus 2.7 GB of DerivedData.** That is by design: it keeps its build
  output between deploys.
- **plugins, diskgame and claude-ops worktrees are small:** 14–210 MB each.
- **Nothing else is worth sharing yet.** Gradle's cache (6.4 GB in `~/.gradle`) is already shared by every
  worktree. The other candidates are listed under [Findings](#findings), with what they would save.

## How it was measured

`du` counts each APFS clone as if it owned its blocks. Two clones of a 48 MB file show 95 MiB in `du`, but
deleting one frees nothing. So for each file I also read its APFS **private size**
(`getattrlist` with `ATTR_CMNEXT_PRIVATESIZE`): the bytes no clone shares, the most that deleting it frees. The
script walked each folder, counted hard links once, and summed both figures. It only read files.

Private size undercounts a little. Blocks shared only between two task worktrees count as private in neither, so
deleting both frees more than the two private sizes added up.

## Per worktree (MiB)

| Worktree | du | Private (real) | The big parts (private) |
|---|---:|---:|---|
| ballup-monorepo `t300` (task) | 1279 | 202 | `ballup-dunk/.venv` 146; `node_modules` is a clone: 0 |
| ballup-monorepo `t70` (task) | 954 | 953 | `ballup-dunk/.venv` 167, `ballup-swish/node_modules` ~780: no longer a clone |
| ballup-monorepo `dev` | 1802 | 1654 | `ios/Pods` 294, `sam/dunk` 202, `admin/node_modules` 197, `.venv` 167 |
| ballup-monorepo `deploy` | 4997 | 3724 | `android/app` (build) 1320, `sam/dunk-websocket` 707, `ios/Pods` 269, `ios/build` 103, dSYM 195 |
| diskgame `dev` | 169 | 96 | `Builds/macOS/DiskGame.app` 62 |
| diskgame `t126` (task) | 66 | 14 | `Library` is a clone |
| plugins `t152` (built) | 206 | 206 | `libs/JUCE` 107, `build/` 98 |
| plugins `t397` (not built) | 108 | 108 | `libs/JUCE` 107 |
| plugins `dev` | 1 | 1 | JUCE not checked out |
| claude-ops `t229` | 1 | 1 | |

## Outside the worktrees (MiB)

| Cache | Size | Shared? |
|---|---:|---|
| DerivedData of `ballup-monorepo-wt/dev` | 3595 | per path |
| DerivedData of `ballup-monorepo-wt/t343` (worktree gone) | 3593 | per path; **the janitor removes it** |
| DerivedData of `ballup-monorepo-wt/deploy` | 2671 | per path |
| DerivedData `ModuleCache.noindex` and others | ~1770 | shared |
| `~/.gradle` | 6444 | shared by all worktrees |

## Findings

These would save disk, but each is a change to a project's own config, not to claude-ops. None is large enough
to be urgent now that spent worktrees and their DerivedData go.

1. **ballup `node_modules` can stop being a clone.** In `t70` it is fully private (~780 MB); in `t300` it is
   shared. A worktree made before `node_modules` joined the `[worktree] clone` list, or an `npm install` that
   rewrote most packages, gets its own copy. No change needed: such a worktree goes when its task ends.
2. **ballup `ios/Pods` (~270–300 MB) is built per worktree** that runs `pod install` (dev and deploy today; any
   task that builds iOS). Adding `ballup-swish/ios/Pods` to ballup's `clone` list would make it a clone like
   `node_modules`, saving ~280 MB per iOS-building task worktree. Low risk, but it belongs in ballup's config.
3. **Xcode DerivedData of live worktrees (~3.5 GB each)** could be shared by building every worktree with one
   `-derivedDataPath`. That is not low risk: parallel builds of different branches would overwrite each other's
   products and indexes. Not changed. Removing the caches of removed worktrees gets most of the saving safely.
4. **plugins `libs/JUCE` (107 MB per worktree)** is a submodule checkout. The `clone` list could clone it, saving
   ~100 MB per worktree. Small; not worth it now.
5. **ballup `deploy` Android build (1.3 GB)** is the persistent deploy output that makes the next deploy faster.
   Keep it.
