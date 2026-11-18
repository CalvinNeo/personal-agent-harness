# Installed package customization inventory

Baseline: AstrBot 4.29.3, compared byte-for-byte with the original `astrbot-4.29.3-py3-none-any.whl` on the deployment host.

| File | Custom behavior | Reason |
| --- | --- | --- |
| `astrbot/core/provider/sources/openai_source.py` | Replaces the logged API-key prefix with a fixed redacted message when retrying rate-limit errors. | Prevents partial credential disclosure in application logs. |
| `astrbot/core/tools/web_search_tools.py` | Converts the AnySearch region value `global` into `intl`. | Preserves callers' `global` choice while sending the region value accepted by AnySearch. |
| `astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py` and `qqofficial_platform_adapter.py` | Records the message IDs returned by QQ for each sent chain. | Lets the Codex bridge bind a quoted bot reply to the exact task, including split text/image sends. |
| `qqofficial_platform_adapter.py` and new `qqofficial/text_mentions.py` | Converts an explicitly configured leading text-mention alias into an At component and removes the alias from both message text representations. | Lets delivered ordinary group messages reach existing wake checks and plugins without relaxing their access controls. |
| `astrbot/core/pipeline/preprocess_stage/stage.py`, new `voice_input.py`, `waking_check/stage.py` and QQ adapter | Defers QQ audio download until access checks, transcribes offline, echoes the transcript and then rematches existing handlers. | Adds optional voice input without changing sender permissions. See [VOICE](../docs/VOICE.md). |
| `astrbot/core/utils/media_utils.py` and new `public_image_download.py` | Routes allowlisted Wikimedia originals through an optional loopback proxy, with URL, redirect, byte and content-type checks. | Supports public reference images without changing global networking or other downloads. |

The cumulative patch covers ten files: seven existing files and three new helpers. The QQ message-event changes also redact upload failure payloads so a failed QR-code upload is not written verbatim to application logs. The helpers are mirrored in `astrbot-public-images/`, `astrbot-qq-mentions/` and `astrbot-voice/` for isolated tests. Public helper names and paths are portable; patched hashes describe these repository sources rather than a private deployment copy.

The installed QQ SDK is `qq-botpy==1.2.1`. Every SDK Python file with a hash in its distribution `RECORD` matches that hash; no SDK Python modifications were found. The two QQ Official adapter files above are AstrBot files, not SDK files, and are covered by the manifest.

`manifest.json` records original and patched SHA-256 hashes. A null original hash means that the new helper must not exist in the baseline. Use the guarded installer, not an unverified copy of installed packages:

```sh
python3 scripts/apply_patches.py --site-packages /path/to/site-packages --check
python3 scripts/apply_patches.py --site-packages /path/to/site-packages
```

The installer accepts either the exact unmodified baseline or the complete patched state. Reapplying the complete patch is a no-op. Unknown, symlinked and mixed old/new sources are rejected. When upgrading from an earlier subset, stop AstrBot, back up the environment and restore the pinned wheel's affected sources before applying the cumulative patch. Do not restore application data or overwrite current databases during this process.

The public-image proxy is optional and restricted to a loopback endpoint. The sample systemd drop-in uses the existing local proxy only; no subscription, account or upstream credential is included. The separate QQ upload-log script remains available for checking older deployments, but its change is already included in this cumulative patch.

## Optional text-mention compatibility

Set `text_mention_aliases` on the individual `qq_official` platform entry in the private AstrBot configuration, for example `"text_mention_aliases": ["示例智能体"]`, then restart AstrBot. The initializer defaults to an empty list: private bot names are never distributed as public defaults. Use names without the `@` sign.

For group events that QQ actually delivers, `@示例智能体 你好` and `＠示例智能体：你好` become a normal bot mention plus `你好` before waking, whitelist checks and plugins. A name must be at the beginning of the top-level message and end at whitespace, a colon, a comma or the end of the message. Similar names, mentions of other people, middle-of-message names and quoted/forwarded content do not enable this compatibility path. Native mentions, private chats, plugin sender checks and the group whitelist retain their existing behavior. An empty mention follows AstrBot's existing waiting-for-content policy.

Typing an `@` name does not create a native QQ mention. The public QQ bot API may not deliver an ordinary group message at all; this patch cannot change QQ's permissions or recover an undelivered message. Selecting the bot from QQ's mention picker remains the reliable entry point. Do not enable private-guild message intents as a workaround for ordinary QQ group messages.

Apply this patch only to the pinned AstrBot version, from the Python environment's `site-packages` directory. Recheck and port it when upgrading AstrBot. Do not copy installed configurations, databases, reminder records, cached images, or logs into a source repository.

## Optional voice input

The voice helper defaults to the standard local state directory and socket; `ASTRBOT_VOICE_STATE_DIRECTORY` and `ASTRBOT_VOICE_SOCKET` can override them. Voice recognition is disabled until a local `config.json` explicitly enables it. The optional one-shot worker, model setup, resource limits and synthetic tests are documented in [VOICE](../docs/VOICE.md). No model weights or user audio are included.
