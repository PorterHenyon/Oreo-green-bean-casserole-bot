# Oreo bot

Guild: `oreos` (`1354116143950073896`)

## Setup

1. Create/activate a virtual env.
2. Install deps:
   - `pip install -r requirements.txt`
3. Put your bot token into `.env`:
   - `DISCORD_TOKEN=...`
   - `GUILD_ID=1354116143950073896`
4. In the Discord Developer Portal for your bot, enable **Message Content Intent** (required to export message text).
5. Run:
   - `python main.py`

## Slash commands (guild-scoped)

- `/grabmessages member:<user> count:<1-500> [channel] [scan_limit] [include_attachments]`
  - Scans recent messages in the chosen channel and exports up to `count` messages from that member into a `.txt` attachment.
- `/reload`
  - Reloads extensions (cogs) without restarting the process.
- `/stop`
  - Stops the bot process.

