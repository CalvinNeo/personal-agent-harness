# Long-term memory

Persistent, explicitly requested preferences and facts for a QQ user in one allowed conversation. Ordinary chat is not saved automatically. Only top-level `Plain` text can authorize a memory operation; forwarded/quoted message chains are not traversed.

Examples:

- `记住我喜欢喝茶`
- `以后回答简洁一点，请记住`
- `把关于饮料的记忆改成我喜欢咖啡`
- `忘记我喜欢喝茶这条记忆`
- `查看我的长期记忆`
- `/记忆 list 2`
- `清空我的长期记忆`

Clear is immediately authorized by an explicit clear request. Negation, quoted commands, capability questions and examples do not authorize mutations. The model tool is bound to the exact action and text parsed from the current request. Ambiguous update/delete targets require a more specific target; `#ID` from the list is also supported.

Each scope is SHA-256 of the exact conversation and sender identifiers. All database queries are scoped to that value. Listing is paginated so old records remain accessible. Passwords, keys, tokens, private keys and common credential formats are rejected before storage. The plugin logs only operation, hashed scope and count. Runtime SQLite files must remain outside Git.

`Main.memory_context_for_task(event, prompt="") -> str` performs synchronous read-only retrieval for normal chat or a delegated Codex task. It verifies the same QQ allowlist, sender and addressed-message conditions, searches at most 100 scoped entries using Chinese bigrams and word overlap, includes general response preferences, and returns up to 8 entries within a 6,000-character bound as labeled JSON background data. It does not grant permissions or execute stored text. No relevant records returns an empty string.

Limitations: deterministic explicit phrasing is supported; arbitrary paraphrases may need a clearer request. Keyword retrieval is not embedding-based and may miss synonyms. Secret filtering is defense in depth, not a complete secret detector. Deleting memory does not erase prior normal chat history or external backups.

Tests use fake events, fake transport and a temporary SQLite database, without QQ or model calls:

```sh
python3 plugins/long_term_memory/test_memory.py
```
