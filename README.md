# AI-on-the-Clocktower v1.5

Browser-playable Blood on the Clocktower v1.5 simulation with LLM players, plus an intelligence-test harness for replaying authentic decision scenarios.

## What is Included

- `app.py`: Streamlit web UI for running games locally in a browser
- `engine.py`: game engine and LLM integration
- `intel_tests/`: scenario replay harness for intelligence tests

## Requirements

- Python 3.10+
- An OpenAI API key and/or Gemini API key

Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

## Run Gameplay UI

Set your API key in PowerShell:

```powershell
$env:OPENAI_API_KEY="your-key-here"
```

Then launch the app:

```powershell
py -m streamlit run app.py
```

## Run Intelligence Tests

Example dry run:

```powershell
py intel_tests\runner.py --dry-run
```

Example real run:

```powershell
$env:OPENAI_API_KEY="your-key-here"
py intel_tests\runner.py --models "gpt-5-mini,gpt-5.4-mini" --repeats 3
```

## Notes

- API keys are read from environment variables or entered in the Streamlit UI.
- Game logs and intelligence-test results are generated locally and are ignored by `.gitignore`.
- This repo contains the current local code, not historical generated logs.
