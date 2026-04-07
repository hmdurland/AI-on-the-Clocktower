# AI-on-the-Clocktower v1.5

Browser-playable Blood on the Clocktower v1.5 simulation with LLM players, plus an intelligence-test harness for replaying authentic decision scenarios.

## Requirements

- Python 3.10+
- An OpenAI API key and/or Gemini API key

## Quickstart ("Just tell me how to play this")

1. On the GitHub page for this project, click the green `Code` button, then click `Download ZIP`.
2. Find the ZIP file on your computer and unzip it somewhere easy to find, such as your Desktop.
3. Open the unzipped folder.
4. Click the folder path/address bar at the top of the File Explorer window, type `powershell`, and press Enter.
5. A PowerShell window should open already pointed at this folder.
6. Install the required packages:

```powershell
py -m pip install -r requirements.txt
```

7. Add your OpenAI API key:

```powershell
$env:OPENAI_API_KEY="your-key-here"
```

8. Start the game in your browser:

```powershell
py -m streamlit run app.py
```

9. Your browser should open automatically. If it does not, look in the PowerShell window for a local web address such as `http://localhost:8501` and open that in your browser.

If `py` does not work on your machine, replace it with `python`.

## What is Included

- `app.py`: Streamlit web UI for running games locally in a browser
- `engine.py`: game engine and LLM integration
- `intel_tests/`: scenario replay harness for intelligence tests

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
