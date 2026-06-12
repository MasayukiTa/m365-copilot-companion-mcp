#!/bin/bash
# Prepare 3 astropy working copies (git worktrees) at the pilot base_commits, so each fleet
# job edits its own checkout. Blobless clone keeps it light; worktrees share the object store.
set -e
WORK="/c/Users/USER/companion-mcp/.fleet/swe/work"
mkdir -p "$WORK"
cd "$WORK"
if [ ! -d astropy-main/.git ]; then
  echo "cloning astropy (blobless)..."
  git clone --filter=blob:none --no-checkout https://github.com/astropy/astropy.git astropy-main
fi
cd astropy-main
add_wt() {
  git worktree remove -f "../wt_$1" 2>/dev/null || true
  rm -rf "../wt_$1" 2>/dev/null || true
  git worktree add -f --detach "../wt_$1" "$2"
}
add_wt astropy__astropy-12907 d16bfe05a744909de4b27f5875fe0d4ed41ce607
add_wt astropy__astropy-14182 a5917978be39d13cd90b517e1de4e7a539ffaa48
add_wt astropy__astropy-14365 7269fa3e33e8d02485a647da91a5a2a60a06af61
echo "--- worktrees ---"
git worktree list
echo SETUP_DONE
