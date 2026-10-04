#!/usr/bin/env bash
# VoicePrompt local installer: isolated user helper environment and pinned CPU-small model.
#
# Runs only from an explicit local checkout or from the installed recovery copy. It never
# uses sudo, the system Python/pip, or curl|sh, and it never edits desktop configuration.
# Layout produced under $XDG_DATA_HOME/voiceprompt:
#   helper/bin/voiceprompt-helper   isolated uv environment (relocatable)
#   plugin/voice-prompt             plugin source registered with the running shell
#   dist/helper/                    wheel + recorded selection + CPU constraints for repair
#   scripts/install.sh              this installer, for recovery without the checkout
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
INSTALL_ROOT="$DATA_HOME/voiceprompt"
PLUGIN_SOURCE_NAME="voiceprompt"
PLUGIN_ID="latikdesu/voice-prompt"

INSTALL_PLUGIN=1
if [ "${1:-}" = "--no-plugin" ]; then
    INSTALL_PLUGIN=0
fi

say() { printf '%s\n' "$*"; }
fail() { printf 'install-error: %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 \
    || fail "uv is required (https://docs.astral.sh/uv/); no system pip or sudo is used"

mkdir -p "$INSTALL_ROOT"
STAGING="$(mktemp -d "$INSTALL_ROOT/.install-staging-XXXXXX")"
cleanup() { rm -rf "$STAGING"; }
trap cleanup EXIT

# --- Select the exact helper wheel and CPU constraints (build from source only when needed) ---
# The wheel is chosen from the version declared by the source metadata or from the retained
# recovery record. It is never guessed from glob (lexical) order, modification time or the
# "newest" file: an ambiguous or missing selection fails instead of silently changing version.
WHEEL_RECORD_NAME="helper-wheel.txt"

wheel_for_version() { # $1 directory  $2 version -> single matching wheel path on stdout, else nothing
    local matches
    matches=("$1"/voiceprompt_helper-"$2"-*.whl)
    if [ -e "${matches[0]}" ] && [ "${#matches[@]}" -eq 1 ]; then
        printf '%s' "${matches[0]}"
    fi
}

source_version() { # $1 pyproject.toml -> the single top-level project version on stdout, else nothing
    local declared
    declared="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*$/\1/p' "$1")"
    case "$declared" in
        ""|*$'\n'*) return 1 ;;
    esac
    printf '%s' "$declared"
}

if [ -f "$SOURCE_ROOT/helper/pyproject.toml" ]; then
    expected_version="$(source_version "$SOURCE_ROOT/helper/pyproject.toml")" \
        || fail "helper/pyproject.toml must declare exactly one top-level version"
    repo_constraints="$SOURCE_ROOT/dist/helper/cpu-constraints.txt"
    needs_build=1
    if WHEEL="$(wheel_for_version "$SOURCE_ROOT/dist/helper" "$expected_version")" \
        && [ -n "$WHEEL" ] && [ -f "$repo_constraints" ]; then
        needs_build=0
        for input in "$SOURCE_ROOT/helper/src/voiceprompt/"*.py "$SOURCE_ROOT/helper/pyproject.toml" "$SOURCE_ROOT/helper/uv.lock"; do
            if [ "$input" -nt "$WHEEL" ]; then needs_build=1; break; fi
        done
    fi
    if [ "$needs_build" = 1 ]; then
        say "Building helper wheel and CPU constraints from the local checkout..."
        uv build --project "$SOURCE_ROOT/helper" --out-dir "$STAGING/dist"
        uv export --project "$SOURCE_ROOT/helper" --frozen --no-dev --no-emit-project \
            --format requirements-txt --output-file "$STAGING/dist/cpu-constraints.txt"
        built=("$STAGING/dist"/voiceprompt_helper-*.whl)
        if [ ! -e "${built[0]}" ] || [ "${#built[@]}" -ne 1 ]; then
            fail "the helper build did not produce exactly one wheel under $STAGING/dist"
        fi
        WHEEL="${built[0]}"
        CONSTRAINTS="$STAGING/dist/cpu-constraints.txt"
    else
        CONSTRAINTS="$repo_constraints"
    fi
    PLUGIN_SRC="$SOURCE_ROOT/plugin/voice-prompt"
else
    CONSTRAINTS="$INSTALL_ROOT/dist/helper/cpu-constraints.txt"
    [ -f "$CONSTRAINTS" ] || fail "no installed CPU constraints under $INSTALL_ROOT/dist/helper"
    wheel_record="$INSTALL_ROOT/dist/helper/$WHEEL_RECORD_NAME"
    if [ -f "$wheel_record" ]; then
        recorded="$(<"$wheel_record")"
        case "$recorded" in
            voiceprompt_helper-*.whl) ;;
            *) fail "the recorded helper wheel name is invalid: $recorded" ;;
        esac
        [ -f "$INSTALL_ROOT/dist/helper/$recorded" ] \
            || fail "the recorded helper wheel $recorded is missing; refusing to substitute another version"
        WHEEL="$INSTALL_ROOT/dist/helper/$recorded"
    else
        installed_wheels=("$INSTALL_ROOT/dist/helper"/voiceprompt_helper-*.whl)
        [ -e "${installed_wheels[0]}" ] || fail "no installed helper wheel under $INSTALL_ROOT/dist/helper"
        [ "${#installed_wheels[@]}" -eq 1 ] \
            || fail "multiple installed helper wheels are present without a recorded selection; refusing to guess"
        WHEEL="${installed_wheels[0]}"
    fi
    PLUGIN_SRC="$INSTALL_ROOT/plugin/voice-prompt"
fi
[ -f "$WHEEL" ] || fail "helper wheel not found"
[ -f "$CONSTRAINTS" ] || fail "CPU constraints not found"

# --- Stage a fresh isolated environment; the working one is untouched until it succeeds ---
say "Creating isolated helper environment (Python >=3.13,<3.14)..."
uv venv --python 3.13 --relocatable "$STAGING/helper"
uv pip install --python "$STAGING/helper/bin/python" --constraint "$CONSTRAINTS" "$WHEEL"
"$STAGING/helper/bin/voiceprompt-helper" version >/dev/null \
    || fail "the staged helper executable did not start"

say "Preparing the pinned CPU-small model and smoke-loading it (downloads once if absent)..."
if ! "$STAGING/helper/bin/voiceprompt-helper" setup --device cpu --model-profile cpu-small --json; then
    fail "model setup or smoke load failed; the previous helper and model were left untouched"
fi

if [ "$INSTALL_PLUGIN" = 1 ]; then
    [ -d "$PLUGIN_SRC" ] || fail "plugin source not found at $PLUGIN_SRC"
    mkdir -p "$STAGING/plugin"
    cp -a "$PLUGIN_SRC" "$STAGING/plugin/voice-prompt"
fi

# --- Replace the staged environment/plugin only after everything above succeeded ---
swap_dir() { # $1 source  $2 destination  $3 backup
    local src="$1" dst="$2" backup="$3"
    rm -rf "$backup"
    if [ -e "$dst" ]; then mv "$dst" "$backup"; fi
    if ! mv "$src" "$dst"; then
        if [ -e "$backup" ]; then mv "$backup" "$dst"; fi
        return 1
    fi
    rm -rf "$backup"
}

swap_dir "$STAGING/helper" "$INSTALL_ROOT/helper" "$STAGING/helper-replaced" \
    || fail "could not replace the helper environment"

if [ "$INSTALL_PLUGIN" = 1 ]; then
    mkdir -p "$INSTALL_ROOT/plugin"
    swap_dir "$STAGING/plugin/voice-prompt" "$INSTALL_ROOT/plugin/voice-prompt" "$STAGING/plugin-replaced" \
        || fail "could not replace the plugin source"
fi

# --- Persist recovery assets so a later repair needs no checkout or PATH override ---
mkdir -p "$INSTALL_ROOT/dist/helper" "$INSTALL_ROOT/scripts"
wheel_name="$(basename "$WHEEL")"
wheel_target="$INSTALL_ROOT/dist/helper/$wheel_name"
constraints_target="$INSTALL_ROOT/dist/helper/cpu-constraints.txt"
if [ "$(realpath -m "$WHEEL")" != "$(realpath -m "$wheel_target")" ]; then
    cp -f "$WHEEL" "$wheel_target"
fi
if [ "$(realpath -m "$CONSTRAINTS")" != "$(realpath -m "$constraints_target")" ]; then
    cp -f "$CONSTRAINTS" "$constraints_target"
fi
# Recovery must never pick an older/superseded version by accident: record the exact wheel
# this installation uses so a later recovery selects it explicitly. Previously retained
# wheels are kept for rollback; only a new selection is added, never deleted here.
printf '%s\n' "$wheel_name" > "$INSTALL_ROOT/dist/helper/$WHEEL_RECORD_NAME"
script_target="$INSTALL_ROOT/scripts/install.sh"
if [ "$(realpath -m "$SCRIPT_DIR/install.sh")" != "$(realpath -m "$script_target")" ]; then
    cp -f "$SCRIPT_DIR/install.sh" "$script_target"
fi
chmod +x "$script_target"

# --- Desktop hookup only against a running shell; failure never claims success ---
if [ "$INSTALL_PLUGIN" = 1 ]; then
    if command -v noctalia >/dev/null 2>&1 && noctalia msg plugins list >/dev/null 2>&1; then
        say "Registering the plugin source and enabling the plugin in the running shell..."
        sources="$(noctalia msg plugins source list)" || fail "could not inspect existing plugin sources"
        registered=0
        while read -r name kind location; do
            if [ "$name" = "$PLUGIN_SOURCE_NAME" ]; then
                if [ "$kind" != path ] || [ "$location" != "$INSTALL_ROOT/plugin" ]; then
                    say "Source $PLUGIN_SOURCE_NAME already belongs to another path; it was not changed."
                    exit 3
                fi
                registered=1
            fi
        done <<< "$sources"
        if [ "$registered" = 0 ] && ! noctalia msg plugins source add "$PLUGIN_SOURCE_NAME" path "$INSTALL_ROOT/plugin"; then
            say "Helper and model are installed, but registering the plugin source failed. Existing sources were not removed."
            exit 3
        fi
        if ! noctalia msg plugins enable "$PLUGIN_ID"; then
            say "Helper and model are installed, but enabling the plugin failed."
            say "Run manually: noctalia msg plugins enable $PLUGIN_ID"
            exit 3
        fi
        say "Plugin source registered and plugin enabled."
    else
        say "No running Noctalia shell detected; desktop hookup was not performed."
        say "When Noctalia is running, run:"
        say "  noctalia msg plugins source add $PLUGIN_SOURCE_NAME path $INSTALL_ROOT/plugin"
        say "  noctalia msg plugins enable $PLUGIN_ID"
    fi
fi

say ""
say "VoicePrompt helper: $INSTALL_ROOT/helper/bin/voiceprompt-helper"
say "Plugin source:      $INSTALL_ROOT/plugin"
say "Recovery assets:    $INSTALL_ROOT/dist/helper, $INSTALL_ROOT/scripts/install.sh"
say "Model:              $CACHE_HOME/voiceprompt/models/cpu-small"
