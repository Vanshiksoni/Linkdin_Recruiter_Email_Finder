# API 1 - LinkedIn Recruiter Email Finder and Gmail Draft Assistant

This project searches recent LinkedIn posts for job-related keywords, extracts recruiter email IDs that are publicly written in those posts, prepares a Gmail application draft, and can send the formal application email with a resume attachment.

## What You Need First

1. Google Chrome installed.
2. A Gmail ID.
3. A resume link, preferably a Google Drive share link.
4. A local resume file path for attachment, for example:
   - `C:\Users\YOUR_USER\Downloads\Your_resume.pdf`
5. A Gmail app password for sending email.
6. Candidate details:
   - Full name
   - Gmail ID
   - Phone number
   - Resume link
   - Skills summary
7. LinkedIn account logged in inside the automation browser.
8. Keywords such as:
   - `Java Developer contract`
   - `.NET Developer contract`
   - `Python Developer hiring`

## Gmail Sending Setup

To send email with resume attachment, fill these in `.env`:

```text
GMAIL_ID=yourname@gmail.com
GMAIL_APP_PASSWORD=your_16_character_google_app_password
RESUME_FILE=C:\Users\YOUR_USER\Downloads\resume.pdf
APPLICATION_PARAGRAPH=I am interested in this opportunity and believe my skills match the requirement.
PORT=5010
```

Use a Gmail app password, not your normal Gmail password. Gmail app passwords require 2-Step Verification on the Google account.

## Browser Choice

Set this in `.env`:

```text
BROWSER=chrome
```

Use `brave`, `chrome`, or `edge`. Selenium needs a supported automation browser, so this cannot use every possible default browser.

For Brave on Windows, this usually works:

```text
BROWSER=brave
BRAVE_BINARY=C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe
BROWSER_USER_DATA_DIR=C:\Users\YOUR_USER\AppData\Local\BraveSoftware\Brave-Browser\User Data
PROFILE_DIRECTORY=Default
```

If you use your real Brave profile, close all Brave windows before starting the app. Brave locks the profile while it is already open.

When sharing the project with someone using Chrome, they can set:

```text
BROWSER=chrome
BRAVE_BINARY=
BROWSER_USER_DATA_DIR=
PROFILE_DIRECTORY=Default
```

## Important Scope

This project only extracts recruiter emails that are publicly mentioned in LinkedIn posts. It does not collect private profile contact information.

If no email is found in a post, the post is saved with status `Manual Review Needed`.

## Setup

```powershell
cd "Assignmet 1"
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open:

```text
http://127.0.0.1:5010
```

## GitHub / Contributor Setup

Do not commit `.env`. It contains personal Gmail, resume, and app-password details and is ignored by Git.

Commit `.env.example` instead. A new user should run:

```powershell
copy .env.example .env
```

Then they should edit `.env` with their own:

- `CANDIDATE_NAME`
- `GMAIL_ID`
- `PHONE`
- `RESUME_LINK`
- `RESUME_FILE`
- `GMAIL_APP_PASSWORD`, only if they want direct sending
- `BROWSER`, usually `chrome`

The app can still open without `.env`, but candidate fields will be blank and email sending will show setup messages until the user fills their own values.

## First-Time Login

1. Run the app.
2. Click `Open LinkedIn Login`.
3. Log in to LinkedIn in the Chrome window that opens.
4. Keep that browser profile for future searches.
5. Return to the app and search jobs.

## Workflow

1. Enter candidate and Gmail details.
2. Enter keywords.
3. Click `Search LinkedIn Posts`.
4. Review extracted recruiter emails.
5. Click `Open Gmail Login` once and log in to Gmail in your system default browser.
6. If you already ran Step 2 earlier, click `Load Latest Leads` to reopen the most recent saved recruiter-email CSV.
7. Edit `Application Paragraph` with your custom message.
8. Click `Open Compose in Default Browser` to compose a prepared message in Gmail, or click `Send Email` to send the formal application email through Gmail SMTP with resume attached.
9. Download/check CSV proof logs.

## Gmail Compose and Attachment Notes

The Gmail compose screen opens in your system default browser and can prefill recipient, subject, and message body, but Gmail compose URLs cannot attach a local resume file automatically. For the attachment requirement, use the `Send Email` button after setting `GMAIL_APP_PASSWORD` and `RESUME_FILE` in `.env`.

If Gmail shows a "browser may not be secure" warning, close the Selenium automation browser and open `http://127.0.0.1:5010` in your normal browser manually. Then use `Open Gmail Login` or `Open Compose in Default Browser`. Gmail opens through Windows' default app launcher instead of Selenium.

## Email Safety

The project does not store a normal Gmail password. It uses a Gmail app password from `.env` for SMTP sending and records sent email proof in `linkedin_logs/sent_email_log.csv`.
