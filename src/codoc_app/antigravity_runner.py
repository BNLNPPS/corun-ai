"""Antigravity CLI command construction for Google model jobs."""


ANTIGRAVITY_MODEL_MAP = {
    'gemini-3.5-flash-low': 'Gemini 3.5 Flash (Low)',
    'gemini-3.5-flash-medium': 'Gemini 3.5 Flash (Medium)',
    'gemini-3.5-flash-high': 'Gemini 3.5 Flash (High)',
    'gemini-3.1-pro-low': 'Gemini 3.1 Pro (Low)',
    'gemini-3.1-pro-high': 'Gemini 3.1 Pro (High)',
    # Backward-compatible aliases for saved definitions that predate the
    # Gemini CLI consumer-tier shutdown.
    'gemini-2.5-flash': 'Gemini 3.5 Flash (High)',
    'gemini-2.5-pro': 'Gemini 3.1 Pro (High)',
}


def antigravity_model_name(model):
    """Return the Antigravity display model name for a corun model id."""
    try:
        return ANTIGRAVITY_MODEL_MAP[model]
    except KeyError as exc:
        raise ValueError(f'unsupported Antigravity model: {model}') from exc


def build_antigravity_command(antigravity_path, model, prompt,
                              timeout_s=3600):
    """Build a non-interactive Antigravity print-mode command."""
    return [
        antigravity_path,
        '--sandbox',
        '--dangerously-skip-permissions',
        '--model', antigravity_model_name(model),
        '--print', prompt,
        '--print-timeout', f'{int(timeout_s)}s',
    ]
