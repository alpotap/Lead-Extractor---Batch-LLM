# Lead Extractor on Linux

## Build or download the package

1. Download `Lead-Extractor-linux.zip` from the project's GitHub release, or build it from a checkout on Linux with `bash scripts/build_release_zip.sh linux`.
2. Extract the ZIP to a folder where you want the app to live. Keep the folder in place after setup because it contains the app's private Python environment.
3. Run **Start Lead Extractor.sh** from your file manager. If your desktop does not offer **Run**, open a terminal in the extracted folder and run `bash "Start Lead Extractor.sh"`. You can also run `bash scripts/start_lead_extractor.sh`.
4. The first launch creates `.venv` and installs the Python packages; later launches reuse that environment and check for package updates.
5. Keep the terminal window open while using the app. Streamlit opens the local app in your browser, usually at `http://localhost:8501`.

## Requirements and Ollama

- Python 3.11 or newer is required. Install Python 3 and its venv support with your distribution's package manager if needed. For example, on Debian or Ubuntu run `sudo apt install python3 python3-venv python3-pip`. The launcher detects `python3` or `python` and explains what to install if neither is suitable.
- Ollama is optional for browsing and editing saved lead records. To process sources with a local model, install and start [Ollama for Linux](https://ollama.com/download/linux), then choose a model in the app. For Ollama Cloud, configure its URL, API key, and model in the app.
- The app stores lead data in `~/LeadData` by default, separately from the package. Keep that folder when updating or replacing the app.
- **Fetch from URL** uses an installed Google Chrome or Microsoft Edge browser. Pasted text and local HTML files do not need either browser.

The package contains the app source and launcher, not Python, Ollama, or model weights.
