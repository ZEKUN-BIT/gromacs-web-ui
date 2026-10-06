#!/usr/bin/env bash
# PAYLOAD_WINDOWS_B64 is supplied by Console.ps1, never interpolated as shell code.
set -euo pipefail
umask 077
[[ "$(id -un)" == gromacs-console ]] || { echo 'Run as the dedicated application user.' >&2; exit 1; }
app_root="$HOME/.local/share/gromacs-console"
mkdir -p "$app_root"
exec 9>>"$app_root/install.lock"
if flock --nonblock --conflict-exit-code 75 9; then
    :
else
    lock_result=$?
    if (( lock_result != 75 )); then
        echo 'Unable to acquire the installation lock.' >&2
        exit "$lock_result"
    fi
    echo 'Another installation is running. Waiting up to 60 seconds for it to finish...' >&2
    if flock --timeout 60 --conflict-exit-code 75 9; then
        :
    else
        lock_result=$?
        if (( lock_result == 75 )); then
            echo 'Installation is still busy after 60 seconds (exit 75). Let the current installation finish, then run Configure or Update again. Existing files were retained.' >&2
        else
            echo 'Unable to acquire the installation lock.' >&2
        fi
        exit "$lock_result"
    fi
fi
# The supervising shell owns the lock. Installers and any descendants receive
# no copy of its descriptor, so a background child cannot retain it after exit.
(
exec 9>&-
payload_windows=$(printf '%s' "$PAYLOAD_WINDOWS_B64" | base64 --decode)
payload_dir=$(wslpath -u "$payload_windows")
cd "$payload_dir"
sha256sum --check application.sha256
updater="$payload_dir/application_update.py"
if [[ -f "$app_root/.application-update.json" ]]; then
    # Recovery changes program files too. Use the packaged stdlib-only manager
    # so a damaged virtual environment cannot bypass the active-job guard.
    python3 "$payload_dir/desktop_service.py" stop --app-root "$app_root"
fi
python3 "$updater" --app-root "$app_root" --action recover
app_only=${APP_ONLY:-0}
if [[ "$app_only" == 1 ]]; then
    # An application-only update must prove that the retained environment is
    # healthy. Exit before stopping the service so Windows can run full repair.
    if [[ ! -x "$app_root/.venv/bin/python" ]] ||
        ! python3 "$updater" --app-root "$app_root" --action check-runtime ||
        ! "$app_root/.venv/bin/python" "$payload_dir/dependency_setup.py" --app-root "$app_root" --payload-dir "$payload_dir" --check-ready; then
        echo 'Application-only update needs a full environment check (exit 76).' >&2
        exit 76
    fi
fi
space_args=()
[[ "$app_only" != 1 ]] || space_args+=(--app-only)
python3 "$updater" --app-root "$app_root" --payload-dir "$payload_dir" --action check-space "${space_args[@]}" --backend "${SETUP_BACKEND:-CUDA}"
build_dir=$(mktemp -d "$app_root/build.XXXXXXXX")
cleanup_build() {
    if [[ -f "$app_root/.application-update.json" ]]; then
        echo "Update recovery needs attention; backups retained in $build_dir." >&2
    else
        rm -rf -- "$build_dir"
    fi
}
trap cleanup_build EXIT
tar --extract --gzip --file application.tar.gz --directory "$build_dir" --no-same-owner
python3 "$updater" --app-root "$app_root" --payload-dir "$payload_dir" --stage "$build_dir" --action prepare
gmx_prefix="$app_root/gromacs"
if [[ "$app_only" != 1 ]]; then
python3 "$payload_dir/desktop_service.py" stop --app-root "$app_root"
echo '[2/4] Detecting GPU support and preparing GROMACS...'
gpu_result=0
python3 "$payload_dir/gpu_setup.py" --prefix "$gmx_prefix" --work-dir "$build_dir" || gpu_result=$?
case "$gpu_result" in
    0) echo 'GPU GROMACS is ready; the CUDA compiler toolkit was not installed.' ;;
    10) echo 'No compatible CUDA GPU is available; preparing or reusing CPU computation.'
        python3 "$payload_dir/gpu_setup.py" --cpu --prefix "$gmx_prefix" --work-dir "$build_dir" ;;
    *) echo 'GPU setup failed. The previous GROMACS installation and simulation results were retained.' >&2; exit "$gpu_result" ;;
esac
"$gmx_prefix/bin/gmx" --version
[[ -d "$gmx_prefix/share/gromacs/top/amber19sb.ff" ]] || { echo 'Missing amber19sb force field.' >&2; exit 1; }
touch "$gmx_prefix/.ready"
if [[ ! -x "$app_root/.venv/bin/python" ]]; then python3 -m venv "$app_root/.venv"; fi
echo '[3/4] Checking Python dependencies, Open Babel, AmberTools and DSSP...'
"$app_root/.venv/bin/python" "$payload_dir/dependency_setup.py" --app-root "$app_root" --payload-dir "$payload_dir"
else
    echo 'Application-only update: reusing the verified computation environment; skipping package setup.'
fi
# Keep settings, results and the virtual environment across updates.
# Staging is complete. Stop only now for application-only updates, repeating
# the active-job check immediately before promotion under the installation lock.
python3 "$payload_dir/desktop_service.py" stop --app-root "$app_root"
echo '[4/4] Updating the application files...'
python3 "$updater" --app-root "$app_root" --stage "$build_dir" --action promote
echo 'Installation complete. Temporary download archives and verification files will now be removed.'
)
