# AgentNew

# from the VM (recommended for a trial)
python backfill.py --folder opp123

# HTTP, see what would happen without touching anything
curl "https://<fn-url>/run?folder=opp123&dry_run=true"

# HTTP, real run
curl "https://<fn-url>/run?folder=opp123"
