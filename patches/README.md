# Installed package customization inventory

Baseline: AstrBot 4.29.3, compared byte-for-byte with the original `astrbot-4.29.3-py3-none-any.whl` on the deployment host.

| File | Custom behavior | Reason |
| --- | --- | --- |
| `astrbot/core/provider/sources/openai_source.py` | Replaces the logged API-key prefix with a fixed redacted message when retrying rate-limit errors. | Prevents partial credential disclosure in application logs. |
| `astrbot/core/tools/web_search_tools.py` | Converts the AnySearch region value `global` into `intl`. | Preserves callers' `global` choice while sending the region value accepted by AnySearch. |
| `astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py` and `qqofficial_platform_adapter.py` | Records the message IDs returned by QQ for each sent chain. | Lets the Codex bridge bind a quoted bot reply to the exact task, including split text/image sends. |

These are the only installed AstrBot Python files that differ from the original wheel. The original wheel provides exact baselines for both files. The existing pre-redaction backup of `openai_source.py` also matches its original wheel member.

The installed QQ SDK is `qq-botpy==1.2.1`. Every SDK Python file with a hash in its distribution `RECORD` matches that hash; no SDK Python modifications were found. The two QQ Official adapter files above are AstrBot files, not SDK files, and are covered by the manifest.

`astrbot-4.29.3-customizations.patch` contains all four source changes. It applies successfully with `patch --dry-run --batch -p1` to the pinned AstrBot source tree. The patch was checked for credential literals, public IP addresses, private identifiers, and email addresses, with no matches found.

Apply this patch only to the pinned AstrBot version, from the Python environment's `site-packages` directory. Recheck and port it when upgrading AstrBot. Do not copy installed configurations, databases, reminder records, cached images, or logs into a source repository.
