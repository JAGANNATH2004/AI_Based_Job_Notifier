# AI Job Scraper & Ranker Pipeline

An end-to-end automated job scraping pipeline that fetches fresher-level AI/ML and Data Science jobs from LinkedIn and Naukri, filters them, ranks them using **Google Gemini AI**, and delivers a curated list of top roles straight to your Telegram daily.

## 🚀 Key Features

*   **Multi-Source Scraping:** Scrapes job postings autonomously from multiple platforms (LinkedIn, Naukri) tailored to specific hubs (Hyderabad, Bangalore, Chennai).
*   **Smart Filtering:** Pre-filters jobs for 0-1 years of experience and matches relevant titles (AI, ML, Data Science).
*   **Intelligent Ranking (Gemini AI):** Leverages **Google Gemini API** (`gemini-2.5-flash` / `gemini-1.5-flash`) to contextually evaluate, match against your profile, and rank the *top 9* most suitable jobs in seconds with massive context capacity.
*   **Deduplication:** Utilises an **SQLite** database to remember previously seen jobs, ensuring you are never notified about the same job post twice.
*   **Automated Scheduling:** Runs autonomously in the background using `APScheduler`. Scrapes for jobs periodically and dispatches a single, consolidated digest message at a configured time every day.
*   **Telegram Integration:** Delivers the finalized list directly to your phone via a Telegram Bot, nicely formatted for quick viewing.

## 🛠️ Tech Stack
*   **Language:** Python 3
*   **Scraping:** Playwright / Chromium
*   **AI Inference:** Google Gemini API (`gemini-2.5-flash`)
*   **Database:** SQLite (`jobs.db`)
*   **Task Scheduling:** `apscheduler`

## 📋 Prerequisites
1.  **Python 3.8+** installed.
2.  A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/).
3.  A **Telegram Bot Token** and your **Chat ID**. (Talk to `BotFather` on Telegram to create a bot).

## ⚙️ Setup & Installation

**1. Install Python dependencies:**
```bash
pip install -r requirements.txt
playwright install chromium
```

**2. Supabase PostgreSQL & Auth Setup:**
1. Open your [Supabase Dashboard](https://supabase.com/dashboard).
2. Go to the **SQL Editor** tab and execute the migration script provided in [`database/schema.sql`](file:///c:/Users/admin/Desktop/Projects/Job/database/schema.sql).
3. Copy your **Project URL**, **Anon Key**, and **Service Role Key** from **Project Settings $\rightarrow$ API**.

**3. Environment Variables Setup:**
Add the keys to your [`.env`](file:///c:/Users/admin/Desktop/Projects/Job/.env) file:
```env
# Supabase Cloud Configuration
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_ANON_KEY="your-anon-public-key"
SUPABASE_SERVICE_ROLE_KEY="your-service-role-key"

# Google Gemini API
GEMINI_API_KEY="your_gemini_api_key_here"
GEMINI_MODEL="gemini-2.5-flash"
```

## 💻 Usage

**1. Launch the Web Interface & Settings Dashboard:**
```bash
python main.py --web
```
Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. From the dashboard you can:
* Customize target **job locations** and **target roles**
* Configure and test your **Telegram bot token and chat ID**
* Set your preferred **daily scheduler report time**
* Search and rank live jobs in real time with AI evaluation

**2. Start the Automated Background Pipeline:**
```bash
python main.py
```

### What happens when you run `main.py`?
1.  **Initialization:** The local SQLite database (`jobs.db`) is set up to track unique job links.
2.  **Startup Cycle:** It immediately runs an initial job scraping cycle fetching from LinkedIn and Naukri based on your configured locations and roles.
3.  **Filtration:** Filters jobs down matching the 0-1 yr exp criteria and deduplicates them using the database.
4.  **Scheduler Backgrounding:** Sits in the background, scraping fresh jobs periodically. 
5.  **Daily Report:** At the scheduled user-specified time (e.g. 09:41 AM IST), it passes the daily cached fresh jobs to Google Gemini AI to rank the **top 9** opportunities, then blasts the curated digest via your Telegram Bot.

## 🧠 Adjusting the AI Ranking Prompt
You can tweak how Gemini evaluates and ranks jobs by modifying `prompt_template.txt` located in the `llm/` directory.

## 🔧 Troubleshooting
*   **Gemini API Key Error:** Ensure `GEMINI_API_KEY` is set in your `.env` file.
*   **0 Fresh Jobs added?** If testing, you might have already populated your database with jobs. To test a fresh run and bypass deduplication, simply delete your `jobs.db` file and run `main.py` again.
*   **Telegram Notification Failed?** Test your bot token and chat ID directly in the web dashboard via the "Test Telegram Message" button.
