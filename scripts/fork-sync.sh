#!/usr/bin/env bash
# Keep local/main -- this fork's customizations -- current with jgravelle's
# main, and cut pull requests out of it.
#
#   fork-sync status                 what is ours, and where it sits
#   fork-sync update [--no-push]     replay local/main onto upstream
#   fork-sync pr <branch> <commit>... [--dry-run]
#                                    build a PR branch from those commits and
#                                    open the PR
#
# where `fork-sync` means, from anywhere inside the checkout:
#
#   MSYS_NO_PATHCONV=1 bash <(git show local/main:scripts/fork-sync.sh)
#
# Running ./scripts/fork-sync.sh directly works too, but mid-replay the file on
# disk is a half-replayed older version -- so the committed one is canonical.
#
# THE LAYOUT
#
#   upstream/main     jgravelle's tree. Fetch only: its push URL is DISABLED.
#   main              a pristine mirror of upstream/main; carries nothing.
#   local/main        upstream/main + our customizations, one commit each.
#                     The main checkout sits on it, and the editable install
#                     (uv tool, pointed at this checkout) runs it, so it is
#                     also what the HTTP proxy serves. Backed up to
#                     origin/local/main.
#   <pr branches>     short-lived: upstream/main + the commits one PR needs.
#                     Built by `pr` without checking anything out, so the
#                     running install is never touched. Deleted once merged.
#
# `git log upstream/main..local/main` is always the exact list of what is
# ours. When jgravelle merges one of our PRs, the next `update` notices the
# change is already upstream and drops our copy, so that list shrinks.
#
# NOTE ON `git stash`: a PreToolUse hook refuses stash, reset, restore and
# `checkout -- <path>`, because agent sessions share one checkout. This script
# never stashes; it refuses a dirty tree.

set -euo pipefail

# Anchor at the repo root. Asked of git, not derived from this file's path,
# because the supported invocation runs the script from a pipe:
#
#   bash <(git show local/main:scripts/fork-sync.sh) <command>
#
# which is immune to the working tree changing mid-replay.
cd "$(git rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "Run from inside the jcodemunch-mcp checkout." >&2; exit 1; }

# Never execute the copy in the working tree. A replay rewrites this very file
# while bash is still reading it, and mid-replay the file on disk is whatever
# half-replayed version the rebase has reached. Run the version committed on
# local/main instead, from a temp copy nothing will touch.
if [ -z "${FORK_SYNC_REEXEC:-}" ]; then
    self=$(mktemp)
    MSYS_NO_PATHCONV=1 git show "refs/heads/local/main:scripts/fork-sync.sh" > "$self" \
        || { rm -f "$self"; echo "local/main has no committed scripts/fork-sync.sh." >&2; exit 1; }
    FORK_SYNC_REEXEC=1 FORK_SYNC_ROOT=$(pwd) FORK_SYNC_SELF=$self exec bash "$self" "$@"
fi
cd "$FORK_SYNC_ROOT"
# Only the temp copy: with FORK_SYNC_REEXEC set by hand, BASH_SOURCE is the
# working tree's own script, which must survive.
CLEANUP=(${FORK_SYNC_SELF:+"$FORK_SYNC_SELF"})
trap 'rm -rf "${CLEANUP[@]}"' EXIT

UPSTREAM_REMOTE=upstream
UPSTREAM=upstream/main
MIRROR=main
MAIN=local/main
FORK_REMOTE=origin
UPSTREAM_REPO=jgravelle/jcodemunch-mcp

die() { printf '%s\n' "$*" >&2; exit 1; }

require_clean() {
    [ -z "$(git status --porcelain)" ] \
        || die "Working tree is dirty. Commit first (this script will not stash)."
}

# The mirror moves by ref update, never by checkout. Refuses when main holds
# anything upstream does not: that is local work in the wrong place.
advance_mirror() {
    if ! git merge-base --is-ancestor "$MIRROR" "$UPSTREAM"; then
        die "$MIRROR has commits $UPSTREAM does not, so it is not a mirror:

    git log --oneline $UPSTREAM..$MIRROR

Move them onto $MAIN (cherry-pick), then: git branch -f $MIRROR $UPSTREAM"
    fi
    if git worktree list --porcelain | grep -qx "branch refs/heads/$MIRROR"; then
        echo "    $MIRROR is checked out somewhere; leaving it (it is only a mirror)"
    else
        git update-ref "refs/heads/$MIRROR" "$UPSTREAM"
    fi
}

# Every file the pre-rebase branch had diverged in, and whether it still
# differs from upstream. A rebase reports a conflict only where two sides edit
# the same region; where upstream rewrote a file we had also changed, ours can
# vanish silently. So read the file list, not the rebase's exit code.
verify_nothing_lost() {
    local pre_tip=$1 pre_base=$2 absorbed=$3 f lost=() absorbed_files remaining_files
    # A file that stops differing is EXPECTED when upstream merged the commit
    # that changed it -- but only if none of our remaining commits touch it.
    absorbed_files=$( [ -n "$absorbed" ] && git show --format= --name-only $absorbed | sort -u )
    remaining_files=$(git log --format= --name-only "$UPSTREAM..$MAIN" | sort -u)
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        git diff --quiet "$UPSTREAM" "$MAIN" -- "$f" || continue
        if grep -qxF "$f" <<<"$absorbed_files" && ! grep -qxF "$f" <<<"$remaining_files"; then
            echo "    absorbed  $f"
            continue
        fi
        lost+=("$f")
    done < <(git diff --name-only "$pre_base" "$pre_tip")

    [ ${#lost[@]} -eq 0 ] && { echo "    ok: every change of ours is still here or was absorbed upstream"; return 0; }

    echo "    ${#lost[@]} file(s) no longer differ from upstream -- absorbed, or lost:" >&2
    printf '        %s\n' "${lost[@]}" >&2
    cat >&2 <<EOF
    Fine if upstream made the same change (see "absorbed" above); a silent
    loss if not. Compare:
        git show $pre_tip -- <file>
        git log $UPSTREAM -- <file>
EOF
    return 1
}

# The top-level cli/ is upstream's and stays byte-identical to it, so upstream
# edits there never conflict with ours (CLAUDE.local.md).
cli_matches_upstream() {
    if git diff --quiet "$UPSTREAM" "$MAIN" -- cli/; then
        echo "    ok"
        return 0
    fi
    echo "    DIVERGED -- cli/ differs from $UPSTREAM:" >&2
    git diff --stat "$UPSTREAM" "$MAIN" -- cli/ >&2
    return 1
}

# Import the server from this checkout and count its tools: the cheapest proof
# the stack still loads after a replay.
smoke() {
    local out
    out=$(PYTHONPATH=src python -c \
        "import jcodemunch_mcp.server as s; print(len(s._build_tools_list()))" </dev/null 2>&1) \
        || { printf '%s\n' "$out" >&2; return 1; }
    echo "    ok: server imports, $out tools"
}

# Sorted ids of the failing tests in tree $1, running test files $2...
failed_ids() {
    local dir=$1; shift
    (cd "$dir" && PYTHONPATH=src python -m pytest -q -rfE -p no:cacheprovider "$@" </dev/null 2>&1 || true) \
        | sed -n 's/^\(FAILED\|ERROR\) \([^ ]*\).*/\2/p' | sort -u
}

restart_proxy_if_up() {
    local p=scripts/jcodemunch-proxy.sh
    [ -x "$p" ] || return 0
    if "$p" status </dev/null 2>/dev/null | grep -q '^UP'; then
        "$p" restart </dev/null | tail -1
    else
        echo "    proxy not running; nothing to restart"
    fi
}

cmd_status() {
    git fetch "$UPSTREAM_REMOTE" --quiet
    local ahead behind b
    ahead=$(git rev-list --count "$UPSTREAM..$MAIN")
    behind=$(git rev-list --count "$MAIN..$UPSTREAM")
    echo "$MAIN: $ahead commit(s) of ours on top of $UPSTREAM" \
         "$([ "$behind" = 0 ] && echo "(current)" || echo "-- $behind upstream commit(s) behind; run 'update'")"
    git log --format='    %h %s' "$UPSTREAM..$MAIN"

    echo
    echo "checked out here: $(git symbolic-ref --quiet --short HEAD || echo 'detached')"

    local others=()
    while IFS= read -r b; do
        case "$b" in "$MAIN"|"$MIRROR") continue ;; esac
        others+=("$b")
    done < <(git for-each-ref --format='%(refname:short)' refs/heads/)
    if [ ${#others[@]} -gt 0 ]; then
        echo
        echo "other branches (PR branches and backups):"
        for b in "${others[@]}"; do
            printf '    %-40s %s commit(s) over %s%s\n' "$b" \
                "$(git rev-list --count "$UPSTREAM..$b")" "$UPSTREAM" \
                "$(git rev-parse --verify --quiet "$FORK_REMOTE/$b" >/dev/null && echo ", pushed" || echo "")"
        done
    fi

    if command -v gh >/dev/null 2>&1; then
        echo
        echo "open PRs from this fork:"
        GITHUB_TOKEN="" gh pr list --repo "$UPSTREAM_REPO" --state open \
            --author "@me" --json number,headRefName,title \
            --jq '.[] | "    #\(.number) \(.headRefName) -- \(.title)"' 2>/dev/null \
            || echo "    (gh query failed)"
    fi
}

cmd_update() {
    local push=1
    [ "${1:-}" = "--no-push" ] && push=0

    # First: mid-replay HEAD is detached, and the branch check below would
    # report that instead of the actual situation.
    local gitdir
    gitdir=$(git rev-parse --git-dir)
    if [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ]; then
        die "A rebase is in progress. Resolve it (git status), then 'git rebase --continue'
-- or 'git rebase --abort' -- and run update again."
    fi

    [ "$(git symbolic-ref --quiet --short HEAD || true)" = "$MAIN" ] \
        || die "Run this from the checkout that has $MAIN checked out."
    require_clean

    echo "==> Fetching $UPSTREAM_REMOTE"
    git fetch "$UPSTREAM_REMOTE" --quiet

    echo "==> Advancing $MIRROR"
    advance_mirror

    local pre_ref="refs/fork-sync/$MAIN/pre-update" pre_tip pre_base
    # Saved state, but local/main is off upstream and no rebase is running: the
    # replay was aborted or rolled back to the saved tip. Start over from here.
    if git rev-parse --verify --quiet "$pre_ref" >/dev/null \
       && ! git merge-base --is-ancestor "$UPSTREAM" "$MAIN"; then
        git update-ref -d "$pre_ref"
        git update-ref -d "$pre_ref-base" 2>/dev/null || true
    fi
    if git rev-parse --verify --quiet "$pre_ref" >/dev/null; then
        # A previous update stopped (conflict, or a failed check). Its checks
        # have not passed yet, so finish them against the SAVED pre-state --
        # re-deriving it now would compare the result against itself.
        pre_tip=$(git rev-parse "$pre_ref")
        pre_base=$(git rev-parse --verify --quiet "$pre_ref-base") \
            || die "Saved state is incomplete: $pre_ref exists without $pre_ref-base.
Inspect both, then delete them with 'git update-ref -d' and re-run."
        echo "==> Finishing an interrupted update (pre-update tip ${pre_tip:0:7})"
    elif git merge-base --is-ancestor "$UPSTREAM" "$MAIN"; then
        echo "==> $MAIN already sits on current $UPSTREAM; nothing to replay"
        # Still back up: commits made directly on local/main need it too.
        [ "$push" -eq 1 ] && git push --quiet --force-with-lease "$FORK_REMOTE" "$MAIN" \
            && echo "==> Backed up to $FORK_REMOTE/$MAIN"
        return 0
    else
        pre_tip=$(git rev-parse "$MAIN")
        pre_base=$(git merge-base "$MAIN" "$UPSTREAM")
        git update-ref "$pre_ref" "$pre_tip"
        git update-ref "$pre_ref-base" "$pre_base"

        echo "==> Replaying $MAIN onto $UPSTREAM ($(git rev-list --count "$MAIN..$UPSTREAM") new upstream commit(s))"
        replay_main
    fi

    finish_update "$pre_tip" "$pre_base" "$push"
}

replay_main() {
    if ! git rebase "$UPSTREAM"; then
        cat >&2 <<EOF

Replay stopped on a conflict. The commit being applied is one of ours:

    git rebase --show-current-patch     # which change, and why
    git status                          # which files

Resolve toward OUR commit's intent while keeping upstream's surrounding
changes, then \`git add <files>\` and \`git rebase --continue\`. Never resolve
by dropping our change to make it apply. \`git rebase --abort\` puts
everything back.

One conflict recurs on every replay:
  - tests/test_v1_108_176.py: upstream keeps unskipping two tests whose
    lever (index_folder.validate_path) this fork removes. Keep our
    @pytest.mark.skip markers. This one can also vanish WITHOUT a conflict;
    the lost-file check below is what catches it.

When the rebase is done, run \`fork-sync update\` again: it finishes the checks
against the saved pre-update state before pushing or restarting anything.
EOF
        exit 1
    fi
}

finish_update() {
    local pre_tip=$1 pre_base=$2 push=$3

    echo "==> Commits of ours that upstream has absorbed (dropped from $MAIN)"
    local absorbed
    absorbed=$(git cherry "$UPSTREAM" "$pre_tip" "$pre_base" | sed -n 's/^- //p')
    if [ -n "$absorbed" ]; then
        git log --no-walk --format='    %h %s' $absorbed
    else
        echo "    none"
    fi

    echo "==> Checking nothing was lost"
    local status=0
    verify_nothing_lost "$pre_tip" "$pre_base" "$absorbed" || status=1

    echo "==> cli/ is byte-identical to $UPSTREAM"
    cli_matches_upstream || status=1

    echo "==> Smoke test"
    smoke || status=1

    if [ "$status" -ne 0 ]; then
        die "Stopping before push. The pre-update tip stays saved at
refs/fork-sync/$MAIN/pre-update, so the next 'update' re-runs these checks.
To return to it: git switch -C $MAIN refs/fork-sync/$MAIN/pre-update"
    fi
    git update-ref -d "refs/fork-sync/$MAIN/pre-update"
    git update-ref -d "refs/fork-sync/$MAIN/pre-update-base"

    if [ "$push" -eq 1 ]; then
        echo "==> Backing up to $FORK_REMOTE/$MAIN"
        git push --quiet --force-with-lease "$FORK_REMOTE" "$MAIN"
    fi

    echo "==> Restarting the proxy"
    restart_proxy_if_up
}

cmd_pr() {
    local branch="" dry=0 commits=() a
    for a in "$@"; do
        case "$a" in
            --dry-run) dry=1 ;;
            *) if [ -z "$branch" ]; then branch=$a; else commits+=("$a"); fi ;;
        esac
    done
    [ -n "$branch" ] && [ ${#commits[@]} -gt 0 ] \
        || die "usage: fork-sync pr <branch> <commit>... [--dry-run]"
    ! git rev-parse --verify --quiet "refs/heads/$branch" >/dev/null \
        || die "Branch $branch already exists."

    git fetch "$UPSTREAM_REMOTE" --quiet

    # Build the branch by ref updates alone. `git replay` never touches the
    # working tree, so the running install keeps serving local/main throughout.
    # On a conflict it exits non-zero and changes nothing.
    git branch --no-track "$branch" "$UPSTREAM"
    local c
    for c in "${commits[@]}"; do
        if ! git replay --advance "$branch" "$c^..$c" | git update-ref --stdin; then
            git branch -D "$branch" >/dev/null
            die "$(git log -1 --format='%h %s' "$c") does not apply cleanly to $UPSTREAM.
It depends on something else of ours; include that commit too, or build the
branch by hand in a scratch checkout. Nothing was changed."
        fi
    done

    echo "==> $branch"
    git log --format='    %h %s' "$UPSTREAM..$branch"
    git diff --stat "$UPSTREAM" "$branch" | sed 's/^/   /'

    # Run the tests the PR touches against the PR's own tree, extracted to a
    # temp dir -- no checkout, no worktree.
    local tests
    local tmp
    tmp=$(mktemp -d)
    CLEANUP+=("$tmp" "$tmp.base" "$tmp.pr-failed" "$tmp.base-failed")
    git archive "$branch" | tar -x -C "$tmp"
    mapfile -t tests < <(git diff --name-only "$UPSTREAM" "$branch" -- 'tests/test_*.py')
    echo "==> Testing the PR's tree against an upstream baseline"
    if [ ${#tests[@]} -gt 0 ]; then
        # Some tests fail on this machine on upstream itself (the semantic
        # search ONNX model, git subprocesses under py3.14). Only a test that
        # passes on upstream and fails on the PR is the PR's fault.
        local base="$tmp.base" base_tests=() t regressions
        mkdir -p "$base"
        git archive "$UPSTREAM" | tar -x -C "$base"
        for t in "${tests[@]}"; do [ -f "$base/$t" ] && base_tests+=("$t"); done
        failed_ids "$tmp" "${tests[@]}" > "$tmp.pr-failed"
        if [ ${#base_tests[@]} -gt 0 ]; then
            failed_ids "$base" "${base_tests[@]}" > "$tmp.base-failed"
        else
            : > "$tmp.base-failed"
        fi
        echo "    $(wc -l < "$tmp.pr-failed") failing on the PR, $(wc -l < "$tmp.base-failed") failing on upstream already"
        # A timing-sensitive test can fail once by chance, so a one-shot diff
        # is noise. Confirm each
        # candidate: it must pass twice more on upstream AND fail again on the
        # PR before it counts as the PR's regression.
        local id confirmed=()
        for id in $(comm -23 "$tmp.pr-failed" "$tmp.base-failed"); do
            if [ -f "$base/${id%%::*}" ] \
               && { [ -n "$(failed_ids "$base" "$id")" ] || [ -n "$(failed_ids "$base" "$id")" ]; }; then
                continue    # flaky on upstream too
            fi
            [ -n "$(failed_ids "$tmp" "$id")" ] && confirmed+=("$id")
        done
        regressions="${confirmed[*]:-}"
        rm -rf "$base" "$tmp.pr-failed" "$tmp.base-failed"
        if [ -n "$regressions" ]; then
            git branch -D "$branch" >/dev/null
            printf '    NEW failure: %s\n' $regressions >&2
            die "The PR breaks tests that pass on $UPSTREAM; branch removed."
        fi
        echo "    ok: no test that passes on upstream fails on the PR"
    else
        (cd "$tmp" && PYTHONPATH=src python -c "import jcodemunch_mcp.server" </dev/null) \
            || { git branch -D "$branch" >/dev/null; die "PR tree does not import; branch removed."; }
        echo "    no test files touched; import ok"
    fi

    if [ "$dry" -eq 1 ]; then
        git branch -D "$branch" >/dev/null
        echo "==> Dry run: nothing pushed, branch removed."
        return 0
    fi

    echo "==> Pushing to $FORK_REMOTE and opening the PR"
    git push --quiet -u "$FORK_REMOTE" "$branch"
    GITHUB_TOKEN="" gh pr create --repo "$UPSTREAM_REPO" --base main \
        --head "$(git remote get-url "$FORK_REMOTE" | sed -E 's#.*github.com[:/]([^/]+)/.*#\1#'):$branch" --fill
}

case "${1:-}" in
    status) cmd_status ;;
    update) shift; cmd_update "${1:-}" ;;
    pr)     shift; cmd_pr "$@" ;;
    *)      die "usage: fork-sync status | update [--no-push] | pr <branch> <commit>... [--dry-run]" ;;
esac
