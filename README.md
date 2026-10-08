# Surya Writes Wisdom

A personal blog with public article pages and a private author dashboard for
importing Google Docs. Imported article formatting is sanitized before display.

## Run locally on Windows

Open PowerShell in this folder and run:

```powershell
python app.py
```

Edit `.env`:

- `FLASK_SECRET_KEY`: set a long, random secret.
- `ADMIN_EMAIL`: the Google account that will manage this blog.
- `GOOGLE_CLIENT_SECRETS_FILE`: path to the OAuth client JSON downloaded from Google Cloud.
- `GOOGLE_CLIENT_SECRETS_JSON`: alternatively, the full OAuth client JSON as a single-line JSON value.
- `GOOGLE_REDIRECT_URI`: keep the local URL below while testing on your computer.

Open <http://127.0.0.1:5000>. Choose **Author sign in**, approve access with
the Google account in `ADMIN_EMAIL`, then paste a Google Docs URL into the
author dashboard. The article will be visible on the public home page.

The first run creates `instance/blog.sqlite3`. Local OAuth tokens are stored
in that database; do not share or commit the database.

## Set up Google Docs import

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. Enable the **Google Drive API** for the project.
3. Configure the OAuth consent screen. While the app is in testing, add the
   account in `ADMIN_EMAIL` as a test user.
4. Create an OAuth client ID of type **Web application**.
5. Add this exact authorized redirect URI:
   `http://127.0.0.1:5000/oauth/callback`
6. Download the client JSON and save it to
   `blog_site/instance/client_secret.json` (create `instance` if it does not
   exist). Never put the JSON in a public folder or commit it.
7. Set `ADMIN_EMAIL` in `.env` to the same Google account, then run the app.

The app requests read-only Google Drive access. It does not edit or delete
Google Docs. Only the configured Google account can import articles; imported
articles are public on the blog.

## Deploy a public preview on Render

The included `render.yaml` configures a free Render web service. To publish:

1. Push this project to a GitHub repository. Do not commit `.env`, OAuth client
   JSON, Google token files, or the local `instance/` folder.
2. Create a Render account, choose **New > Blueprint**, and connect the
   repository containing `render.yaml`.
3. Enter the `ADMIN_EMAIL` for the Google account that will manage the blog.
   Render generates `FLASK_SECRET_KEY`; keep it private.
4. Once Render gives the service its `onrender.com` URL, set
   `GOOGLE_REDIRECT_URI` to
   `https://YOUR-SERVICE.onrender.com/oauth/callback`.
5. In Google Cloud Console, add that same HTTPS URL as an authorized redirect
   URI for the OAuth web client. Set `GOOGLE_CLIENT_SECRETS_JSON` in Render to
   the full contents of the downloaded OAuth client JSON. Keep it private.
6. Redeploy the service and open the Render URL.

Render's free web service can sleep while idle, so the first visit after a
period of inactivity may take longer. Its filesystem is temporary, so connect
a hosted database before publishing posts.

### Connect free Supabase storage

The app uses SQLite locally and switches to PostgreSQL when `DATABASE_URL` is
set. Its posts and Google OAuth refresh token are both stored in the database.

1. Create a project at [Supabase](https://supabase.com/dashboard). Keep the
   database password private and save it somewhere secure.
2. In the project dashboard, open **Connect** and select the **Session pooler**
   connection string. Copy the URI; do not paste it into chat, GitHub, or a
   public file.
3. In Render, open the `surya-writes-wisdom` service's **Environment** page and
   set `DATABASE_URL` to that connection string.
4. Save and deploy. Once Render reports **Live**, sign in to the author
   dashboard again so the Google token is stored in Supabase. Posts imported
   from then on are stored there as well.

The Supabase Free plan currently lists a 500 MB database and pauses projects
after a week of inactivity. It does not list automatic daily backups on the
Free plan. This is more durable than Render's temporary filesystem, but it is
not a backup; keep the original articles in Google Docs and export backups
periodically. Check Supabase's [current pricing](https://supabase.com/pricing)
before signing up, since plan limits can change.

The custom domain `suryawriteswisdom.com` is purchased separately from a domain
registrar. The free Render URL works without buying a domain. After registering
the custom domain, add it to the Render service and follow Render's DNS
instructions.
