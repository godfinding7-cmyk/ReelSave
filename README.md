# ReelSave - Instagram downloader

Folder structure:

```
reelsave/
  server.py          backend (Flask + yt-dlp)
  requirements.txt
  public/index.html  website (English + Hindi)
```

## Run locally

```
pip install -r requirements.txt
python server.py
```
Open http://localhost:8000

## Deploy (VPS / Render / Railway)

```
gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:8000 server:app
```
Behind nginx or Cloudflare, set `TRUST_PROXY=1` so rate limiting sees the real visitor IP.

## Keep it working

- Instagram changes often. Update weekly: `pip install -U yt-dlp` (then restart).
- If you get "private / login" errors for public posts, Instagram is blocking your server IP. Fixes: export a `cookies.txt` from a throwaway Instagram account and set `IG_COOKIES_FILE=/path/cookies.txt`, or route through a residential proxy with `IG_PROXY=http://user:pass@host:port`.
- Photo posts use a best-effort fallback and may return only one image.

## Before going live

- Rename "ReelSave" (search and replace in `index.html`).
- Add Privacy Policy, Terms and Contact pages (ad networks ask for them).
- Paste your ad code at the `AD SLOT` comment in `index.html`.
