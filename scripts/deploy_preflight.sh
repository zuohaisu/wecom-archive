#!/usr/bin/env bash
#
# Deployment checkout safety helpers. This file is deliberately source-only:
# deploy_server.sh uses it for direct/manual deployments, while deploy.yml
# reads this target-revision copy with `git show` before its first checkout so
# the protection applies to the deploy that introduces it as well.

# deploy_preflight_check_untracked_collisions <target-revision>
#
# Refuse a checkout when an untracked path already on disk conflicts with a
# file, symlink, submodule, or directory layout in the target tree. This is a
# read-only check of the checkout: it never reads artifact content and never
# moves, deletes, cleans, resets, or overwrites a working-tree path.
deploy_preflight_check_untracked_collisions() {
    local target_revision="$1"
    local git_bin="${DEPLOY_PREFLIGHT_GIT_BIN:-git}"
    local path_list untracked_path parent_path object_type
    local collision_found=0 collision_reported=0 any_collision=0

    if ! "$git_bin" rev-parse --verify --quiet "${target_revision}^{tree}" >/dev/null; then
        echo "ERROR: deployment preflight could not resolve target revision $target_revision; refusing to check out an unknown target." >&2
        return 1
    fi

    # Keep NUL-delimited Git paths in a temporary file rather than a shell
    # variable so unusual (including newline-containing) path names remain
    # paths, never shell input or artifact content. The file is outside the
    # checkout and is removed before this function returns.
    if ! path_list="$(mktemp /tmp/wecom-deploy-preflight.XXXXXX)"; then
        echo "ERROR: deployment preflight could not create its path-list scratch file; refusing to check out." >&2
        return 1
    fi
    if ! "$git_bin" ls-files --others --exclude-standard -z >"$path_list"; then
        rm -f -- "$path_list"
        echo "ERROR: deployment preflight could not enumerate untracked paths; refusing to check out." >&2
        return 1
    fi

    while IFS= read -r -d '' untracked_path; do
        # A path can disappear between Git's enumeration and this read-only
        # check. It no longer poses a checkout collision; a later Git checkout
        # remains the final safety backstop for any subsequent filesystem race.
        [ -e "$untracked_path" ] || [ -L "$untracked_path" ] || continue

        # An exact target object conflicts with an untracked regular file or
        # symlink. A normal directory itself is not emitted by `git ls-files
        # --others`; its children are considered below. A symlink to a
        # directory must still be treated as a path object, not a directory.
        if { [ ! -d "$untracked_path" ] || [ -L "$untracked_path" ]; } \
            && "$git_bin" cat-file -e "${target_revision}:${untracked_path}" 2>/dev/null; then
            collision_found=1
        else
            # Git also rejects hierarchy collisions: an untracked `a/b` blocks
            # a target regular file or symlink at `a`, and an untracked
            # symlink at `a` blocks target `a/b`. Inspect ancestors without
            # reading either artifact's contents; only non-tree target objects
            # make an ancestor collision.
            parent_path="$untracked_path"
            while [ "$parent_path" != "${parent_path%/*}" ]; do
                parent_path="${parent_path%/*}"
                if object_type="$("$git_bin" cat-file -t "${target_revision}:${parent_path}" 2>/dev/null)" \
                    && [ "$object_type" != "tree" ]; then
                    collision_found=1
                    break
                fi
            done
        fi

        if [ "$collision_found" -eq 1 ]; then
            if [ "$collision_reported" -eq 0 ]; then
                echo "ERROR: deployment preflight blocked checkout: untracked working-tree paths collide with the target revision ($target_revision):" >&2
                collision_reported=1
            fi
            printf '  %s\n' "$untracked_path" >&2
            collision_found=0
            any_collision=1
        fi
    done <"$path_list"

    if ! rm -f -- "$path_list"; then
        echo "ERROR: deployment preflight could not remove its path-list scratch file; refusing to check out." >&2
        return 1
    fi

    if [ "$any_collision" -eq 1 ]; then
        cat >&2 <<'EOF'
No checkout or cleanup was performed. This is an ownership-transition conflict, not a generic checkout failure.
Safe remediation (human-approved):
  1. Verify each server-only artifact is equivalent to the target repository artifact.
  2. Preserve a backup outside this checkout.
  3. Move or remove only each verified collision; do not use git clean, git reset --hard, or a forced checkout.
  4. Retry the standard CD workflow.
EOF
        return 1
    fi

    return 0
}
