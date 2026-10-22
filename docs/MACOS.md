# Lead Extractor on macOS

## Build or download the package

1. Download `Lead-Extractor-macos.zip` from the project's GitHub release, or build it from a checkout on macOS with `bash scripts/build_release_zip.sh macos`.
2. Extract the ZIP to a folder where you want the app to live. Keep the folder in place after setup because it contains the app's private Python environment.
3. Open the extracted folder and double-click **Start Lead Extractor.command**. The first launch creates `.venv` and installs the Python packages; later launches reuse that environment and check for package updates.
4. If macOS blocks the launcher because it came from the internet, use Finder's **Open** action from the context menu once, then confirm. Alternatively, start it from Terminal with `bash "Start Lead Extractor.command"`.
5. Keep the terminal window open while using the app. Streamlit opens the local app in your browser, usually at `http://localhost:8501`.

## Requirements and Ollama

- Python 3.11 or newer is required. Install it from [python.org](https://www.python.org/downloads/macos/) or with Homebrew (`brew install python`). The launcher detects `python3` or `python` and explains what to install if neither is suitable.
- Ollama is optional for browsing and editing saved lead records. To process sources with a local model, install and start [Ollama for macOS](https://ollama.com/download/mac), then choose a model in the app. For Ollama Cloud, configure its URL, API key, and model in the app.
- The app stores lead data in `~/LeadData` by default, separately from the package. Keep that folder when updating or replacing the app.
- **Fetch from URL** uses an installed Google Chrome or Microsoft Edge browser. Pasted text and local HTML files do not need either browser.

The package contains the app source and launcher, not Python, Ollama, or model weights.
