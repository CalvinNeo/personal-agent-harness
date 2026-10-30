"""Read scoped memory through the trusted plugin, never through its database."""


def with_memory_context(context, event, prompt, logger):
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return prompt
    try:
        for metadata in getter():
            instance = getattr(metadata, "star_cls", None)
            reader = getattr(instance, "memory_context_for_task", None)
            if not callable(reader):
                continue
            memory = reader(event, prompt)
            if not isinstance(memory, str) or not memory:
                return prompt
            # Never truncate the task or JSON memory mid-record. Selection is
            # already bounded by the memory plugin; skip it if the task is full.
            if len(memory) > 6000 or len(prompt) + len(memory) > 12000:
                logger.info("Codex memory context omitted reason=prompt_budget")
                return prompt
            return prompt + memory
    except Exception as error:
        logger.warning("Codex memory context unavailable type=%s", type(error).__name__)
    return prompt
