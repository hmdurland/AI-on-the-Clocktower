# Intelligence Tests

This is a minimal prototype harness for replaying authentic BOTC-style prompts against one or more models.

Current prototype features:
- Reuses the real `format_player_prompt()` from `engine.py`
- Loads scenarios from JSON
- Runs one or more model ids
- Stores prompt, raw response, and evaluation to `intel_tests/results`
- Includes a toy Slayer scenario with two days of public discussion

Example prompt-only run:

```powershell
py intel_tests\runner.py --dry-run
```

Example real run against two models:

```powershell
$env:OPENAI_API_KEY="your-key-here"
py intel_tests\runner.py --models "gpt-5-mini,gpt-5.4-mini" --repeats 3
```

Notes:
- The harness currently focuses on prompt replay and result capture.
- The first toy evaluator checks Slayer target legality and whether the expected target was chosen.
- This is intended as the core architecture, not the final interface.
