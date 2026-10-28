# Installed package customization inventory

Baseline: AstrBot 4.29.3, compared byte-for-byte with the original `astrbot-4.29.3-py3-none-any.whl` on the deployment host.

| File | Custom behavior | Reason |
| --- | --- | --- |
| `astrbot/core/provider/sources/openai_source.py` | Replaces the logged API-key prefix with a fixed redacted message when retrying rate-limit errors. | Prevents partial credential disclosure in application logs. |
| `astrbot/core/tools/web_search_tools.py` | Converts the AnySearch region value `global` into `intl`. | Preserves callers' `global` choice while sending the region value accepted by AnySearch. |

These are the only installed AstrBot Python files that differ from the original wheel. The original wheel provides exact baselines for both files. The existing pre-redaction backup of `openai_source.py` also matches its original wheel member.

The installed QQ SDK is `qq-botpy==1.2.1`. Every SDK Python file with a hash in its distribution `RECORD` matches that hash; no SDK Python modifications were found.

`astrbot-4.29.3-customizations.patch` contains both source changes. It applies successfully with `patch --dry-run --batch -p1` to files freshly extracted from the original wheel. The patch was checked for credential literals, public IP addresses, private identifiers, and email addresses, with no matches found.

Apply this patch only to the pinned AstrBot version, from the Python environment's `site-packages` directory. Recheck and port it when upgrading AstrBot. Do not copy installed configurations, databases, reminder records, cached images, or logs into a source repository.
