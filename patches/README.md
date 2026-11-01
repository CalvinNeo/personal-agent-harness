# Installed package customization inventory

Baseline: AstrBot 4.29.3, compared byte-for-byte with the original `astrbot-4.29.3-py3-none-any.whl` on the deployment host.

| File | Custom behavior | Reason |
| --- | --- | --- |
| `astrbot/core/provider/sources/openai_source.py` | Replaces the logged API-key prefix with a fixed redacted message when retrying rate-limit errors. | Prevents partial credential disclosure in application logs. |
| `astrbot/core/tools/web_search_tools.py` | Converts the AnySearch region value `global` into `intl`. | Preserves callers' `global` choice while sending the region value accepted by AnySearch. |
| `astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py` and `qqofficial_platform_adapter.py` | Records the message IDs returned by QQ for each sent chain. | Lets the Codex bridge bind a quoted bot reply to the exact task, including split text/image sends. |

| `astrbot/core/utils/media_utils.py` and new `public_image_download.py` | Routes allowlisted Wikimedia originals through an optional loopback proxy, with URL, redirect, byte and content-type checks. | Supports public reference images without changing global networking or other downloads. |

The cumulative patch covers six files, including one new helper. The QQ message-event changes also redact upload failure payloads so a failed QR-code upload is not written verbatim to application logs. The helper is mirrored in `astrbot-public-images/` for isolated tests.

The installed QQ SDK is `qq-botpy==1.2.1`. Every SDK Python file with a hash in its distribution `RECORD` matches that hash; no SDK Python modifications were found. The two QQ Official adapter files above are AstrBot files, not SDK files, and are covered by the manifest.

`manifest.json` records original and patched SHA-256 hashes. A null original hash means that the new helper must not exist in the baseline. Use the guarded installer, not an unverified copy of installed packages:

```sh
python3 scripts/apply_patches.py --site-packages /path/to/site-packages --check
python3 scripts/apply_patches.py --site-packages /path/to/site-packages
```

The installer accepts either the exact unmodified baseline or the complete patched state. Reapplying the complete patch is a no-op. Unknown, symlinked and mixed old/new sources are rejected. When upgrading from an earlier subset, stop AstrBot, back up the environment and restore the pinned wheel's affected sources before applying the cumulative patch. Do not restore application data or overwrite current databases during this process.

The public-image proxy is optional and restricted to a loopback endpoint. The sample systemd drop-in uses the existing local proxy only; no subscription, account or upstream credential is included. The separate QQ upload-log script remains available for checking older deployments, but its change is already included in this cumulative patch.

Apply this patch only to the pinned AstrBot version, from the Python environment's `site-packages` directory. Recheck and port it when upgrading AstrBot. Do not copy installed configurations, databases, reminder records, cached images, or logs into a source repository.
