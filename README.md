# PyHost — Free File Hosting on Netrun

## Deploy
1. Push this repo to GitHub.
2. Go to netrun.io → New Project → Connect GitHub → select repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Done. `/data` folder automatically persistent.

## Admin
- URL: `/x-admin-7k9p2`
- Default code: `1298` (change in Settings immediately)

## User
- Site root `/` → enter access code generated from admin panel.
