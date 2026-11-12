# Kerwan Cup Discord Bot

Runs League of Legends tournament matches from Discord, with Challonge as the bracket:

1. `/tournament publish` reads every playable match from Challonge and opens a **private thread** per match, adding both team captains.
2. Each captain clicks **Report score** and enters the series score. Reports are **blind**: neither captain sees the other's entry.
3. If both reports match, the bot updates **Challonge**, posts the result in the results channel, and **deletes the thread**. If they don't match, both reports are cleared and the admin role is pinged.
4. When Challonge unlocks the next matches, their threads open automatically.
5. When every match of a round is done, the bot posts an image of the updated bracket. A final bracket is posted when the tournament ends.

The format (single/double elimination, round robin, swiss, group stage + bracket) comes from Challonge, so nothing is hardcoded.

## How teams are matched to Discord roles

Name each team's Discord role **exactly like the team on Challonge** (case doesn't matter). The bot links them automatically and remembers the link, so renaming the role later is fine. If a name can't match, use `/team link`.

A team's captain is whoever has **both** the team's role and the captain role.

## One-time setup

### 1. Discord application
1. https://discord.com/developers/applications → **New Application** → **Bot** → **Reset Token** (this is `DISCORD_TOKEN`).
2. On the same page, turn on **Server Members Intent**.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`. Bot permissions:
   View Channels, Send Messages, Send Messages in Threads, Create Private Threads, Manage Threads, Embed Links, Attach Files, Read Message History, Mention Everyone *(only needed so it can ping the admin role if that role isn't mentionable)*.
4. Open the generated URL and add the bot to the server.
5. Make sure the bot can see the matches, results and bracket channels.

### 2. Database (Neon)
1. Create a free project at https://neon.tech.
2. **Connect** → copy the connection string (the *pooled* one is fine) into `DATABASE_URL`.
   Tables are created automatically on first start.

Any Postgres works (Supabase, Railway, local), only `DATABASE_URL` changes.

### 3. Challonge
Your username and API key from https://challonge.com/settings/developer. The tournament must be owned by (or shared as admin with) that account.

### 4. Run it
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use `source .venv/bin/activate` elsewhere
pip install -r requirements.txt
playwright install chromium      # for bracket images
copy .env.example .env           # then fill it in
python -m bot
```
Or with Docker: `docker build -t kerwancup . && docker run --env-file .env kerwancup`.

The bot must stay running for buttons to work. Your PC works for testing; for the tournament use an always-on host (Railway, Fly.io, a VPS, a Raspberry Pi…) using the Dockerfile.

## Running a tournament

| Command | What it does |
|---|---|
| `/setup` | Challonge tournament URL, captain role, matches channel, results channel, optional admin role, bracket channel and alerts channel |
| `/team list` | Every Challonge team with its linked role and captain(s), flags anything missing |
| `/team link` | Manually link a Challonge team to a role |
| `/tournament publish` | Sync with Challonge and open threads for playable matches (safe to run any time) |
| `/tournament status` | Matches waiting on reports |
| `/tournament bracket` | Post the current bracket image now |
| `/match force` | Set a result as admin (run inside the thread, or pass `match_id`) |
| `/match reset` | Clear both captains' reports |
| `/match resync` | Retry sending a confirmed result to Challonge |

Typical flow: add teams on Challonge → create matching Discord roles + give captains the captain role → `/setup` → `/team list` until nothing is flagged → **Start** the tournament on Challonge → `/tournament publish`. From then on it runs itself; when it's over, click **Finalize** on Challonge.

Admin commands are visible to members with **Manage Server**. To let your admin role use them too, go to Server Settings → Integrations → the bot → allow the role.

If a result is entered directly on Challonge, the next `/tournament publish` (or the next verified match) closes that thread and announces it.

## Notes
- Uses the Challonge v1 API, which Challonge has deprecated (still works, rate-limited). All Challonge calls are in `bot/challonge.py`.
- Ties are only accepted in round robin / swiss / group-stage matches.
