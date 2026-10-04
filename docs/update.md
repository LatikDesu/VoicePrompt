# Updates and removal

The installer replaces the helper environment and the installed plugin source. Noctalia settings, model cache, and optional transcript history are separate and are not reset.

## Update from source

Stop recording and copy any unsaved text you want to keep before restarting the plugin. From the updated VoicePrompt source directory:

```sh
noctalia msg plugins disable latikdesu/voice-prompt
bash scripts/install.sh
```

The installer stages and checks a new CPU helper before replacing the old one. It also replaces the installed plugin files and enables the plugin when Noctalia is running.

**CUDA and evdev packages do not survive this rebuild.** If you use either, disable the plugin again and restore the extras before resuming recording:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
noctalia msg plugins disable latikdesu/voice-prompt

# Only if you use CUDA:
uv pip install --python "$VP/helper/bin/python" nvidia-cublas-cu12 'nvidia-cudnn-cu12>=9,<10'

# Only if you use direct keyboard Hold:
uv pip install --python "$VP/helper/bin/python" 'evdev>=1.7,<2'

noctalia msg plugins enable latikdesu/voice-prompt
```

Open More → Diagnostics to check the selected profile. CUDA does not silently fall back to CPU. Use `uv pip install` to add extras, not an exact environment sync that removes unselected packages.

An explicit `helper_path` override still wins over the standard installed path. Clear a stale override in Advanced settings or update it to the environment you intend to run.

## Repair an existing installation

The retained installer reinstalls the recorded local wheel; it does not fetch a newer VoicePrompt version:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
noctalia msg plugins disable latikdesu/voice-prompt
bash "$VP/scripts/install.sh"
```

Restore CUDA/evdev extras afterward as above. `--no-plugin` rebuilds only the helper and skips both plugin-file replacement and desktop registration. Use it for helper-only repair, not to update the whole plugin or perform a first installation.

## Backup and rollback

Before updating, save the installed plugin and recovery artifacts in a new backup directory:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
BACKUP="$(mktemp -d "$HOME/voiceprompt-backup.XXXXXX")"
cp -a "$VP/plugin/voice-prompt" "$BACKUP/plugin"
cp -a "$VP/dist/helper" "$BACKUP/artifacts"
printf 'Backup: %s\n' "$BACKUP"
```

To roll back, set `BACKUP` to that saved directory. The recorded wheel name avoids guessing between retained versions. Restore the matching helper and plugin together:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
noctalia msg plugins disable latikdesu/voice-prompt
WHEEL="$(cat "$BACKUP/artifacts/helper-wheel.txt")"
uv pip install --python "$VP/helper/bin/python" --reinstall \
  --constraint "$BACKUP/artifacts/cpu-constraints.txt" "$BACKUP/artifacts/$WHEEL"
test -d "$BACKUP/plugin" && rm -rf "$VP/plugin/voice-prompt" && \
  cp -a "$BACKUP/plugin" "$VP/plugin/voice-prompt"
noctalia msg plugins enable latikdesu/voice-prompt
```

This restores code and dependency constraints, not the model cache or Noctalia settings. If the saved version uses another pinned model revision, prepare its matching model. Keep optional runtime dependencies compatible with the helper you restore.

## Uninstall

If you also want to delete transcript history, use Clear history in Diagnostics before disabling the plugin. Otherwise, history and cached models remain available for a later installation.

For the standard installer-created source, inspect its registration and remove only VoicePrompt:

```sh
VP="${XDG_DATA_HOME:-$HOME/.local/share}/voiceprompt"
noctalia msg plugins source list
noctalia msg plugins disable latikdesu/voice-prompt
noctalia msg plugins source remove voiceprompt
rm -rf "$VP"
```

Verify that `voiceprompt` points to your own `$VP/plugin` directory before removing it. If you created a differently named source, remove that name instead. Never remove a shared community source; other plugins use it.

Removing `$VP` deletes the isolated helper, installed plugin files, recovery artifacts, and installer. It does not remove `${XDG_CACHE_HOME:-$HOME/.cache}/voiceprompt/models` or Noctalia's plugin-data history. Delete those separately only when you no longer need them; history may contain private dictated text.
