# Strava Training Coach

Personal AI training coach that connects to your Strava account and lets you ask questions about your training using Claude.

## Features

- Fetches your last 30 days of Strava activities and injects them as context
- Personal knowledge base — edit markdown files to tell your coach about your goals, injuries, schedule, etc.
- Prompt caching so repeated questions in a session are fast and cheap
- Type `refresh` mid-session to reload fresh Strava data without restarting
- Supports runs, rides, swims, and all other Strava sport types

## Setup

### 1. Get Strava API credentials

1. Go to [https://www.strava.com/settings/api](https://www.strava.com/settings/api)
2. Create an application (name/website can be anything, e.g. "My Training Coach")
3. Set **Authorization Callback Domain** to `localhost`
4. Copy your **Client ID** and **Client Secret**

### 2. Get your Anthropic API key

Create a key at [https://console.anthropic.com/](https://console.anthropic.com/)

### 3. Install dependencies

```bash
cd strava-coach
pip install -r requirements.txt
```

### 4. Configure credentials

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

### 5. Fill in your personal knowledge base

Edit `knowledge_base/about_me.md` with your athletic profile, goals, injuries, schedule, etc. The more context you give, the better the coaching.

### 6. Run

```bash
python coach.py
```

On first run, your browser will open for Strava authorization. Approve it and return to the terminal.

## Usage

```
=== Strava Training Coach ===
Fetching recent Strava activities...
Athlete: John Doe
Loaded 18 activities from the last 30 days.

Your Strava Training Coach is ready!
Type 'refresh' to reload Strava data
Type 'quit' to exit

You: How has my running volume trended this month?

Coach: Looking at your last 30 days, you've done 12 runs totaling 87 km...
```

**Commands:**
- `refresh` — re-fetch Strava data (useful after a recent workout syncs)
- `quit` / `exit` / `q` — exit

## Project Structure

```
strava-coach/
├── coach.py              # Main chatbot loop
├── strava/
│   ├── auth.py           # OAuth2 flow + token management
│   └── client.py         # Strava API client
├── knowledge_base/
│   └── about_me.md       # Your personal context (edit this!)
├── .tokens.json          # Auto-created after first auth (gitignore this)
├── .env                  # Your secrets (gitignore this)
├── .env.example
└── requirements.txt
```

## Privacy

- Your Strava tokens are stored locally in `.tokens.json` — never shared
- Your training data is sent to Anthropic's API to power the AI responses (same as using Claude.ai)
- Add `.tokens.json` and `.env` to your `.gitignore` if you track this project in git
