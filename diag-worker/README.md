# Diagnostics relay (Cloudflare Worker)

Lets "Export Diagnostics" email the zip straight to the developer without the
user needing an email app. The app POSTs the zip here; the Worker mails it via
Resend. No email credentials are in the app.

One-time setup:
1. Free accounts: Cloudflare and Resend (resend.com). Sign up to Resend with the
   same address as `TO_EMAIL` -- the free `onboarding@resend.dev` sender can
   only deliver to the account owner's own address.
2. Create a Resend API key.
3. In this folder: `npx wrangler login`, then set a random `APP_TOKEN` in
   `wrangler.toml`, then
   `npx wrangler secret put RESEND_API_KEY` (paste the key) and `npx wrangler deploy`.
4. Put the deployed `https://pc2sonos-diagnostics.<you>.workers.dev` URL and the
   same token into `DIAG_UPLOAD_URL` / `DIAG_UPLOAD_TOKEN` in `diagnostics.py`.
   Until they're set the app just uses the download / Gmail fallback.
